"""Trusted ingestion: which sources are authorized to supply knowledge to this pipeline.

The RAG poisoning baseline showed a document being accepted, indexed, retrieved and used
as a source for an answer — on the strength of nothing but its own front matter. It said
`tenant: fcai` and `status: published`, and those lines were treated as if they proved
something. They do not: they are content, written by whoever wrote the file.

So this module separates two things the project was conflating:

    document metadata    what the file says about itself     — never a trust signal
    provenance metadata  what the application knows about
                         where the file actually came from   — assigned here, by us

The policy is deliberately positive: a small allowlist of source roots the application
controls. Everything outside is untrusted. It is *not* a list of known-bad names or
patterns — an attacker picks the filename, so blocking known-bad only blocks the payloads
we already thought of.

What this answers: "is this source authorized to supply knowledge to this pipeline?"
What it does NOT answer: "is this content safe?". An authorized source can still be
compromised, can still carry text copied from somewhere else, and can still contain an
instruction aimed at the model. That is a different control and a different lesson.
"""

from pathlib import Path

from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The source roots this application publishes knowledge from. Application-controlled, in
# code, reviewed like code — not configurable by a document and not read from the
# environment, because an env var an attacker can set is not an authorization boundary.
TRUSTED_SOURCE_ROOTS = (PROJECT_ROOT / "knowledge_base",)

POLICY_NAME = "trusted-source-v1"

# Metadata the application owns. A document that tries to set any of these is refused
# rather than silently corrected: a legitimate document has no reason to declare its own
# provenance, so the attempt is itself the signal. Overwriting quietly would work too, but
# it would hide the attempt — and the whole point of this lesson is that the attempt is
# visible.
RESERVED_FIELDS = ("provenance_trusted", "provenance_source", "provenance_policy")


class ProvenanceDecision(BaseModel):
    """Where a file really came from, decided by resolved path, never by its contents."""

    source_file: str
    trusted: bool
    # Always the directory the file really resolved to, whether authorized or not, so the
    # field means one thing and reports can group by it.
    source: str
    reason: str


def display_path(path: Path) -> str:
    # Project-relative when it is inside the project, absolute otherwise: a root pointed
    # somewhere else entirely still has to print, not raise.
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def resolved_roots() -> list[Path]:
    # Reads the module constant at call time rather than binding it as a default argument:
    # a default is frozen at import, which would make the policy impossible to exercise
    # against an alternate root in a test — and an untested policy is not a control.
    return [root.resolve() for root in TRUSTED_SOURCE_ROOTS]


def classify_source(path: Path) -> ProvenanceDecision:
    """Decide whether a path belongs to an authorized source root.

    resolve() first, then containment on the resolved paths. That ordering is the control:
    it collapses `..` segments and follows symlinks, so a link sitting inside a trusted
    root that points at a file outside it resolves outside and is rejected. Containment is
    is_relative_to on real path components, not a string prefix — otherwise a sibling
    directory named `knowledge_base_evil` would pass for being spelled like the root.
    """
    resolved = path.resolve()
    where = display_path(resolved.parent)
    for root in resolved_roots():
        if resolved.is_relative_to(root):
            return ProvenanceDecision(
                source_file=path.name,
                trusted=True,
                source=where,
                reason=f"inside the authorized source root {display_path(root)}",
            )

    return ProvenanceDecision(
        source_file=path.name,
        trusted=False,
        source=where,
        reason=f"{where} is not an authorized source root",
    )


def reserved_fields_declared(metadata: dict) -> list[str]:
    return [field for field in RESERVED_FIELDS if field in metadata]


def provenance_metadata(decision: ProvenanceDecision) -> dict:
    # Only ever built for a trusted decision; the untrusted branch never reaches indexing,
    # so there is no such thing as a stored chunk with provenance_trusted=false.
    return {
        "provenance_trusted": True,
        "provenance_source": decision.source,
        "provenance_policy": POLICY_NAME,
    }


def trusted_roots_description() -> list[str]:
    return [display_path(root) for root in resolved_roots()]
