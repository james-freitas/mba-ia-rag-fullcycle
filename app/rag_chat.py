import argparse

from langchain.chat_models import init_chat_model
from langchain_core.documents import Document
from langchain_core.prompt_values import PromptValue
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.config import settings
from app.retrieve import connect_store, ensure_collection_ready

TOP_K = 5
FILTERS = {"tenant": "fcai", "product": "fcai-cloud", "status": "published"}
PREVIEW_LIMIT = 300
NO_ANSWER_MESSAGE = (
    "Não encontrei informação suficiente na base de conhecimento "
    "para responder com segurança."
)

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


class RagAnswer(BaseModel):
    answer: str = Field(description="Answer in Portuguese, based only on the context.")
    has_answer: bool = Field(description="True only if the context supports the answer.")
    used_chunk_ids: list[str] = Field(
        description="Chunk IDs from the context that support the answer."
    )


def format_context(documents: list[Document]) -> str:
    blocks = []
    for document in documents:
        metadata = document.metadata
        lines = [
            f"[Chunk ID: {metadata.get('chunk_id')}]",
            f"[Document: {metadata.get('source_file')}]",
            f"[Title: {metadata.get('title')}]",
        ]
        if metadata.get("section"):
            lines.append(f"[Section: {metadata['section']}]")
        lines.append(f"[Version: {metadata.get('version')}]")
        lines.append("Content:")
        lines.append(document.page_content)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def print_prompt(prompt_value: PromptValue) -> None:
    print("\n--- Prompt sent to the model ---")
    for message in prompt_value.to_messages():
        print(f"\n[{message.type}]")
        print(message.text)
    print("\n--- End of prompt ---")


def print_debug(results: list[tuple[Document, float]]) -> None:
    print("\n--- Retrieved chunks ---")
    if not results:
        print("No chunks retrieved.")
    for rank, (document, score) in enumerate(results, start=1):
        metadata = document.metadata
        preview = " ".join(document.page_content.split())[:PREVIEW_LIMIT]
        print(f"\n{rank}. Score: {score:.2f}")
        print(f"   Chunk ID: {metadata.get('chunk_id')}")
        print(f"   Source: {metadata.get('source_file')}")
        print(f"   Title: {metadata.get('title')}")
        if metadata.get("section"):
            print(f"   Section: {metadata['section']}")
        print(f"   Version: {metadata.get('version')}")
        print(f"   Preview: {preview}")
    print("\n--- End of retrieved chunks ---")


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
    store = connect_store()

    model = init_chat_model(
        settings.openai_chat_model,
        model_provider="openai",
        api_key=settings.openai_api_key,
    ).with_structured_output(RagAnswer)
    prompt = ChatPromptTemplate.from_messages(
        [("system", SYSTEM_PROMPT), ("human", "{question}")]
    )

    print("RAG chat started.")
    print("This chat uses the internal knowledge base.")
    print("Type 'exit' to quit.")

    while True:
        question = input("\n> ").strip()
        if question.lower() in {"exit", "quit"}:
            break
        if not question:
            continue

        results = store.similarity_search_with_score(
            question, k=TOP_K, filter=FILTERS
        )
        if args.debug:
            print_debug(results)

        if not results:
            print_no_answer()
            continue

        documents = [document for document, _ in results]
        prompt_value = prompt.invoke(
            {"context": format_context(documents), "question": question}
        )
        if args.show_prompt:
            print_prompt(prompt_value)

        response = model.invoke(prompt_value)

        print("\nAnswer:")
        print(response.answer)

        if response.has_answer:
            print_sources(documents, response.used_chunk_ids)
        else:
            print("\nSources:")
            print("No sources.")


if __name__ == "__main__":
    main()
