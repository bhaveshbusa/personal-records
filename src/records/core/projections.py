"""Projections — derived views folded from the domain-event log.

The read-side of event sourcing: pure functions over an explicit event list,
rebuildable at any time from history. No store, no I/O — testable offline.

Three projections (old repo's set, restored in 2R.2, rewired to typed
events):
  • renewal_calendar — per product, the latest renewal event wins; days-to-
    renewal and a status band.
  • current_policies — latest filed state per entity (the canonical view).
  • renewal_offers   — each live proposed renewal paired with the current
    policy for its entity, with the year-on-year premium delta.

Discard rule, shared by all folds and regression-tested since the old
repo's MultiCover incident: a DocumentDiscarded event retracts *that
document's* facts — state backed by a different document for the same
entity survives. The log keeps every event; only the projections fold
discarded evidence away. Renewal-calendar selection is over active evidence,
so discarding its latest row reveals the previous active renewal for that
product. Current-policy state deliberately retains its stricter no-revival
semantics (see `current_policies`).
"""

from __future__ import annotations

from datetime import date, datetime

from records.core.event_log import DomainEvent
from records.core.model import (
    DocumentDiscarded,
    PolicyCorrected,
    PolicyFiled,
    RenewalAccepted,
    RenewalProposed,
)
from records.core.trust import min_trust

STATUS_EXPIRED = "expired"
STATUS_DUE_SOON = "due_soon"
STATUS_OK = "ok"
STATUS_UNKNOWN = "unknown"  # no renewal date on record

DEFAULT_DUE_SOON_DAYS = 30


def _days_until(iso_date: str | None, today: date) -> int | None:
    if not iso_date:
        return None
    try:
        return (datetime.strptime(iso_date[:10], "%Y-%m-%d").date() - today).days
    except ValueError:
        return None


def renewal_calendar(
    events: list[DomainEvent],
    today: date | None = None,
    due_soon_days: int = DEFAULT_DUE_SOON_DAYS,
) -> list[dict]:
    """Every known product with its latest renewal state, days-to-renewal and
    a status band (expired / due_soon / ok / unknown), sorted soonest-first.
    The substrate for `records renewals`, reminders, and the MCP
    `get_renewals` tool."""
    today = today or date.today()

    # Retraction applies to the document, not merely to whichever row happens
    # to be visible when the discard event is replayed. Filter discarded
    # evidence first, then select latest-per-product. Otherwise a sequence
    # old proposal → later acceptance → discard later document forgets the old
    # proposal instead of revealing it again.
    discarded = {event.doc_id for event in events if isinstance(event, DocumentDiscarded)}
    latest: dict[str, DomainEvent] = {}
    for event in events:
        if (
            isinstance(event, (RenewalProposed, RenewalAccepted))
            and event.doc_id not in discarded
        ):
            latest[event.product] = event  # replay order: later events win

    rows = []
    for product, event in latest.items():
        days_left = _days_until(event.renewal_date, today)
        if days_left is None:
            status = STATUS_UNKNOWN
        elif days_left < 0:
            status = STATUS_EXPIRED
        elif days_left <= due_soon_days:
            status = STATUS_DUE_SOON
        else:
            status = STATUS_OK
        rows.append(
            {
                "product": product,
                "state": type(event).__name__,
                "annual_premium": event.annual_premium,
                "renewal_date": event.renewal_date,
                "days_left": days_left,
                "status": status,
                "doc_id": event.doc_id,  # provenance: which evidence document
                "trust": event.trust,  # extracted | verified (2R.5)
            }
        )
    rows.sort(key=lambda r: (r["days_left"] is None, r["days_left"]))
    return rows


def current_policies(events: list[DomainEvent]) -> list[dict]:
    """Latest filed state per entity — the canonical "what do I currently
    hold" view. Folds PolicyFiled/PolicyCorrected (later events win);
    RenewalProposed is invisible by construction (a proposed future state
    is not the current policy). A discard removes the entity only if the
    discarded document is the one backing its current state — the old
    repo's real-data regression (2026-07-08): discarding a misextracted
    renewal invitation must not delete the policy filed from the schedule."""
    state: dict[str, dict] = {}
    for event in events:
        if isinstance(event, (PolicyFiled, PolicyCorrected)):
            state[event.entity_id] = {
                "entity_id": event.entity_id,
                "doc_id": event.doc_id,
                "doc_type": event.doc_type,
                "state": type(event).__name__,
                "fields": event.fields,
                "valid_from": event.valid_from,
                "valid_to": event.valid_to,
                "provider": event.provider,
                "trust": event.trust,  # extracted | verified (2R.5)
            }
        elif isinstance(event, DocumentDiscarded):
            state = {
                eid: row for eid, row in state.items() if row["doc_id"] != event.doc_id
            }
    return list(state.values())


def conflicting_policy_filed(candidate: PolicyFiled, events: list[DomainEvent]) -> dict | None:
    """Guard for the accept path: if emitting `candidate` would overwrite a
    DIFFERENT document's current state for the same entity, return that
    existing state so the caller can route to review instead —
    `current_policies` folds "latest write wins" with no merge, so filing
    an incomplete/misclassified record over an earlier correct one would
    silently lose it. None if there is no conflict: no prior state for the
    entity, or the same document being re-filed."""
    for row in current_policies(events):
        if row["entity_id"] == candidate.entity_id and row["doc_id"] != candidate.doc_id:
            return row
    return None


def renewal_offers(events: list[DomainEvent]) -> list[dict]:
    """Every live RenewalProposed offer paired with the current policy for
    its entity — "what does my renewal quote look like next to what I have
    now?" — with the year-on-year premium delta the ±40% band consumes.

    Discarded offers are excluded (the MultiCover false "+62.7% renewal
    shock" retraction). An offer with no entity link, or an entity with no
    current policy, still appears — flagged not comparable — so nothing
    silently vanishes. Entity linking that populates `entity_id` on real
    ingests lands in 2R.3."""
    policies = {p["entity_id"]: p for p in current_policies(events)}
    discarded = {e.doc_id for e in events if isinstance(e, DocumentDiscarded)}
    offers = []
    for event in events:
        if not isinstance(event, RenewalProposed) or event.doc_id in discarded:
            continue
        current = policies.get(event.entity_id) if event.entity_id is not None else None
        if current is None:
            reason = (
                "no current policy on record for this entity"
                if event.entity_id is not None
                else "offer is not linked to a policy entity (entity linking lands in 2R.3)"
            )
            change: dict = {"comparable": False, "reason": reason}
        else:
            prev_field = current["fields"].get("annual_premium")
            prev = (
                prev_field.value
                if prev_field is not None and isinstance(prev_field.value, (int, float))
                else None
            )
            if prev is None or prev == 0:
                change = {"comparable": False, "reason": "current policy has no priced annual_premium"}
            else:
                delta = round(event.annual_premium - prev, 2)
                change = {
                    "comparable": True,
                    "previous": prev,
                    "latest": event.annual_premium,
                    "delta": delta,
                    "pct_change": round(delta / prev * 100, 1),
                }
        offers.append(
            {
                "entity_id": event.entity_id,
                "doc_id": event.doc_id,
                "product": event.product,
                "quoted_premium": event.annual_premium,
                "renewal_date": event.renewal_date,
                "current_policy_doc_id": current["doc_id"] if current else None,
                "premium_change": change,
                # A comparison is only as trusted as its weakest side (2R.5).
                "trust": min_trust(
                    [event.trust] + ([current["trust"]] if current else [])
                ),
            }
        )
    return offers
