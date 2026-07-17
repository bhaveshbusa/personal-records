"""Quote-ready profile — deterministic fold of events + documents into a
quote-form-shaped view, with per-entry trust and provenance (ported from the
old repo's `quote_profile.py`, 2R.5).

"Ready to re-quote" means having every answer a comparison site asks for in
one place, each one traceable: value, trust (the core/trust.py ladder),
source document, as-of date. Pure functions — callers provide the replayed
events and the document list; exports write under the data directory.

Ported onto the new model: the old per-field trust dicts became event-level
`trust` (see core/trust.py); the old `occurred_at` became the policy's
`valid_from` / the store's `ingested_at` (business time where we have it).
Claims counting is by `claim_evidence` documents on file — raw_evidence
documents never link to entities (no Link stage), so the old
linked-entities filter is dropped until reference/evidence linking lands
(same future work as ADR 0002)."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from records.config import data_dir
from records.core import (
    TRUST_EXTRACTED,
    TRUST_VERIFIED,
    DomainEvent,
    NcdConfirmed,
    PolicyCorrected,
    PolicyFiled,
    RenewalProposed,
    current_policies,
    min_trust,
)


def _entry(value=None, trust=TRUST_EXTRACTED, source_doc_id=None, as_of=None, **extra) -> dict:
    return {"value": value, "trust": trust, "source_doc_id": source_doc_id, "as_of": as_of, **extra}


def _selected_policy(events: list[DomainEvent]) -> dict | None:
    """The current-policy row the profile describes: the entity whose state
    was filed/corrected most recently (log order) and survives discards.
    Single-policy default, like the old repo's `_latest` fold."""
    rows = {row["entity_id"]: row for row in current_policies(events)}
    if not rows:
        return None
    last_entity = None
    for event in events:
        if isinstance(event, (PolicyFiled, PolicyCorrected)) and event.entity_id in rows:
            last_entity = event.entity_id
    return rows.get(last_entity) or next(iter(rows.values()))


def _within_five_years(value, today: date | None = None) -> bool:
    if not value:
        return True  # unknown date: count it (conservative — worst for the user to forget)
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        return True
    today = today or date.today()
    try:
        cutoff = today.replace(year=today.year - 5)
    except ValueError:  # Feb 29
        cutoff = today.replace(year=today.year - 5, day=28)
    return parsed >= cutoff


