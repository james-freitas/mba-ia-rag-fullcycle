"""The agent evaluation dataset: what the triage agent is expected to *do*.

The Knowledge Chat dataset holds expected answers. This one holds expected **behaviour**:
which tools should be called, which must not be, in what order, with which arguments,
and how many steps it may take. An agent that reaches the right answer through the wrong
tools is a different system from one that reaches it through the right ones — and only
the second is safe to change later.

Nothing here runs the agent or the tools. This step only writes the criterion down, and
checks it is expressible: a case demanding a tool that does not exist would be
unmeasurable, so the valid names come from SUPPORT_TOOLS itself.
"""

import argparse
import sys
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.config import PROJECT_ROOT
from app.eval_dataset import DatasetError, connect_langfuse
from app.support_tools import SUPPORT_TOOLS

DATASET_NAME = "fcai-support-triage-agent-v1"
DATASET_DESCRIPTION = (
    "Comportamento esperado do Support Triage Agent: quais ferramentas chamar, quais "
    "não chamar, em que ordem e com quais argumentos."
)

DATASET_PATH = PROJECT_ROOT / "evals" / "support_triage_agent.jsonl"

# Read off the tools rather than typed out here: a case that expects a tool the agent
# does not have is not a failing case, it is an unmeasurable one.
TOOL_NAMES = frozenset(tool.name for tool in SUPPORT_TOOLS)
TICKET_TOOL = "create_support_ticket"
SEVERITIES = ("P1", "P2", "P3")

TrajectoryMatch = Literal["exact", "in_order", "any_order"]


class AgentEvalCase(BaseModel):
    id: str
    input: str
    should_answer: bool
    should_clarify: bool
    should_create_ticket: bool
    should_refuse: bool
    expected_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    expected_arguments: dict = Field(default_factory=dict)
    expected_trajectory: list[str] = Field(default_factory=list)
    trajectory_match_type: TrajectoryMatch
    max_steps: int = Field(ge=0)
    expected_terms: list[str] = Field(default_factory=list)
    tags: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def check_case(self) -> "AgentEvalCase":
        if not self.id.strip():
            raise ValueError("id cannot be empty")
        if not self.input.strip():
            raise ValueError("input cannot be empty")

        unknown = (
            set(self.expected_tools)
            | set(self.forbidden_tools)
            | set(self.expected_trajectory)
            | set(self.expected_arguments)
        ) - TOOL_NAMES
        if unknown:
            raise ValueError(f"unknown tools: {', '.join(sorted(unknown))}")

        # The trajectory is the order of the expected tools, so it cannot introduce one.
        outside = set(self.expected_trajectory) - set(self.expected_tools)
        if outside:
            raise ValueError(f"trajectory has tools not expected: {', '.join(sorted(outside))}")

        if self.should_clarify and self.should_create_ticket:
            raise ValueError("should_clarify and should_create_ticket cannot both be true")
        if self.should_refuse and self.should_create_ticket:
            raise ValueError("should_refuse and should_create_ticket cannot both be true")

        if self.should_create_ticket and TICKET_TOOL not in self.expected_tools:
            raise ValueError(f"a ticket case must expect {TICKET_TOOL}")
        # Asking a question back or turning the request down are the two outcomes that
        # reach no tool at all: expecting one would contradict the outcome itself.
        if self.should_refuse and self.expected_tools:
            raise ValueError("a refusal case cannot expect tools")
        if self.should_clarify and self.expected_tools:
            raise ValueError("a clarification case cannot expect tools")

        severity = self.expected_arguments.get(TICKET_TOOL, {}).get("severity")
        if severity is not None and severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")

        if len(self.expected_trajectory) > self.max_steps:
            raise ValueError(
                f"trajectory has {len(self.expected_trajectory)} steps but max_steps is "
                f"{self.max_steps}"
            )

        return self


def load_cases() -> list[AgentEvalCase]:
    if not DATASET_PATH.exists():
        raise DatasetError(f"dataset file not found: {DATASET_PATH}")

    cases: list[AgentEvalCase] = []
    with DATASET_PATH.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                cases.append(AgentEvalCase.model_validate_json(line))
            except ValidationError as exc:
                raise DatasetError(f"line {number}: {exc}")

    if not cases:
        raise DatasetError(f"no cases found in {DATASET_PATH}")

    return cases


def validate_dataset(cases: list[AgentEvalCase]) -> list[str]:
    errors: list[str] = []

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            errors.append(f"duplicated id: {case.id}")
        seen.add(case.id)

    # A dataset where every case expects one tool never exercises the ordering rules, so
    # the trajectory fields would be machinery nothing tests.
    if not any(len(case.expected_trajectory) > 1 for case in cases):
        errors.append("no case expects more than one tool: the trajectory is never tested")

    return errors


def build_item(case: AgentEvalCase) -> dict:
    return {
        "id": case.id,
        "input": {"message": case.input},
        "expected_output": case.model_dump(exclude={"id", "input"}),
        "metadata": {"tags": case.tags, "case_type": "agent"},
    }


def sync_dataset(cases: list[AgentEvalCase]) -> int:
    client = connect_langfuse()
    client.create_dataset(name=DATASET_NAME, description=DATASET_DESCRIPTION)

    for case in cases:
        client.create_dataset_item(dataset_name=DATASET_NAME, **build_item(case))

    client.flush()
    return len(cases)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluation dataset for the triage agent.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the JSONL file without sending anything to Langfuse",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        print("Loading agent evaluation cases...")
        cases = load_cases()
        print(f"Loaded cases: {len(cases)}")

        print("Validating dataset...")
        errors = validate_dataset(cases)
        if errors:
            raise DatasetError("\n".join(errors))

        if args.validate_only:
            print("Dataset is valid.")
            return

        print(f"Dataset: {DATASET_NAME}")
        synced = sync_dataset(cases)
        print(f"Synced items: {synced}")
        print("Agent dataset sync completed.")
    except DatasetError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
