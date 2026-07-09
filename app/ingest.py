import sys
from pathlib import Path

import yaml

KNOWLEDGE_BASE_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"

REQUIRED_METADATA = [
    "title",
    "tenant",
    "product",
    "plan",
    "doc_type",
    "version",
    "status",
    "visibility",
]


class IngestionError(Exception):
    pass


def parse_document(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise IngestionError(f"{path.name}: file is empty")
    if not raw.startswith("---"):
        raise IngestionError(f"{path.name}: missing YAML front matter")

    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise IngestionError(f"{path.name}: invalid front matter delimiters")

    try:
        metadata = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise IngestionError(f"{path.name}: invalid YAML front matter ({exc})")

    if not isinstance(metadata, dict):
        raise IngestionError(f"{path.name}: front matter is not a mapping")

    missing = [key for key in REQUIRED_METADATA if key not in metadata]
    if missing:
        raise IngestionError(
            f"{path.name}: missing required metadata: {', '.join(missing)}"
        )

    content = parts[2].strip()
    if not content:
        raise IngestionError(f"{path.name}: document body is empty")

    return {
        "path": str(path),
        "file_name": path.name,
        "metadata": metadata,
        "content": content,
        "content_length": len(content),
    }


def load_documents() -> list[dict]:
    if not KNOWLEDGE_BASE_DIR.is_dir():
        raise IngestionError(
            f"knowledge base directory not found: {KNOWLEDGE_BASE_DIR}"
        )

    paths = sorted(KNOWLEDGE_BASE_DIR.glob("*.md"))
    if not paths:
        raise IngestionError(
            f"no Markdown documents found in {KNOWLEDGE_BASE_DIR}"
        )

    return [parse_document(path) for path in paths]


def print_summary(documents: list[dict]) -> None:
    print("Loading documents from knowledge_base...\n")
    print("Loaded documents:\n")

    for index, document in enumerate(documents, start=1):
        metadata = document["metadata"]
        print(f"{index}. {document['file_name']}")
        print(f"   Title: {metadata['title']}")
        print(f"   Tenant: {metadata['tenant']}")
        print(f"   Product: {metadata['product']}")
        print(f"   Plan: {metadata['plan']}")
        print(f"   Type: {metadata['doc_type']}")
        print(f"   Version: {metadata['version']}")
        print(f"   Status: {metadata['status']}")
        print(f"   Content length: {document['content_length']} chars")
        print()

    print(f"Total documents loaded: {len(documents)}\n")
    print("Ingestion validation completed.")


def main() -> None:
    try:
        documents = load_documents()
    except IngestionError as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print_summary(documents)


if __name__ == "__main__":
    main()
