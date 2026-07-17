"""Human-review queue — where the trust gate parks what it won't accept.

Same append-only JSONL + latest-per-key fold as the document store: an item
is (re)written on every state change; the latest version per doc_id wins.
States: pending → confirmed | rejected.

Confirming is the human overriding the gate: a renewal extraction's lines
become events (RenewalAccepted for an already-accepted shape,
RenewalProposed otherwise); a flat-field extraction becomes the event
matching its doc_type (PolicyFiled, NcdConfirmed, or deliberately none for
proof artifacts — 2R.2 dispatch). Rejecting parks the document as
evidence-only — no events, the document stays in the store.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from records.config import data_dir
from records.core import (
    REGISTRY,
    RENEWAL_ALREADY_ACCEPTED,
    TRUST_VERIFIED,
    Extraction,
    FieldExtraction,
    RenewalAccepted,
    RenewalProposed,
    event_for_fields,
    extraction_from_dict,
    field_extraction_from_dict,
    has_event_vocabulary,
    known_entities,
    link_document,
)

PENDING = "pending"
CONFIRMED = "confirmed"
REJECTED = "rejected"


def _queue_path(root: Path | None) -> Path:
    return (root or data_dir()) / "review_queue.jsonl"


def _append(item: dict, root: Path | None) -> dict:
    path = _queue_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(item, default=str) + "\n")
    return item


def _fold(root: Path | None) -> dict[str, dict]:
    path = _queue_path(root)
    if not path.exists():
        return {}
    latest: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            latest[item["doc_id"]] = item
    return latest


def add(
    doc_id: str,
    reasons: tuple[str, ...] | list[str],
    extraction: Extraction | FieldExtraction | None,
    *,
    root: Path | None = None,
) -> dict:
    """Park one document for human review. extraction may be a renewal
    Extraction (lines), a FieldExtraction (flat canonical fields), or None
    when the LLM response was unparseable — the reviewer still sees why."""
    return _append(
        {
            "doc_id": doc_id,
            "status": PENDING,
            "reasons": list(reasons),
            "extraction": asdict(extraction) if extraction is not None else None,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "resolved_at": None,
        },
        root,
    )


def list_pending(*, root: Path | None = None) -> list[dict]:
    return [i for i in _fold(root).values() if i["status"] == PENDING]


def get(doc_id: str, *, root: Path | None = None) -> dict | None:
    return _fold(root).get(doc_id)


def confirm(doc_id: str, *, root: Path | None = None, events: list | None = None) -> list:
    """Human confirms the extraction as-is. Returns the domain events to
    append (the caller owns the event log — this module never writes it).

    `events` is the replayed log for entity linking (2R.3): when given, the
    confirmed item links to the policy entity its identifiers match.
    Confirming is a human override, so an ambiguous or missing link never
    blocks here — it just leaves the event unlinked (or, for doc types that
    mint/fall back, on the dispatch fallback entity)."""
    item = _fold(root).get(doc_id)
    if item is None or item["status"] != PENDING:
        raise KeyError(f"no pending review item for doc_id: {doc_id}")
    if item["extraction"] is None:
        raise ValueError(f"{doc_id} has no extraction to confirm — re-ingest or reject it")
    if "shape" not in item["extraction"]:
        # A FieldExtraction (non-quote doc_type): confirming emits the
        # domain event matching the doc_type (2R.2 dispatch). Confirming is
        # the human overriding the gate, so the overwrite guard does not
        # re-fire here — the conflict was already surfaced as the review
        # reason that parked the item.
        extraction = field_extraction_from_dict(item["extraction"])
        schema = REGISTRY.get(extraction.doc_type)
        if schema is None or not has_event_vocabulary(extraction.doc_type):
            raise ValueError(
                f"{doc_id} is a {extraction.doc_type} — no event vocabulary for "
                "this doc_type yet; reject it or leave it pending"
            )
        entity_id = None
        if events is not None:
            link = link_document(
                extraction.fields, known_entities(events), doc_type=extraction.doc_type
            )
            entity_id = link.entity_id if link.linked else None
        # Human confirmed: the fact enters the log as verified (2R.5 trust).
        event = event_for_fields(extraction, schema, entity_id=entity_id, trust=TRUST_VERIFIED)
        _append(
            {**item, "status": CONFIRMED, "resolved_at": datetime.now(timezone.utc).isoformat()},
            root,
        )
        return [event] if event is not None else []

    extraction = extraction_from_dict(item["extraction"])
    accepted = extraction.shape.renewal_status == RENEWAL_ALREADY_ACCEPTED
    entity_id = None
    if events is not None and extraction.identifiers:
        link = link_document(
            extraction.identifiers, known_entities(events), doc_type="renewal_quote"
        )
        entity_id = link.entity_id if link.linked else None
    emitted = []
    for line in extraction.lines:
        if line.annual_premium is None:
            continue  # a line the extractor couldn't price emits no fact
        premium = float(line.annual_premium.value)
        renewal_date = str(line.renewal_date.value) if line.renewal_date else None
        if accepted:
            emitted.append(
                RenewalAccepted(
                    extraction.doc_id, line.product, premium, renewal_date, trust=TRUST_VERIFIED
                )
            )
        else:
            emitted.append(
                RenewalProposed(
                    doc_id=extraction.doc_id,
                    product=line.product,
                    annual_premium=premium,
                    provenance=line.annual_premium,
                    renewal_date=renewal_date,
                    entity_id=entity_id,
                    trust=TRUST_VERIFIED,
                )
            )
    _append({**item, "status": CONFIRMED, "resolved_at": datetime.now(timezone.utc).isoformat()}, root)
    return emitted


def reject(doc_id: str, *, root: Path | None = None) -> dict:
    """Human rejects the extraction: no events, document remains as evidence."""
    item = _fold(root).get(doc_id)
    if item is None or item["status"] != PENDING:
        raise KeyError(f"no pending review item for doc_id: {doc_id}")
    return _append(
        {**item, "status": REJECTED, "resolved_at": datetime.now(timezone.utc).isoformat()}, root
    )
