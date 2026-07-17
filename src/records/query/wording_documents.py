"""Read-only policy-wording document resolution.

Policy wording is reference text rather than a domain event, so the current
single-policy resolution rule reads document metadata: select the most
recently ingested ``policy_wording`` document that has not been superseded.

This helper is shared by the CLI coverage route and the MCP evidence tool.
Keeping it here prevents the read-only MCP boundary from importing the
LLM-backed query router.

Known gap (ADR 0002): resolution will eventually follow question -> policy
entity -> governing wording for that policy and period. Until that association
exists, this preserves the established latest-non-superseded rule exactly.
"""

from __future__ import annotations

from pathlib import Path

from records.store import list_documents


def current_wording_document(*, root: Path | None = None) -> dict | None:
    """Return the latest non-superseded policy wording, if one is on file."""
    candidates = [
        document
        for document in list_documents(root=root)
        if document.get("doc_type") == "policy_wording"
        and document.get("superseded_by") is None
    ]
    return (
        max(candidates, key=lambda document: document["ingested_at"])
        if candidates
        else None
    )
