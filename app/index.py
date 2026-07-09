import json
import sys
from pathlib import Path

from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_postgres import PGEngine, PGVectorStore

from app.config import settings
from app.db import enable_vector_extension
from app.ingest import OUTPUT_PATH as CHUNKS_PATH

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TABLE_NAME = "fcai_knowledge_base"
ID_COLUMN = {"name": "chunk_id", "data_type": "TEXT", "nullable": False}


class IndexingError(Exception):
    pass


def load_chunks() -> list[Document]:
    if not CHUNKS_PATH.exists():
        raise IndexingError(f"chunks file not found: {CHUNKS_PATH}")

    documents: list[Document] = []
    with CHUNKS_PATH.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IndexingError(f"line {number}: invalid JSON ({exc})")

            content = record.get("content")
            metadata = record.get("metadata") or {}
            if not content:
                raise IndexingError(f"line {number}: missing content")
            if not metadata.get("chunk_id"):
                raise IndexingError(f"line {number}: missing metadata.chunk_id")

            documents.append(Document(page_content=content, metadata=metadata))

    if not documents:
        raise IndexingError(f"no chunks found in {CHUNKS_PATH}")

    return documents


def index_chunks(documents: list[Document]) -> None:
    # Embedding model that turns each chunk's text into a vector.
    embeddings = OpenAIEmbeddings(
        model=settings.openai_embedding_model,
        api_key=settings.openai_api_key,
    )
    # Ask the model for one embedding to learn its vector dimension,
    # which the table column needs to be sized correctly.
    vector_size = len(embeddings.embed_query("dimension probe"))

    # (Re)create the vector table; overwrite_existing keeps reruns idempotent.
    engine = PGEngine.from_connection_string(url=settings.database_url)
    engine.init_vectorstore_table(
        table_name=TABLE_NAME,
        vector_size=vector_size,
        id_column=ID_COLUMN,
        overwrite_existing=True,
    )

    # Open the store over that table and write the chunks, using each
    # chunk_id as the row id so a rerun updates instead of duplicating.
    store = PGVectorStore.create_sync(
        engine=engine,
        embedding_service=embeddings,
        table_name=TABLE_NAME,
        id_column=ID_COLUMN["name"],
    )
    store.add_documents(
        documents, ids=[document.metadata["chunk_id"] for document in documents]
    )


def main() -> None:
    print(f"Loading chunks from {CHUNKS_PATH.relative_to(PROJECT_ROOT)}...")
    try:
        documents = load_chunks()
    except IndexingError as exc:
        print(f"Indexing failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Loaded chunks: {len(documents)}")

    print("Connecting to Postgres...")
    enable_vector_extension()
    print("pgvector extension enabled.")

    print(f"Indexing chunks into table: {TABLE_NAME}")
    index_chunks(documents)

    print(f"Indexed chunks: {len(documents)}")
    print("Indexing completed.")


if __name__ == "__main__":
    main()
