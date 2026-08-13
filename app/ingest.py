import argparse
import json
import sys
from pathlib import Path

import frontmatter
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from pydantic import BaseModel

from app.manifest import document_hash
from app.provenance import (
    TRUSTED_SOURCE_ROOTS,
    ProvenanceDecision,
    classify_source,
    provenance_metadata,
    reserved_fields_declared,
    resolved_roots,
    trusted_roots_description,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Derived from the allowlist, never spelled again: the authorized source roots ARE the
# definition of where knowledge comes from, so the scanner and the policy cannot disagree.
KNOWLEDGE_BASE_DIR = TRUSTED_SOURCE_ROOTS[0]
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

# Metadata that gets prepended to the chunk text, so it reaches the embedding.
CONTEXT_HEADER_FIELDS = [
    ("Document title", "title"),
    ("Document type", "doc_type"),
    ("Product", "product"),
    ("Plan", "plan"),
    ("Version", "version"),
    ("Section", "section"),
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


class IngestionOutcome(BaseModel):
    """Why a document was accepted or refused — the two reasons kept apart.

    A file can parse perfectly and still be refused. "Structurally valid" is a statement
    about the file; "trusted" is a statement about where it came from. Collapsing them
    into one boolean would make a rejected poison document read as a malformed one, which
    is precisely the confusion this control exists to remove.
    """

    source_file: str
    structural_valid: bool
    structural_error: str | None = None
    provenance_trusted: bool
    provenance_source: str
    accepted: bool
    reason: str


def parse_document(path: Path) -> frontmatter.Post:
    """Structural validation only: does this file parse and carry the required fields?

    Says nothing about trust. Kept separate so the ingestion can report "valid document
    from an unauthorized source" instead of the misleading "invalid document".
    """
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise IngestionError(f"{path.name}: file is empty")

    post = frontmatter.loads(raw.strip())
    if not post.metadata:
        raise IngestionError(f"{path.name}: missing YAML front matter")

    missing = [key for key in REQUIRED_METADATA if key not in post.metadata]
    if missing:
        raise IngestionError(
            f"{path.name}: missing required metadata: {', '.join(missing)}"
        )

    if not post.content.strip():
        raise IngestionError(f"{path.name}: document body is empty")

    # Hash of the complete file (front matter included), so any edit marks
    # the document as changed for the incremental indexer. Integrity, not provenance:
    # it proves the file did not change after we read it, never where it came from.
    post.metadata["document_hash"] = document_hash(raw)
    return post


def ingest_document(path: Path) -> tuple[frontmatter.Post | None, IngestionOutcome]:
    """The single trust decision, used by production indexing and by the evaluations.

    Provenance is decided first and from the path alone, so an untrusted file can never
    be admitted no matter what its front matter claims. It is still parsed afterwards —
    only to label the outcome for the report, never to index it. Parsing is safe to do on
    untrusted input here: python-frontmatter reads YAML with SafeLoader.
    """
    decision = classify_source(path)

    try:
        post = parse_document(path)
        structural_valid, structural_error = True, None
    except IngestionError as exc:
        post, structural_valid, structural_error = None, False, str(exc)

    refusal = refusal_reason(
        decision, structural_valid, structural_error, post.metadata if post else {}
    )
    outcome = IngestionOutcome(
        source_file=decision.source_file,
        structural_valid=structural_valid,
        structural_error=structural_error,
        provenance_trusted=decision.trusted,
        provenance_source=decision.source,
        accepted=refusal is None,
        reason=refusal or f"trusted source — {decision.reason}",
    )
    if not outcome.accepted:
        return None, outcome

    post.metadata.update(provenance_metadata(decision))
    return post, outcome


def refusal_reason(
    decision: ProvenanceDecision,
    structural_valid: bool,
    structural_error: str | None,
    metadata: dict,
) -> str | None:
    """Why this document must not be ingested, or None when it may be."""
    # Provenance decides first: an untrusted source is refused whether or not it parsed,
    # and the outcome keeps both facts so "valid but unauthorized" stays visible.
    if not decision.trusted:
        return f"untrusted source — {decision.reason}"
    if not structural_valid:
        return f"structurally invalid — {structural_error}"

    # Several metadata fields are application-owned — document_hash, chunk_id, source_file
    # and the provenance trio. The first ones are simply overwritten because a document
    # setting them is confused, not dangerous. Provenance is fatal instead: it is the field
    # that grants trust, so a document reaching for it is the attempt this control exists
    # to catch, and refusing keeps the attempt visible.
    declared = reserved_fields_declared(metadata)
    if declared:
        return f"declares application-owned provenance field(s): {', '.join(declared)}"

    return None


def load_documents() -> list[tuple[Path, frontmatter.Post]]:
    """Every document the knowledge base publishes, after the trust decision.

    A refusal inside an authorized root is fatal rather than skipped: the knowledge base is
    curated, so a document that cannot be ingested means the index would be quietly
    incomplete. Failing loudly is the only reading that does not hide it — and it is the
    same policy on the CLI and in the evaluations, not one each.
    """
    accepted, rejected = load_documents_with_outcomes()
    if rejected:
        raise IngestionError(
            "documents refused by the trusted ingestion policy:\n"
            + "\n".join(f"  {o.source_file}: {o.reason}" for o in rejected)
        )
    return accepted


def load_documents_with_outcomes() -> tuple[
    list[tuple[Path, frontmatter.Post]], list[IngestionOutcome]
]:
    roots = resolved_roots()
    missing = [root for root in roots if not root.is_dir()]
    if missing:
        raise IngestionError(
            f"authorized source root not found: {', '.join(str(r) for r in missing)}"
        )

    paths = sorted(path for root in roots for path in root.glob("*.md"))
    if not paths:
        raise IngestionError(
            f"no Markdown documents found in {', '.join(str(r) for r in roots)}"
        )

    accepted: list[tuple[Path, frontmatter.Post]] = []
    rejected: list[IngestionOutcome] = []
    for path in paths:
        post, outcome = ingest_document(path)
        if post is None:
            rejected.append(outcome)
        else:
            accepted.append((path, post))
    return accepted, rejected


def section_of(header_metadata: dict) -> str | None:
    for key in ("h3", "h2", "h1"):
        if header_metadata.get(key):
            return header_metadata[key]
    return None


def build_context_header(metadata: dict) -> str:
    return "\n".join(
        f"{label}: {metadata[key]}"
        for label, key in CONTEXT_HEADER_FIELDS
        if metadata.get(key)
    )


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
        # Deterministic id: same file + same content + same position = same id,
        # so reindexing never duplicates and a changed document gets new ids.
        metadata["chunk_id"] = (
            f"{path.stem}-{metadata['document_hash'][:12]}-{index:04d}"
        )
        metadata["source_file"] = path.name
        metadata["chunk_index"] = index
        metadata["section"] = section_of(piece.metadata)

        # Metadata alone never reaches the embedding, so the header is prepended to
        # the text itself: a chunk from the middle of a document stays self-explanatory.
        content = piece.page_content
        if context_header:
            content = f"{build_context_header(metadata)}\n\n{content}"

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
    print(f"Trusted source roots: {', '.join(trusted_roots_description())}")
    try:
        documents = load_documents()
    except IngestionError as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Accepted documents: {len(documents)}")

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
