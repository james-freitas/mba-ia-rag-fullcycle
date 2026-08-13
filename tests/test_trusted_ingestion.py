"""Tests for the trusted ingestion policy: which sources may supply knowledge.

Filesystem only — no model, no Langfuse, no Postgres. The policy is a decision about
paths, so these drive it with real files in tmp_path and with the repository's own
knowledge base and poison fixtures.

The property being pinned down is that trust comes from where a file *is*, never from what
it *says*: every poison fixture here is a structurally perfect document that declares
`tenant: fcai` and `status: published`, and every one of them must still be refused.
"""

import json

import pytest

from app import index, provenance
from app.index import IndexingError, load_chunks
from app.ingest import (
    KNOWLEDGE_BASE_DIR,
    chunk_document,
    ingest_document,
    load_documents,
    parse_document,
)

FIXTURES_DIR = provenance.PROJECT_ROOT / "evals" / "security_rag_fixtures"
POISON = FIXTURES_DIR / "security-poison-priority-shield.md"
TRUSTED = KNOWLEDGE_BASE_DIR / "product-support-sla.md"

VALID_DOCUMENT = """---
title: Documento de Teste
tenant: fcai
product: fcai-cloud
plan: all
doc_type: policy
version: 2026-01
status: published
visibility: internal
---

# Documento de Teste

Conteúdo suficiente para passar na validação estrutural.
"""


def write(path, body: str = VALID_DOCUMENT):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def trusted_root(tmp_path, monkeypatch):
    """An authorized source root somewhere else, so the policy itself can be exercised."""
    root = tmp_path / "knowledge_base"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(provenance, "TRUSTED_SOURCE_ROOTS", (root,))
    return root


# --- the two questions, kept apart --------------------------------------------------


def test_document_inside_a_trusted_root_is_accepted():
    post, outcome = ingest_document(TRUSTED)
    assert post is not None
    assert outcome.structural_valid and outcome.provenance_trusted and outcome.accepted


def test_valid_document_outside_a_trusted_root_is_rejected():
    post, outcome = ingest_document(POISON)
    # The point of the whole lesson: valid, and refused anyway.
    assert outcome.structural_valid is True
    assert outcome.provenance_trusted is False
    assert outcome.accepted is False
    assert post is None
    assert "untrusted source" in outcome.reason


@pytest.mark.parametrize("field, value", [("status", "published"), ("tenant", "fcai")])
def test_front_matter_claims_do_not_grant_trust(field, value):
    # The document really does say it, and it really is refused anyway.
    assert parse_document(POISON).metadata[field] == value
    assert ingest_document(POISON)[1].accepted is False


def test_every_poison_fixture_is_valid_and_still_refused():
    fixtures = sorted(FIXTURES_DIR.glob("*.md"))
    assert fixtures, "the poison fixtures must still exist, untouched"
    for path in fixtures:
        _, outcome = ingest_document(path)
        assert outcome.structural_valid is True, path.name
        assert outcome.accepted is False, path.name


# --- provenance metadata is application-owned ---------------------------------------


def test_front_matter_cannot_declare_itself_trusted(trusted_root):
    liar = write(
        trusted_root / "liar.md",
        VALID_DOCUMENT.replace(
            "visibility: internal",
            "visibility: internal\nprovenance_trusted: true\nprovenance_source: knowledge_base",
        ),
    )

    post, outcome = ingest_document(liar)
    # Refused rather than silently corrected, so the attempt stays visible.
    assert post is None
    assert outcome.provenance_trusted is True  # it IS in a trusted root
    assert outcome.accepted is False
    assert "provenance" in outcome.reason


def test_provenance_metadata_is_assigned_by_the_application():
    post, _ = ingest_document(TRUSTED)
    assert post.metadata["provenance_trusted"] is True
    assert post.metadata["provenance_source"] == KNOWLEDGE_BASE_DIR.name
    assert post.metadata["provenance_policy"] == provenance.POLICY_NAME
    # And it reaches every chunk, which is what the indexer checks.
    for chunk in chunk_document(TRUSTED, post):
        assert chunk["metadata"]["provenance_trusted"] is True


# --- path resolution ----------------------------------------------------------------


def test_dotdot_does_not_escape_the_policy(tmp_path, trusted_root):
    outside = write(tmp_path / "outside" / "poison.md")
    traversed = trusted_root / ".." / "outside" / "poison.md"
    assert traversed.exists()
    assert provenance.classify_source(traversed).trusted is False
    assert provenance.classify_source(outside).trusted is False


def test_symlink_out_of_a_trusted_root_is_not_trusted(tmp_path, trusted_root):
    outside = write(tmp_path / "outside" / "poison.md")
    link = trusted_root / "looks-legit.md"
    link.symlink_to(outside)

    # The link lives inside the authorized root; the file it points at does not.
    assert link.exists() and link.is_symlink()
    assert provenance.classify_source(link).trusted is False
    assert ingest_document(link)[1].accepted is False


def test_sibling_directory_with_a_prefix_name_is_not_trusted(tmp_path, trusted_root):
    evil = write(tmp_path / "knowledge_base_evil" / "poison.md")

    # A string prefix check would accept this path. Real containment must not.
    assert str(evil).startswith(str(trusted_root))
    assert provenance.classify_source(evil).trusted is False


# --- nothing rejected can reach the index -------------------------------------------


def test_no_rejected_document_reaches_the_chunks():
    # Nothing refused can produce a chunk, so no poison filename can appear in what the
    # production ingestion hands to the indexer.
    produced = {
        chunk["metadata"]["source_file"]
        for path, post in load_documents()
        for chunk in chunk_document(path, post)
    }
    assert produced
    assert not any(name.startswith("security-poison-") for name in produced)


def test_indexer_refuses_a_chunks_file_written_before_the_policy(tmp_path, monkeypatch):
    chunks = tmp_path / "chunks.jsonl"
    record = {
        "content": "conteúdo",
        "metadata": {
            "chunk_id": "x-0000",
            "source_file": "product-support-sla.md",
            "document_hash": "abc123",
        },
    }
    chunks.write_text(json.dumps(record) + "\n", encoding="utf-8")
    monkeypatch.setattr(index, "CHUNKS_PATH", chunks)

    # This catches a stale chunks file, which is the likely accident. It is NOT an
    # authorization boundary: the flag is data in a local file, so whoever can write the
    # file can write the flag. The real decision happens on the path, before this file.
    with pytest.raises(IndexingError, match="trusted provenance"):
        load_chunks()


# --- production path uses the policy ------------------------------------------------


def test_production_ingestion_applies_the_policy_by_default():
    documents = load_documents()
    assert documents, "the knowledge base must still ingest"
    for path, post in documents:
        assert post.metadata["provenance_trusted"] is True
        assert post.metadata["provenance_policy"] == provenance.POLICY_NAME
        assert provenance.classify_source(path).trusted is True


def test_trusted_roots_are_application_controlled():
    # In code, not in the environment and not in a document.
    assert provenance.trusted_roots_description() == ["knowledge_base"]
    # The scanner and the policy must name the same place, or the knowledge base would be
    # globbed from one directory and authorized from another.
    assert KNOWLEDGE_BASE_DIR in provenance.TRUSTED_SOURCE_ROOTS


def test_reserved_fields_match_what_the_application_assigns():
    # The fields a document may not declare are exactly the fields the app writes. If the
    # two lists drifted, a document could declare one the rejection does not cover.
    decision = provenance.classify_source(TRUSTED)
    assert set(provenance.provenance_metadata(decision)) == set(provenance.RESERVED_FIELDS)
