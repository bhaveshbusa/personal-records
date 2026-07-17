"""Domain model — designed fresh from the MultiCover failure.

The failure class this model makes unrepresentable: schema-valid but
reality-invalid extraction (a bundle total mapped into a single line's
premium with confidence 1.0). Hence:

- A Document describes 1..n product lines; single-policy is the n=1 case.
- Document-level `stated_total` is structurally separate from any line's
  premium — the exact confusion that produced the false +62.7% delta.
- The classifier's shape verdict (line_count, renewal_status, or unsure)
  is captured *before* facts, with an explicit escape hatch.
- Quote != acceptance: `RenewalProposed` (per line) and `RenewalAccepted`
  are distinct events. An auto-renewal invitation for the current period
  is not a future offer.

Everything here is a frozen dataclass: values in, values out, no behaviour.
Routing logic lives in `records.review.rules` (pure functions), persistence
in `records.store` — the model knows about neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from records.core.trust import TRUST_EXTRACTED

RENEWAL_PROPOSED = "proposed"
RENEWAL_ALREADY_ACCEPTED = "already_accepted"


@dataclass(frozen=True)
class Field:
    """A single extracted value with its provenance and confidence.

    Provenance answers "where did this come from"; confidence answers "how
    sure was the extractor". The MultiCover failure proves confidence alone
    cannot catch shape errors — it had confidence 1.0 and correct provenance.
    """

    value: float | str
    confidence: float  # 0.0 - 1.0, extractor's own estimate
    source_text: str = ""  # verbatim snippet the value came from
    source_page: int | None = None


@dataclass(frozen=True)
class ProductLine:
    """One product line within a document (motor, home, ...)."""

    product: str
    annual_premium: Field | None = None
    renewal_date: Field | None = None  # ISO date string value, when stated


@dataclass(frozen=True)
class DocTypeVerdict:
    """Classifier verdict on which registry type a document is — the
    pipeline's first LLM judgment (2R.1), emitted before shape or any fact.

    "unknown" is the escape hatch: the classifier must never force a fit.
    Routing (threshold, unregistered slugs) is `review.rules.doc_type_issues`.
    """

    doc_type: str
    confidence: float = 0.0
    rationale: str = ""


@dataclass(frozen=True)
class Shape:
    """Classifier verdict on what kind of document this is — emitted and
    checked *before* any extracted fact is accepted.

    `unsure=True` is the escape hatch: the classifier must never force a
    fit. "Misfiled is worse than unextracted."
    """

    line_count: int | None = None
    renewal_status: str | None = None  # RENEWAL_PROPOSED | RENEWAL_ALREADY_ACCEPTED
    unsure: bool = False


@dataclass(frozen=True)
class Extraction:
    """The full proposed extraction for one document: shape + lines + the
    document-level stated total. This is what the LLM proposes and the
    deterministic rules judge.

    `identifiers` are the document-level identity fields (policy_number,
    vehicle_registration, provider — the registry's canonical fields for
    quote-like types) that the entity linker (2R.3) resolves against; they
    are never facts, never lines."""

    doc_id: str
    shape: Shape
    lines: tuple[ProductLine, ...] = ()
    stated_total: Field | None = None
    identifiers: dict[str, Field] = field(default_factory=dict)


@dataclass(frozen=True)
class FieldExtraction:
    """Registry-driven extraction for a non-quote document: flat canonical
    fields with provenance, judged against the doc_type's schema. Quote-like
    documents use `Extraction` (lines + stated_total — the MultiCover model)
    instead."""

    doc_id: str
    doc_type: str
    fields: dict[str, Field] = field(default_factory=dict)


# --- Domain events (persistence lands in Phase 2's event spine) ---
#
# Every fact event carries `trust` (core/trust.py, reconciled 2R.5): how the
# fact entered the log — "extracted" when the deterministic rules accepted
# an LLM extraction, "verified" when a human confirmed from the review
# queue. One string per event, not per field: the queue confirms whole
# extractions, and per-field granularity already lives in each Field's
# confidence + source_text. DocumentDiscarded carries none — a retraction
# is an instruction to the folds, not a fact about the world.


@dataclass(frozen=True)
class RenewalProposed:
    """An insurer proposed a renewal for one product line (a quote).

    `entity_id` links the offer to the policy entity it renews so the
    renewal_offers projection can pair it with current state. Nothing sets
    it until entity linking lands (2R.3) — an unlinked offer simply pairs
    with nothing."""

    doc_id: str
    product: str
    annual_premium: float
    provenance: Field
    renewal_date: str | None = None  # ISO date, business time
    entity_id: str | None = None
    trust: str = TRUST_EXTRACTED


@dataclass(frozen=True)
class RenewalAccepted:
    """The user accepted a renewal — distinct from being offered one."""

    doc_id: str
    product: str
    annual_premium: float
    renewal_date: str | None = None
    trust: str = TRUST_EXTRACTED


@dataclass(frozen=True)
class PolicyFiled:
    """A record document (policy_schedule) became the current state of an
    entity — either a clean extraction the rules accepted, or a human
    confirming from the review queue. Restored from the old repo (2R.2),
    rebuilt on typed `Field` provenance instead of loose trust strings.

    `entity_id` identifies the policy across the log: the policy number
    when extracted, else the document id (good enough to project a
    per-entity current record)."""

    doc_id: str
    doc_type: str
    entity_id: str
    fields: dict[str, Field] = field(default_factory=dict)
    valid_from: str | None = None  # ISO date, business time (cover start)
    valid_to: str | None = None
    provider: str | None = None
    trust: str = TRUST_EXTRACTED


@dataclass(frozen=True)
class PolicyCorrected:
    """A previously filed record was corrected — same payload as
    PolicyFiled; the projection folds it identically (later event wins).
    The correction path that emits it is future work; the vocabulary and
    fold exist so the log's shape is settled now."""

    doc_id: str
    doc_type: str
    entity_id: str
    fields: dict[str, Field] = field(default_factory=dict)
    valid_from: str | None = None
    valid_to: str | None = None
    provider: str | None = None
    trust: str = TRUST_EXTRACTED


