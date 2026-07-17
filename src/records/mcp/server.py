"""Read-only Model Context Protocol boundary.

The MCP host replaces the CLI's LLM intent classifier: it chooses a named
tool, while the tool reads deterministic projections from the local event
log. This module deliberately has no pipeline, review-mutation, event-append,
or document-update imports. Its one review dependency is the read-only
``list_pending`` query used to report records awaiting human attention.
Assistant-driven writes are a different trust model and are out of scope.

Raw document bytes and local storage paths are not exposed. Provenance returns
allowlisted document metadata plus the typed domain events supported by that
document; field-level evidence snippets may appear inside those events.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from records.core import current_policies, replay
from records.query.tools import (
    missing_info_answer,
    quote_comparison_answer,
    renewal_answer,
)
from records.query.wording_documents import current_wording_document
from records.query.wording_index import search
from records.review.queue import list_pending
from records.store import get_document

SERVER_NAME = "personal-records"
SERVER_INSTRUCTIONS = (
    "Read-only access to the user's local personal-record projections, quote comparisons, "
    "review gaps, policy-wording evidence, and provenance. Use list_records to discover canonical entity_id values; "
    "get_record accepts one of those exact entity_id values. Use get_provenance with a "
    "returned doc_id when the user asks where a fact came from. For coverage questions, "
    "use search_policy_wording and interpret only the returned clauses; its evidence is not "
    "a coverage verdict. No tool can ingest, "
    "confirm, reject, discard, or otherwise modify records."
)

_SAFE_DOCUMENT_FIELDS = (
    "doc_id",
    "file_name",
    "media_type",
    "ingested_at",
    "doc_type",
    "chunk_count",
    "superseded_by",
)


def _record_row(row: dict[str, Any]) -> dict[str, Any]:
    """Make one ``current_policies`` row explicitly JSON-shaped."""
    return {
        **row,
        "fields": {name: asdict(field) for name, field in row["fields"].items()},
    }


def list_records(*, root: Path | None = None) -> dict[str, Any]:
    """Return every current policy record with evidence and trust."""
    records = [_record_row(row) for row in current_policies(replay(root=root))]
    return {
        "found": bool(records),
        "records": records,
        "sources": [record["doc_id"] for record in records],
    }


def get_record(entity_id: str, *, root: Path | None = None) -> dict[str, Any]:
    """Return one current policy by its exact canonical ``entity_id``.

    ``entity_id`` is deliberately the only lookup identifier: it is the key
    of the projection and is returned by ``list_records``. Document evidence
    IDs remain a separate provenance concept.
    """
    identifier = entity_id.strip()
    record = next(
        (
            _record_row(row)
            for row in current_policies(replay(root=root))
            if row["entity_id"] == identifier
        ),
        None,
    )
    return {
        "found": record is not None,
        "identifier_type": "entity_id",
        "entity_id": identifier,
        "record": record,
        "sources": [record["doc_id"]] if record is not None else [],
    }


def get_renewals(
    product: str | None = None, *, root: Path | None = None
) -> dict[str, Any]:
    """Return active renewal records, optionally filtered by product.

    Every row includes its complete evidence document ID and trust level.
    Calculations and discard handling come from the deterministic renewal
    projection; this tool performs no LLM call.
    """
    normalized = product.strip().lower() if product and product.strip() else None
    return renewal_answer(replay(root=root), normalized)


def compare_quotes(
    product: str | None = None, *, root: Path | None = None
) -> dict[str, Any]:
    """Compare renewal offers with current policies deterministically."""
    normalized = product.strip().lower() if product and product.strip() else None
    return quote_comparison_answer(replay(root=root), normalized)


def find_missing_info(*, root: Path | None = None) -> dict[str, Any]:
    """Return missing renewal facts and documents awaiting human review."""
    result = missing_info_answer(replay(root=root), list_pending(root=root))
    gaps = result["gaps"]
    pending = result["pending_review"]
    sources = [item["doc_id"] for item in gaps]
    sources.extend(item["doc_id"] for item in pending)
    return {
        "found": bool(gaps or pending),
        "record_set_empty": result["empty"],
        "gaps": gaps,
        "pending_review": pending,
        "sources": list(dict.fromkeys(sources)),
    }


def get_provenance(doc_id: str, *, root: Path | None = None) -> dict[str, Any]:
    """Return safe metadata and supporting domain events for one document.

    Raw document content and its local ``storage_path`` are intentionally
    excluded. ``found`` is false when neither document metadata nor a domain
    event exists for the supplied ID.
    """
    document = get_document(doc_id, root=root)
    metadata = (
        {name: document[name] for name in _SAFE_DOCUMENT_FIELDS if name in document}
        if document is not None
        else None
    )
    supporting_events = [
        {"event_type": type(event).__name__, "data": asdict(event)}
        for event in replay(root=root)
        if event.doc_id == doc_id
    ]
    return {
        "found": metadata is not None or bool(supporting_events),
        "doc_id": doc_id,
        "document": metadata,
        "events": supporting_events,
    }


def search_policy_wording(
    question: str, *, root: Path | None = None
) -> dict[str, Any]:
    """Retrieve relevant clauses from the current policy wording.

    Selection, BM25 ranking, and substantive-term filtering are deterministic
    and read-only. The result is evidence for the hosting assistant to
    interpret, never a coverage verdict. Retrieval retains the wording
    index's maximum of four chunks.
    """
    wording_document = current_wording_document(root=root)
    if wording_document is None:
        return {
            "found": False,
            "reason": "no_wording_on_file",
            "clauses": [],
            "sources": [],
        }

    doc_id = wording_document["doc_id"]
    matches = search(question, doc_id, root=root, strict=True)
    if not matches:
        return {
            "found": False,
            "reason": "no_relevant_clause",
            "clauses": [],
            "sources": [doc_id],
        }

    clauses = [
        {
            "doc_id": chunk["doc_id"],
            "chunk_id": chunk["chunk_id"],
            "section_ref": chunk.get("section_ref"),
            "heading": chunk.get("heading"),
            "page": chunk["page"],
            "clause_text": chunk["text"],
            "score": score,
        }
        for chunk, score in matches
    ]
    return {
        "found": True,
        "reason": None,
        "clauses": clauses,
        "sources": [doc_id],
    }


def create_server(*, root: Path | None = None) -> FastMCP:
    """Build a server bound to ``root`` (default: PERSONAL_RECORDS_HOME).

    The root parameter exists so protocol tests can use a throwaway local
    store without patching globals; production calls leave it unset.
    """
    server = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @server.tool(name="list_records", structured_output=True)
    def list_records_tool() -> dict[str, Any]:
        """List current policy records with canonical entity IDs and evidence.

        An empty projection returns ``found: false`` with empty arrays.
        """
        return list_records(root=root)

    @server.tool(name="get_record", structured_output=True)
    def get_record_tool(entity_id: str) -> dict[str, Any]:
        """Get one current policy using an exact entity_id from list_records.

        Unknown identifiers return ``found: false`` and ``record: null``.
        """
        return get_record(entity_id, root=root)

    @server.tool(name="get_renewals", structured_output=True)
    def renewals_tool(product: str | None = None) -> dict[str, Any]:
        """Get active renewal dates, premiums, status, trust and evidence IDs.

        Optionally filter by a lowercase product such as ``motor`` or ``home``.
        """
        return get_renewals(product, root=root)

    @server.tool(name="compare_quotes", structured_output=True)
    def compare_quotes_tool(product: str | None = None) -> dict[str, Any]:
        """Compare renewal offers with current policies, optionally by product.

        Returns deterministic premium deltas, both evidence IDs, and the
        comparison's minimum trust. No offers returns ``found: false``.
        """
        return compare_quotes(product, root=root)

    @server.tool(name="find_missing_info", structured_output=True)
    def find_missing_info_tool() -> dict[str, Any]:
        """Find missing renewal dates and documents awaiting human review.

        ``found`` reports whether any gap exists; ``record_set_empty``
        distinguishes a brand-new store from a complete current record set.
        """
        return find_missing_info(root=root)

    @server.tool(name="get_provenance", structured_output=True)
    def provenance_tool(doc_id: str) -> dict[str, Any]:
        """Explain which local document and domain events support a fact.

        Supply the complete ``doc_id`` returned by another tool. Raw document
        content and local filesystem paths are never returned.
        """
        return get_provenance(doc_id, root=root)

    @server.tool(name="search_policy_wording", structured_output=True)
    def search_policy_wording_tool(question: str) -> dict[str, Any]:
        """Find relevant clauses in the current policy wording.

        Returns up to four exact clauses with complete citation metadata and
        BM25 scores. Interpret only those clauses; this tool gives evidence,
        not a coverage verdict. Missing evidence fails closed explicitly.
        """
        return search_policy_wording(question, root=root)

    return server


def run() -> None:
    """Run the read-only MCP server over stdio."""
    create_server().run(transport="stdio")
