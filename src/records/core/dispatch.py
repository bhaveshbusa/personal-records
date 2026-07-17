"""Doc-type → domain-event dispatch: which doc types become which facts.

Ported from the old repo's `domain_events.event_for_confirmed_record` — the
single place that owns the mapping. Semantics kept:

- policy_schedule → PolicyFiled, ncd_letter → NcdConfirmed.
- A confirmed proof artifact with no fact to record (certificate) returns
  None: the stored document itself is the record.
- A doc_type this dispatcher doesn't recognise at all is an error, not a
  silent no-op — an unmapped type reaching dispatch means the schema
  registry and this map have drifted apart. (In the old repo too, only a
  subset of registry types had events; the rest raised loudly.)

Callers that must not crash on unmapped types (`decide_fields`,
`queue.confirm`) ask `has_event_vocabulary` first and park/refuse with a
clear message instead. renewal_quote never reaches here: quote-like types
go through the shape/lines path (RenewalProposed/RenewalAccepted), and
reference_text/raw_evidence types stop at intake.
"""

from __future__ import annotations

from records.core.model import FieldExtraction, NcdConfirmed, PolicyFiled
from records.core.registry import DocTypeSchema
from records.core.trust import TRUST_EXTRACTED

# Confirmed doc types that are artifacts, not facts about an entity — they
# emit no domain event; the document store holds the record.
EVENT_FREE_DOC_TYPES = frozenset({"certificate"})

_DISPATCHED_DOC_TYPES = frozenset({"policy_schedule", "ncd_letter"})


def has_event_vocabulary(doc_type: str) -> bool:
    """Can a confirmed FieldExtraction of this doc_type be dispatched —
    either to a domain event or to a deliberate no-event acceptance?"""
    return doc_type in _DISPATCHED_DOC_TYPES or doc_type in EVENT_FREE_DOC_TYPES


def _entity_id(extraction: FieldExtraction) -> str:
    f = extraction.fields.get("policy_number")
    return str(f.value) if f is not None else extraction.doc_id


def _value(extraction: FieldExtraction, field_name: str | None) -> str | None:
    if field_name is None:
        return None
    f = extraction.fields.get(field_name)
    return None if f is None else str(f.value)


def event_for_fields(
    extraction: FieldExtraction,
    schema: DocTypeSchema,
    entity_id: str | None = None,
    trust: str = TRUST_EXTRACTED,
) -> PolicyFiled | NcdConfirmed | None:
    """Build the domain event a confirmed flat-field extraction becomes.
    None means "accepted, nothing to record" (proof artifact). Raises
    ValueError for a doc_type with no dispatch — the drift alarm.

    `entity_id` is the linker's resolution (2R.3) when it linked or minted;
    absent, the old fallback applies: policy_number, else doc_id.
    `trust` is how the fact enters the log (2R.5): "extracted" from the
    rules' auto-accept path, "verified" from a human confirm."""
    if extraction.doc_type == "policy_schedule":
        return PolicyFiled(
            doc_id=extraction.doc_id,
            doc_type=extraction.doc_type,
            entity_id=entity_id or _entity_id(extraction),
            fields=dict(extraction.fields),
            valid_from=_value(extraction, schema.valid_from_source),
            valid_to=_value(extraction, schema.valid_to_source),
            provider=_value(extraction, schema.provider_field),
            trust=trust,
        )
    if extraction.doc_type == "ncd_letter":
        return NcdConfirmed(
            doc_id=extraction.doc_id,
            doc_type=extraction.doc_type,
            entity_id=entity_id or _entity_id(extraction),
            fields=dict(extraction.fields),
            provider=_value(extraction, schema.provider_field),
            trust=trust,
        )
    if extraction.doc_type in EVENT_FREE_DOC_TYPES:
        return None
    raise ValueError(f"no event dispatch for confirmed doc_type: {extraction.doc_type!r}")