@dataclass(frozen=True)
class NcdConfirmed:
    """A no-claims-discount letter was confirmed. Consumed by the quote
    profile in 2R.5 — nothing folds it yet."""

    doc_id: str
    doc_type: str
    entity_id: str
    fields: dict[str, Field] = field(default_factory=dict)
    provider: str | None = None
    trust: str = TRUST_EXTRACTED


@dataclass(frozen=True)
class DocumentDiscarded:
    """A document's facts were retracted (event-sourced correction: the log
    keeps every event; projections fold this document's facts away). Always
    document-scoped — the old repo's MultiCover mitigation: discarding a
    misread renewal invitation must not delete the policy filed from the
    schedule (same entity, different document)."""

    doc_id: str
    reason: str = ""


def _field_from_dict(data: dict | None) -> Field | None:
    return Field(**data) if data else None


def extraction_from_dict(data: dict) -> Extraction:
    """Rebuild an Extraction from `dataclasses.asdict` output (the review
    queue's serialization). Inverse of asdict — no schema evolution logic
    until a real migration demands it."""
    return Extraction(
        doc_id=data["doc_id"],
        shape=Shape(**data["shape"]),
        lines=tuple(
            ProductLine(
                product=line["product"],
                annual_premium=_field_from_dict(line.get("annual_premium")),
                renewal_date=_field_from_dict(line.get("renewal_date")),
            )
            for line in data.get("lines", ())
        ),
        stated_total=_field_from_dict(data.get("stated_total")),
        identifiers={
            name: Field(**f) for name, f in (data.get("identifiers") or {}).items()
        },
    )


def field_extraction_from_dict(data: dict) -> FieldExtraction:
    """Rebuild a FieldExtraction from `dataclasses.asdict` output (the review
    queue's serialization)."""
    return FieldExtraction(
        doc_id=data["doc_id"],
        doc_type=data["doc_type"],
        fields={name: Field(**f) for name, f in data.get("fields", {}).items()},
    )


@dataclass(frozen=True)
class Decision:
    """Outcome of routing one extraction: either events to emit, or reasons
    it must go to human review — never both, never neither."""

    events: tuple = ()
    review_reasons: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.review_reasons