def quote_profile(
    events: list[DomainEvent], documents: list[dict], today: date | None = None
) -> dict:
    """Fold events and stored documents into a quote-form-shaped profile.
    No store access and no LLM calls: callers provide both collections."""
    policy = _selected_policy(events)
    fields = policy["fields"] if policy else {}
    policy_trust = policy["trust"] if policy else TRUST_EXTRACTED
    as_of = policy["valid_from"] if policy else None

    def policy_field(*names) -> dict:
        for name in names:
            f = fields.get(name)
            if f is not None:
                return _entry(f.value, policy_trust, policy["doc_id"], as_of)
        return _entry()

    # NCD: a confirmed NCD letter for the selected entity beats the schedule's
    # own ncd_years (old precedence kept); unlinked NCD letters count when no
    # policy is selected at all.
    ncd_events = [
        e
        for e in events
        if isinstance(e, NcdConfirmed)
        and (policy is None or e.entity_id in (policy["entity_id"], e.doc_id))
    ]
    ncd_event = next(
        (e for e in reversed(ncd_events) if e.fields.get("ncd_years") is not None), None
    )
    if ncd_event is not None:
        f = ncd_event.fields["ncd_years"]
        ncd = _entry(f.value, ncd_event.trust, ncd_event.doc_id, source_kind="NcdConfirmed")
    else:
        ncd = policy_field("ncd_years")
        ncd["source_kind"] = "PolicyFiled" if ncd["value"] is not None else None

    # Claims in the last 5 years: claim_evidence documents on file.
    claims = [
        doc
        for doc in documents
        if doc.get("doc_type") == "claim_evidence"
        and _within_five_years(doc.get("ingested_at"), today)
    ]
    claim_dates = sorted(str(d.get("ingested_at"))[:10] for d in claims if d.get("ingested_at"))

    # Premium history: filed/corrected premiums + quoted premiums for the
    # selected entity (or everything when nothing links), in log order.
    history = []
    for event in events:
        if isinstance(event, (PolicyFiled, PolicyCorrected)):
            premium = event.fields.get("annual_premium")
            if premium is not None and (policy is None or event.entity_id == policy["entity_id"]):
                history.append(
                    _entry(premium.value, event.trust, event.doc_id, event.valid_from, kind=type(event).__name__)
                )
        elif isinstance(event, RenewalProposed):
            if policy is None or event.entity_id in (None, policy["entity_id"]):
                history.append(
                    _entry(event.annual_premium, event.trust, event.doc_id, event.renewal_date, kind="RenewalProposed")
                )

    return {
        "vehicle": {
            "registration": policy_field("vehicle_registration"),
            "make": policy_field("vehicle_make", "make"),
            "model": policy_field("vehicle_model", "model"),
        },
        "cover": {
            "current_provider": _entry(policy["provider"], policy_trust, policy["doc_id"], as_of)
            if policy and policy["provider"]
            else policy_field("provider"),
            "cover_level": policy_field("cover_level"),
            "compulsory_excess": policy_field("compulsory_excess"),
            "voluntary_excess": policy_field("voluntary_excess"),
            "current_premium": policy_field("annual_premium"),
            "policy_start": policy_field("period_start_date"),
            "policy_end": policy_field("policy_end_date"),
        },
        "history": {
            "ncd_years": ncd,
            "claims_last_5_years": _entry(
                {"count": len(claims), "dates": claim_dates},
                TRUST_VERIFIED,
                [doc.get("doc_id") for doc in claims],
                (today or date.today()).isoformat(),
                basis="documents on file",
            ),
            "named_drivers": policy_field("named_drivers"),
            "premium_history": _entry(
                history,
                min_trust([item["trust"] for item in history]),
                [item["source_doc_id"] for item in history],
                history[-1]["as_of"] if history else None,
            ),
        },
    }


def profile_completeness(profile: dict) -> tuple[int, int]:
    """(populated, total) leaf fields — the router's one-line summary."""
    entries = [entry for group in profile.values() for entry in group.values()]
    return sum(e["value"] is not None for e in entries), len(entries)


def to_json(profile: dict) -> str:
    return json.dumps(profile, indent=2, ensure_ascii=False, default=str) + "\n"


def _display(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def to_markdown(profile: dict) -> str:
    headings = (("vehicle", "Your vehicle"), ("cover", "Your cover"), ("history", "Your history"))
    lines = ["# Quote-ready profile", ""]
    for group, heading in headings:
        lines.extend([f"## {heading}", ""])
        for name, entry in profile.get(group, {}).items():
            source = entry.get("source_doc_id")
            if isinstance(source, list):
                source = ", ".join(filter(None, source)) or "none"
            lines.append(
                f"- **{name.replace('_', ' ').title()}**: {_display(entry.get('value'))} "
                f"_(trust: {entry.get('trust')}; source: {source or 'none'}; as of: {entry.get('as_of') or 'unknown'})_"
            )
            if entry.get("basis"):
                lines.append(f"  - Basis: {entry['basis']}")
        lines.append("")
    return "\n".join(lines)


def export_profile(profile: dict, *, root: Path | None = None, name: str = "quote_profile") -> dict:
    """Write the profile as JSON + Markdown under the data directory's
    exports folder. Returns {"json_path", "markdown_path"}."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "quote_profile"
    out_dir = (root or data_dir()) / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{safe}.json"
    markdown_path = out_dir / f"{safe}.md"
    json_path.write_text(to_json(profile), encoding="utf-8")
    markdown_path.write_text(to_markdown(profile), encoding="utf-8")
    return {"json_path": str(json_path), "markdown_path": str(markdown_path)}
