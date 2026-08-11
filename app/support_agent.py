"""The Support Triage Agent: a model choosing which of three tools to use, and when.

The Knowledge Chat has one path — question in, answer out. Here the model decides:
answer from the documentation, read the tenant's live usage, open a ticket, ask a
question back, or refuse. The interesting failures are not wrong answers; they are
wrong *choices*, and a wrong choice can write to disk.

So two things are captured on purpose. The final answer is structured output validated
by Pydantic, and the tool calls are read off the messages the agent returned — never
parsed out of prose the model wrote about itself. A trajectory reconstructed from the
model's own narration would be measuring the narration.

Nothing here is evaluated yet. This is the run that a later step will score.
"""

import argparse

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from pydantic import BaseModel, Field

from app.config import settings
from app.support_tools import SUPPORT_TOOLS

SYSTEM_PROMPT = (
    "You are a support triage agent for FCAI Cloud. Always answer in Portuguese.\n\n"
    "Use tools only when they are needed, and never more than the question requires.\n\n"
    "- search_knowledge_base: questions about documentation, plans, SLA, billing, the "
    "product, the company and its policies.\n"
    "- get_current_usage: live usage only — quota consumption, current ingestion, "
    "projected overage.\n"
    "- create_support_ticket: only when the user clearly asks to open a ticket.\n\n"
    "Never answer a live usage question from the documentation, and never answer a "
    "policy question from the usage tool. One reports the numbers; the other explains "
    "the rules.\n\n"
    "To open a ticket you need two things: a severity, and what is broken. When the "
    "user gives you both — 'abra um chamado P1, o painel está fora do ar' — that is "
    "enough: write the summary yourself from what they told you and open it. Do not ask "
    "for detail they already gave.\n\n"
    "Ask one clarification question only when you genuinely cannot fill those two. "
    "'Abre um chamado urgente aí' names no severity and no problem, so it needs one.\n\n"
    "Never invent live usage numbers and never invent a ticket id: both come from "
    "tools, or they do not exist.\n\n"
    "If the request has nothing to do with FCAI Cloud, say so plainly instead of "
    "answering it. Keep the final response short."
)


class SupportAgentResult(BaseModel):
    answer: str = Field(description="The reply to the user, in Portuguese.")
    needs_clarification: bool = Field(description="True when you asked a question back.")
    ticket_created: bool = Field(description="True only if create_support_ticket ran.")
    ticket_id: str | None = Field(
        default=None, description="The id the tool returned. Never invent one."
    )
    task_completed: bool = Field(description="True when the request was fully handled.")
    reason: str = Field(description="One short sentence, in Portuguese, on why you did that.")


class AgentToolCall(BaseModel):
    name: str
    arguments: dict


class AgentRunDebug(BaseModel):
    tool_calls: list[AgentToolCall] = Field(default_factory=list)
    steps_count: int = 0


class AgentRun(BaseModel):
    result: SupportAgentResult
    debug: AgentRunDebug | None = None


_agent = None


def get_agent():
    # Built on first use, like the pipeline: the tools import lazily too, so a run that
    # never touches the knowledge base never opens Postgres.
    global _agent
    if _agent is None:
        model = init_chat_model(
            settings.openai_chat_model,
            model_provider="openai",
            api_key=settings.openai_api_key,
            temperature=0,
            max_tokens=settings.ai_max_output_tokens,
        )
        _agent = create_agent(
            model,
            tools=SUPPORT_TOOLS,
            system_prompt=SYSTEM_PROMPT,
            response_format=SupportAgentResult,
        )
    return _agent


def extract_tool_calls(messages: list) -> list[AgentToolCall]:
    """Read the trajectory off the messages, not off what the model said it did.

    Every tool call the agent made is recorded on an AIMessage by the framework. A
    trajectory rebuilt from the model's own prose would be scoring the narration.
    """
    return [
        AgentToolCall(name=call["name"], arguments=call.get("args") or {})
        for message in messages
        for call in getattr(message, "tool_calls", None) or []
    ]


def run_support_agent(message: str, include_debug: bool = False) -> AgentRun:
    state = get_agent().invoke({"messages": [{"role": "user", "content": message}]})

    tool_calls = extract_tool_calls(state["messages"])
    debug = AgentRunDebug(tool_calls=tool_calls, steps_count=len(tool_calls))
    return AgentRun(
        result=state["structured_response"],
        debug=debug if include_debug else None,
    )


def print_run(run: AgentRun) -> None:
    result = run.result
    print(f"\nAnswer: {result.answer}")
    if result.ticket_id:
        print(f"Ticket: {result.ticket_id}")

    if not run.debug:
        return

    print(f"\nSteps: {run.debug.steps_count}")
    for index, call in enumerate(run.debug.tool_calls, start=1):
        print(f"  {index}. {call.name}({call.arguments})")
    flags = {
        "needs_clarification": result.needs_clarification,
        "ticket_created": result.ticket_created,
        "task_completed": result.task_completed,
    }
    print("  " + ", ".join(f"{name}={value}" for name, value in flags.items()))
    print(f"  reason: {result.reason}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Support Triage Agent, in the terminal.")
    parser.add_argument(
        "--debug", action="store_true", help="show the tool calls the agent made"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print("Support Triage Agent started.")
    print("Type 'exit' to quit.")

    while True:
        try:
            message = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if message.lower() in {"exit", "quit"}:
            return
        if not message:
            continue

        print_run(run_support_agent(message, include_debug=args.debug))


if __name__ == "__main__":
    main()
