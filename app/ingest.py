import argparse
import json
import sys
from pathlib import Path

import frontmatter
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_BASE_DIR = PROJECT_ROOT / "knowledge_base"
OUTPUT_PATH = PROJECT_ROOT / "data" / "chunks.jsonl"

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

HEADERS_TO_SPLIT_ON = [
    ("#", "h1"),
    ("##", "h2"),
    ("###", "h3"),
]

CHUNK_SIZE = 900
CHUNK_OVERLAP = 150


class IngestionError(Exception):
    pass


def load_document(path: Path) -> frontmatter.Post:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise IngestionError(f"{path.name}: file is empty")

    post = frontmatter.loads(raw)
    if not post.metadata:
        raise IngestionError(f"{path.name}: missing YAML front matter")

    missing = [key for key in REQUIRED_METADATA if key not in post.metadata]
    if missing:
        raise IngestionError(
            f"{path.name}: missing required metadata: {', '.join(missing)}"
        )

    if not post.content.strip():
        raise IngestionError(f"{path.name}: document body is empty")

    return post


def load_documents() -> list[tuple[Path, frontmatter.Post]]:
    if not KNOWLEDGE_BASE_DIR.is_dir():
        raise IngestionError(
            f"knowledge base directory not found: {KNOWLEDGE_BASE_DIR}"
        )

    paths = sorted(KNOWLEDGE_BASE_DIR.glob("*.md"))
    if not paths:
        raise IngestionError(
            f"no Markdown documents found in {KNOWLEDGE_BASE_DIR}"
        )

    return [(path, load_document(path)) for path in paths]


def section_of(header_metadata: dict) -> str | None:
    for key in ("h3", "h2", "h1"):
        if header_metadata.get(key):
            return header_metadata[key]
    return None


def build_context_header(metadata: dict, section: str | None) -> str:
    lines = [
        f"Document title: {metadata.get('title')}",
        f"Document type: {metadata.get('doc_type')}",
        f"Product: {metadata.get('product')}",
        f"Plan: {metadata.get('plan')}",
        f"Version: {metadata.get('version')}",
    ]
    if section:
        lines.append(f"Section: {section}")
    return "\n".join(lines)


def chunk_document(
    path: Path, post: frontmatter.Post, context_header: bool = True
) -> list[dict]:
    # First splitter: break the Markdown by headings, keeping each section's
    # heading path (h1/h2/h3) inside the resulting Document metadata.
    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=HEADERS_TO_SPLIT_ON)
    # Second splitter: cut any section that is still too long into smaller
    # overlapping pieces, so no chunk exceeds the size limit.
    character_splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )

    # Run both splitters in sequence: headings first, then size.
    sections = markdown_splitter.split_text(post.content)
    pieces = character_splitter.split_documents(sections)

    chunks = []
    for index, piece in enumerate(pieces):
        # Start from the document's front matter metadata and enrich each chunk
        # with its own identity (id/index), origin (source_file/section) and size.
        metadata = dict(post.metadata)
        metadata["chunk_id"] = f"{path.stem}-{index}"
        metadata["source_file"] = path.name
        metadata["chunk_index"] = index
        metadata["section"] = section_of(piece.metadata)

        # Prepend a short context header to the indexed text, so a chunk taken out
        # of the middle of a document still states which document, section, plan
        # and version it belongs to. This is what the embedding actually sees.
        content = piece.page_content
        if context_header:
            header = build_context_header(post.metadata, metadata["section"])
            content = f"{header}\n\n{content}"

        metadata["content_length"] = len(content)
        chunks.append({"content": content, "metadata": metadata})

    return chunks


def save_chunks(chunks: list[dict]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as output:
        for chunk in chunks:
            output.write(json.dumps(chunk, ensure_ascii=False) + "\n")


def print_sample(chunks: list[dict]) -> None:
    print("\nSample chunks:")
    for chunk in chunks[:5]:
        metadata = chunk["metadata"]
        print(
            f"- {metadata['chunk_id']} | {metadata['source_file']} | "
            f"{metadata['section']} | {metadata['content_length']} chars"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest and chunk the knowledge base.")
    parser.add_argument("--no-context-header", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    context_header = not args.no_context_header

    print("Loading documents from knowledge_base...")
    try:
        documents = load_documents()
    except IngestionError as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Loaded documents: {len(documents)}")

    print("Generating chunks...")
    print(f"Context header: {'enabled' if context_header else 'disabled'}")
    chunks = []
    for path, post in documents:
        chunks.extend(chunk_document(path, post, context_header=context_header))
    print(f"Generated chunks: {len(chunks)}")

    save_chunks(chunks)
    print(f"Chunks saved to {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
    print("Ingestion completed.")

    print_sample(chunks)


if __name__ == "__main__":
    main()
