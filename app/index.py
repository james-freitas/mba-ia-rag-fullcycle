import argparse
import json
import sys
from pathlib import Path

from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_postgres import PGEngine, PGVectorStore

from app.config import settings
from app.db import enable_vector_extension, get_connection
from app.ingest import OUTPUT_PATH as CHUNKS_PATH
from app.manifest import MANIFEST_PATH, load_manifest, save_manifest

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
            for field in ("chunk_id", "source_file", "document_hash"):
                if not metadata.get(field):
                    raise IndexingError(f"line {number}: missing metadata.{field}")

            documents.append(Document(page_content=content, metadata=metadata))

    if not documents:
        raise IndexingError(f"no chunks found in {CHUNKS_PATH}")

    return documents


def group_by_source(documents: list[Document]) -> dict[str, list[Document]]:
    grouped: dict[str, list[Document]] = {}
    for document in documents:
        grouped.setdefault(document.metadata["source_file"], []).append(document)
    return grouped


def classify_sources(
    grouped: dict[str, list[Document]], indexed_sources: dict
) -> dict[str, list[str]]:
    changes: dict[str, list[str]] = {
        "new": [],
        "changed": [],
        "unchanged": [],
        "removed": [],
    }
    for source, documents in grouped.items():
        previous = indexed_sources.get(source)
        if previous is None:
            changes["new"].append(source)
        elif previous["document_hash"] != documents[0].metadata["document_hash"]:
            changes["changed"].append(source)
        else:
            changes["unchanged"].append(source)
    changes["removed"] = [
        source for source in indexed_sources if source not in grouped
    ]
    return changes


def table_exists() -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT to_regclass(%s)", (f"public.{TABLE_NAME}",)
        ).fetchone()
    return row[0] is not None


def connect_store(
    embeddings: OpenAIEmbeddings, create_table: bool, *, table_name: str = TABLE_NAME
) -> PGVectorStore:
    engine = PGEngine.from_connection_string(url=settings.database_url)
    if create_table:
        # The table column size must match the embedding model, so ask the
        # model for one embedding to learn its vector dimension.
        vector_size = len(embeddings.embed_query("dimension probe"))
        engine.init_vectorstore_table(
            table_name=table_name,
            vector_size=vector_size,
            id_column=ID_COLUMN,
            overwrite_existing=True,
        )
    return PGVectorStore.create_sync(
        engine=engine,
        embedding_service=embeddings,
        table_name=table_name,
        id_column=ID_COLUMN["name"],
    )


def manifest_entry(documents: list[Document]) -> dict:
    metadata = documents[0].metadata
    return {
        "document_hash": metadata["document_hash"],
        "title": metadata.get("title"),
        "tenant": metadata.get("tenant"),
        "product": metadata.get("product"),
        "version": metadata.get("version"),
        "status": metadata.get("status"),
        "chunk_ids": [document.metadata["chunk_id"] for document in documents],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index chunks into pgvector, reindexing only what changed."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete every indexed chunk and reindex all current chunks",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print(f"Loading chunks from {CHUNKS_PATH.relative_to(PROJECT_ROOT)}...")
    try:
        documents = load_chunks()
    except IndexingError as exc:
        print(f"Indexing failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    grouped = group_by_source(documents)
    print(f"Loaded chunks: {len(documents)}")
    print(f"Sources in chunks: {len(grouped)}")

    print("Connecting to Postgres...")
    enable_vector_extension()

    indexed_sources = load_manifest().get("sources", {})

    # Without a manifest (or without the table) the index state is unknown,
    # so rebuild the table from scratch instead of diffing against stale rows.
    rebuild = not indexed_sources or not table_exists()
    if rebuild:
        indexed_sources = {}

    changes = classify_sources(grouped, indexed_sources)
    print(f"New sources: {len(changes['new'])}")
    print(f"Changed sources: {len(changes['changed'])}")
    print(f"Removed sources: {len(changes['removed'])}")
    print(f"Unchanged sources: {len(changes['unchanged'])}")

    if args.force:
        delete_ids = [
            chunk_id
            for entry in indexed_sources.values()
            for chunk_id in entry["chunk_ids"]
        ]
        sources_to_index = list(grouped)
    else:
        delete_ids = [
            chunk_id
            for source in changes["removed"] + changes["changed"]
            for chunk_id in indexed_sources[source]["chunk_ids"]
        ]
        sources_to_index = changes["new"] + changes["changed"]

    documents_to_index = [
        document for source in sources_to_index for document in grouped[source]
    ]

    embeddings = OpenAIEmbeddings(
        model=settings.openai_embedding_model,
        api_key=settings.openai_api_key,
    )
    store = connect_store(embeddings, create_table=rebuild)

    if delete_ids:
        store.delete(ids=delete_ids)
    if documents_to_index:
        store.add_documents(
            documents_to_index,
            ids=[document.metadata["chunk_id"] for document in documents_to_index],
        )

    save_manifest(
        {source: manifest_entry(docs) for source, docs in grouped.items()}
    )

    print(f"Deleted chunks: {len(delete_ids)}")
    print(f"Indexed chunks: {len(documents_to_index)}")
    print(f"Manifest saved to {MANIFEST_PATH.relative_to(PROJECT_ROOT)}")
    print("Indexing completed.")


if __name__ == "__main__":
    main()
