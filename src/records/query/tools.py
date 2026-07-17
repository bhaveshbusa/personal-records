"""Deterministic query tools — the only things that answer questions.

Pure functions over replayed events (and the review queue for gap
questions). Every answer carries the evidence doc_id(s) it came from.
The LLM upstream (router.py) only classifies intent; it never sees the
event log and never composes facts — "the LLM is never the database and
never the calculator."

Ported essentials of the old repo's calculators/query tools, rebuilt on
the typed event model. The MCP server (Phase 4) wraps these same
functions.
"""

from __future__ import annotations

from datetime import date

from records.core import (
    DomainEvent,
    RenewalAccepted,
    RenewalProposed,
    renewal_calendar,
    renewal_offers,
)


def _rows(events: list[DomainEvent], product: str | None, today: date | None) -> list[dict]:
    calendar = renewal_calendar(events, today=today)
    if product:
        calendar = [r for r in calendar if r["product"] == product]
    return calendar


def renewal_answer(
    events: list[DomainEvent], product: str | None = None, today: date | None = None
) -> dict:
    """When does <product> (or everything) renew?"""
    rows = _rows(events, product, today)
    return {"found": bool(rows), "rows": rows, "sources": [r["doc_id"] for r in rows]}


def premium_answer(
    events: list[DomainEvent], product: str | None = None, today: date | None = None
) -> dict:
    """What does <product> cost? Latest state per product."""
    rows = _rows(events, product, today)
    return {
        "found": bool(rows),
        "premiums": [
            {
                "product": r["product"],
                "annual_premium": r["annual_premium"],
                "state": r["state"],
                "doc_id": r["doc_id"],
            }
            for r in rows
        ],
        "sources": [r["doc_id"] for r in rows],
    }


def premium_history_answer(events: list[DomainEvent], product: str | None = None) -> dict:
    """How has <product>'s premium changed? Chronological deltas from the
    full event history (this is why the log keeps every event)."""
    history: dict[str, list] = {}
    for event in events:
        if isinstance(event, (RenewalProposed, RenewalAccepted)):
            history.setdefault(event.product, []).append(event)
    if product:
        history = {p: evts for p, evts in history.items() if p == product}

    changes = []
    for prod, evts in history.items():
        steps = []
        for prev, curr in zip(evts, evts[1:]):
            delta = (
                (curr.annual_premium - prev.annual_premium) / prev.annual_premium
                if prev.annual_premium
                else None
            )
            steps.append(
                {
                    "from": prev.annual_premium,
                    "to": curr.annual_premium,
                    "change_pct": round(delta * 100, 1) if delta is not None else None,
                    "sources": [prev.doc_id, curr.doc_id],
                }
            )
        changes.append(
            {
                "product": prod,
                "observations": len(evts),
                "latest_premium": evts[-1].annual_premium,
                "steps": steps,
                "sources": [e.doc_id for e in evts],
            }
        )
    return {"found": bool(changes), "products": changes}


def quote_comparison_answer(events: list[DomainEvent], product: str | None = None) -> dict:
    """How does my renewal quote compare to my current policy? (2R.5 — the
    old repo's COMPARISON route.) The maths is `renewal_offers`' pairing
    (offer ↔ current policy via entity link, delta + pct); this tool only
    filters and shapes it for the router. The old `calculators.premium_change`
    survives as that projection's delta; `days_until_expiry` as the
    calendar's days_left — deterministic code either way, the LLM never
    calculates."""
    offers = renewal_offers(events)
    if product:
        offers = [o for o in offers if o["product"] == product]
    sources = []
    for offer in offers:
        sources.append(offer["doc_id"])
        if offer["current_policy_doc_id"]:
            sources.append(offer["current_policy_doc_id"])
    return {"found": bool(offers), "offers": offers, "sources": sources}


def missing_info_answer(
    events: list[DomainEvent], pending_review: list[dict], today: date | None = None
) -> dict:
    """What's incomplete? Products without renewal dates, documents stuck in
    review, or an empty record set."""
    rows = renewal_calendar(events, today=today)
    gaps = [
        {
            "product": r["product"],
            "gap": "no renewal date on record",
            "doc_id": r["doc_id"],
            "trust": r["trust"],
        }
        for r in rows
        if r["renewal_date"] is None
    ]
    stuck = [
        {"doc_id": item["doc_id"], "reasons": item["reasons"], "queued_at": item["queued_at"]}
        for item in pending_review
    ]
    return {
        "empty": not rows and not stuck,
        "gaps": gaps,
        "pending_review": stuck,
    }
