import argparse

from langchain.chat_models import init_chat_model
from langchain_core.documents import Document
from langchain_core.prompt_values import PromptValue
from langchain_core.prompts import ChatPromptTemplate

from app.config import settings
from app.retrieve import connect_store, ensure_collection_ready

TOP_K = 5
FILTERS = {"tenant": "fcai", "product": "fcai-cloud", "status": "published"}

SYSTEM_PROMPT = (
    "You are a support assistant for FCAI. "
    "Answer the user's question using ONLY the context below. "
    "Do not use prior knowledge and do not invent information. "
    "If the context does not contain enough information to answer, say clearly "
    "that there is not enough information in the knowledge base. "
    "Be objective and clear, and always answer in Portuguese.\n\n"
    "Context:\n{context}"
)


def format_context(documents: list[Document]) -> str:
    blocks = []
    for document in documents:
        metadata = document.metadata
        lines = [
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAG chat.")
    parser.add_argument("--show-prompt", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    ensure_collection_ready()
    store = connect_store()

    model = init_chat_model(
        settings.openai_chat_model,
        model_provider="openai",
        api_key=settings.openai_api_key,
    )
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

        documents = store.similarity_search(question, k=TOP_K, filter=FILTERS)
        prompt_value = prompt.invoke(
            {"context": format_context(documents), "question": question}
        )
        if args.show_prompt:
            print_prompt(prompt_value)

        response = model.invoke(prompt_value)

        print("\nAnswer:")
        print(response.text)


if __name__ == "__main__":
    main()
