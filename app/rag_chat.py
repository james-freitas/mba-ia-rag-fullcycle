"""RAG chat: plans the query, retrieves the chunks and answers using only them.

The planning step lives in app/query_planner.py. This file owns the conversation:
the final prompt, the structured answer, the sources and everything printed on screen.
"""

import argparse

from langchain.chat_models import init_chat_model
from langchain_core.documents import Document
from langchain_core.prompt_values import PromptValue
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.config import settings
from app.query_planner import (
    PLANNER_PROMPT,
    QueryPlan,
    build_filters,
    ensure_planner_covers_index,
    format_query_plan,
    search_chunks,
)
from app.retrieve import connect_store, ensure_collection_ready, print_chunk

PREVIEW_LIMIT = 300
NO_ANSWER_MESSAGE = (
    "Não encontrei informação suficiente na base de conhecimento "
    "para responder com segurança."
)
FALLBACK_CLARIFICATION = "Pode detalhar melhor a sua pergunta?"

SYSTEM_PROMPT = (
    "You are a support assistant for FCAI. "
    "Answer the user's question using ONLY the context below. "
    "Do not use prior knowledge and do not invent information or sources.\n\n"
    "Rules:\n"
    "- Write 'answer' in Portuguese, objective and clear.\n"
    "- Set 'has_answer' to true only if the context supports the answer.\n"
    "- 'used_chunk_ids' must contain only Chunk IDs present in the context.\n"
    f"- If the context is not enough, set 'has_answer' to false, set 'answer' to "
    f"'{NO_ANSWER_MESSAGE}' and leave 'used_chunk_ids' empty.\n\n"
    "Context:\n{context}"
)

ANSWER_PROMPT = ChatPromptTemplate.from_messages(
    [("system", SYSTEM_PROMPT), ("human", "{question}")]
)


class RagAnswer(BaseModel):
    answer: str = Field(description="Answer in Portuguese, based only on the context.")
    has_answer: bool = Field(description="True only if the context supports the answer.")
    used_chunk_ids: list[str] = Field(
        description="Chunk IDs from the context that support the answer."
    )


def format_context(documents: list[Document]) -> str:
    # The chunk text already carries title, section, plan and version (context header
    # added at ingestion). The model only needs the id it must cite back.
    blocks = [
        f"[Chunk ID: {document.metadata.get('chunk_id')}]\nContent:\n{document.page_content}"
        for document in documents
    ]
    return "\n\n".join(blocks)


def print_prompt(prompt_value: PromptValue, label: str) -> None:
    print(f"\n--- {label} ---")
    for message in prompt_value.to_messages():
        print(f"\n[{message.type}]")
        print(message.text)
    print(f"\n--- End of {label.lower()} ---")


def print_debug(results: list[tuple[Document, float]]) -> None:
    print("\n--- Retrieved chunks ---\n")
    if not results:
        print("No chunks retrieved.")
    for rank, (document, score) in enumerate(results, start=1):
        print_chunk(rank, document, score, preview_limit=PREVIEW_LIMIT)
    print("--- End of retrieved chunks ---")


def print_sources(documents: list[Document], used_chunk_ids: list[str]) -> None:
    documents_by_id = {
        document.metadata.get("chunk_id"): document for document in documents
    }
    sources = []
    for chunk_id in used_chunk_ids:
        document = documents_by_id.get(chunk_id)
        if document is not None and document not in sources:
            sources.append(document)

    print("\nSources:")
    if not sources:
        print("No sources.")
        return

    for rank, document in enumerate(sources, start=1):
        metadata = document.metadata
        print(f"{rank}. {metadata.get('source_file')}")
        print(f"   Title: {metadata.get('title')}")
        if metadata.get("section"):
            print(f"   Section: {metadata['section']}")
        print(f"   Version: {metadata.get('version')}")


def print_clarification(query_plan: QueryPlan) -> None:
    print("\nAnswer:")
    print(query_plan.clarification_question or FALLBACK_CLARIFICATION)


def print_no_answer() -> None:
    print("\nAnswer:")
    print(NO_ANSWER_MESSAGE)
    print("\nSources:")
    print("No sources.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAG chat.")
    parser.add_argument("--show-prompt", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    ensure_collection_ready()
    ensure_planner_covers_index()
    store = connect_store()

    # temperature=0: the planner is a classifier, and the same question must always
    # produce the same plan. With the default temperature it flip-flopped between
    # asking for clarification and guessing.
    model = init_chat_model(
        settings.openai_chat_model,
        model_provider="openai",
        api_key=settings.openai_api_key,
        temperature=0,
    )
    planner = model.with_structured_output(QueryPlan)
    answerer = model.with_structured_output(RagAnswer)

    print("RAG chat started.")
    print("This chat uses the internal knowledge base.")
    print("Type 'exit' to quit.")

    while True:
        question = input("\n> ").strip()
        if question.lower() in {"exit", "quit"}:
            break
        if not question:
            continue

        planner_prompt_value = PLANNER_PROMPT.invoke({"question": question})
        if args.show_prompt:
            print_prompt(planner_prompt_value, "Query planner prompt")

        query_plan = planner.invoke(planner_prompt_value)
        filters = build_filters(query_plan)
        if args.debug:
            print(format_query_plan(query_plan, filters))

        if query_plan.needs_clarification:
            print_clarification(query_plan)
            continue

        results = search_chunks(store, query_plan, filters)
        if args.debug:
            print_debug(results)

        if not results:
            print_no_answer()
            continue

        documents = [document for document, _ in results]
        prompt_value = ANSWER_PROMPT.invoke(
            {"context": format_context(documents), "question": question}
        )
        if args.show_prompt:
            print_prompt(prompt_value, "Final answer prompt")

        response = answerer.invoke(prompt_value)

        print("\nAnswer:")
        print(response.answer)

        if response.has_answer:
            print_sources(documents, response.used_chunk_ids)
        else:
            print("\nSources:")
            print("No sources.")


if __name__ == "__main__":
    main()
