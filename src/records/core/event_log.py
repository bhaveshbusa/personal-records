"""Domain-event log — the append-only business facts of the system.

Event sourcing: confirmed facts are recorded as immutable events; current
state and every derived view (renewal calendar) are projections rebuilt from
this log. Events are durable truth and are never deleted.

Storage is an append-only JSONL file under the data directory. `append` is
the single write path. Events go in as the typed dataclasses from
`records.core.model` and come back out typed on `replay` — the envelope
(event_id, recorded_at) is a persistence detail callers never build by hand.

Ported from the old repo's `domain_events.py`, rewired to the Phase 1 event
types. The old telemetry stream (`events.py`: tokens/cost) is separate and
lands with the LLM adapter — domain truth and disposable telemetry never
share a file.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from records.config import data_dir
from records.core.model import (
    DocumentDiscarded,
    Field,
    NcdConfirmed,
    PolicyCorrected,
    PolicyFiled,
    RenewalAccepted,
    RenewalProposed,
)

DomainEvent = (
    RenewalProposed
    | RenewalAccepted
    | PolicyFiled
    | PolicyCorrected
    | DocumentDiscarded
    | NcdConfirmed
)

_EVENT_TYPES: dict[str, type] = {
    "RenewalProposed": RenewalProposed,
    "RenewalAccepted": RenewalAccepted,
    "PolicyFiled": PolicyFiled,
    "PolicyCorrected": PolicyCorrected,
    "DocumentDiscarded": DocumentDiscarded,
    "NcdConfirmed": NcdConfirmed,
}


def _log_path(root: Path | None) -> Path:
    return (root or data_dir()) / "domain_events.jsonl"


def append(event: DomainEvent, *, root: Path | None = None) -> dict:
    """Append one domain event. The single write path."""
    event_type = type(event).__name__
    if event_type not in _EVENT_TYPES:
        raise TypeError(f"not a domain event: {event_type}")
    envelope = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "data": asdict(event),
    }
    path = _log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(envelope, default=str) + "\n")
    return envelope


def replay(*, root: Path | None = None) -> list[DomainEvent]:
    """Read the log oldest-first as typed events (event-sourcing replay
    order). Returns [] if the log doesn't exist yet."""
    path = _log_path(root)
    if not path.exists():
        return []
    events: list[DomainEvent] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                continue
            cls = _EVENT_TYPES.get(envelope.get("event_type"))
            if cls is None:
                continue  # unknown/future event type: skip, don't crash replay
            data = dict(envelope["data"])
            if data.get("provenance") is not None:
                data["provenance"] = Field(**data["provenance"])
            if data.get("fields"):
                data["fields"] = {name: Field(**f) for name, f in data["fields"].items()}
            events.append(cls(**data))
    return events
