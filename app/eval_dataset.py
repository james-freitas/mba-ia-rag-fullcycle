"""The evaluation dataset: the questions the Knowledge Chat is expected to get right.

The cases live in a JSONL file inside the repository, so they are versioned and
reviewed like code. This module validates them and pushes them to Langfuse, which
is where the experiments and the scores will run later.

Nothing here calls the RAG pipeline: this step only creates the dataset.
"""

import argparse
import sys

# The evaluation modules are the only place the application imports a backend SDK.
# Tracing stays vendor-neutral over OTLP (see app/observability.py), but datasets and
# experiments have no such protocol: they are a Langfuse API, so here the dependency
# is the only way in.
from langfuse import Langfuse
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.config import PROJECT_ROOT, settings
from app.ingest import KNOWLEDGE_BASE_DIR

# DocType and Plan come from the planner on purpose: what a case expects has to be
# something the retrieval can actually filter by, or the expectation is unmeasurable.
from app.query_planner import DocType, Plan

DATASET_NAME = "fcai-knowledge-chat-v1"
DATASET_DESCRIPTION = (
    "Perguntas representativas do FCAI Knowledge Chat: casos respondíveis, "
    "casos fora da base e casos ambíguos."
)

DATASET_PATH = PROJECT_ROOT / "evals" / "fcai_knowledge_chat.jsonl"


class DatasetError(Exception):
    pass


class EvalCase(BaseModel):
    id: str
    question: str
    should_answer: bool
    should_clarify: bool
    accepted_source_files: list[str] = Field(default_factory=list)
    required_terms: list[str] = Field(default_factory=list)
    expected_doc_types: list[DocType] = Field(default_factory=list)
    expected_plan: Plan | None = None
    tags: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def check_case(self) -> "EvalCase":
        if not self.id.strip():
            raise ValueError("id cannot be empty")
        if not self.question.strip():
            raise ValueError("question cannot be empty")
        if self.should_answer and self.should_clarify:
            raise ValueError("should_answer and should_clarify cannot both be true")
        if self.should_clarify and self.accepted_source_files:
            raise ValueError("a clarification case cannot have accepted source files")
        if self.should_answer and not self.accepted_source_files:
            raise ValueError("an answerable case needs accepted source files")
        if self.should_answer and not self.required_terms:
            raise ValueError("an answerable case needs required terms")
        return self


def load_cases() -> list[EvalCase]:
    if not DATASET_PATH.exists():
        raise DatasetError(f"dataset file not found: {DATASET_PATH}")

    cases: list[EvalCase] = []
    with DATASET_PATH.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                cases.append(EvalCase.model_validate_json(line))
            except ValidationError as exc:
                raise DatasetError(f"line {number}: {exc}")

    if not cases:
        raise DatasetError(f"no cases found in {DATASET_PATH}")

    return cases


def validate_dataset(cases: list[EvalCase]) -> list[str]:
    errors: list[str] = []

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            errors.append(f"duplicated id: {case.id}")
        seen.add(case.id)

    # A source file that does not exist would make every future run fail the case for
    # the wrong reason, so a typo has to be caught here and not during an experiment.
    known_sources = {path.name for path in KNOWLEDGE_BASE_DIR.glob("*.md")}
    for case in cases:
        unknown = sorted(set(case.accepted_source_files) - known_sources)
        if unknown:
            errors.append(f"{case.id}: unknown source files: {', '.join(unknown)}")

    return errors


def connect_langfuse() -> Langfuse:
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        raise DatasetError(
            "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required to reach the "
            "dataset. Set them in .env (see .env.example)."
        )

    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
    if not client.auth_check():
        raise DatasetError(f"Langfuse rejected the credentials at {settings.langfuse_host}")

    return client


def item_field(item, name: str):
    # Langfuse hands a DatasetItem; a local run hands a plain dict built by build_item.
    return item[name] if isinstance(item, dict) else getattr(item, name)


def select_items(items: list, case_id: str | None, limit: int | None) -> list:
    if case_id:
        items = [item for item in items if item_field(item, "id") == case_id]
        if not items:
            raise DatasetError(f"case not found in the dataset: {case_id}")

    return items[:limit] if limit else items


def build_item(case: EvalCase) -> dict:
    return {
        # The case id is the item id, so syncing again updates the item instead of
        # creating a second copy of the same question.
        "id": case.id,
        "input": {"question": case.question},
        # Whatever is not the item's identity or its input is the expectation.
        "expected_output": case.model_dump(exclude={"id", "question"}),
        # Repeated out of the expectation on purpose: here they are an index to
        # group and filter the items by, not something the system has to produce.
        "metadata": {"tags": case.tags},
    }


def sync_dataset(cases: list[EvalCase]) -> int:
    client = connect_langfuse()
    client.create_dataset(name=DATASET_NAME, description=DATASET_DESCRIPTION)

    for case in cases:
        client.create_dataset_item(dataset_name=DATASET_NAME, **build_item(case))

    client.flush()
    return len(cases)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluation dataset for the RAG chat.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the JSONL file without sending anything to Langfuse",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        print("Loading evaluation cases...")
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
        print("Dataset sync completed.")
    except DatasetError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
