"""Shape rules and deterministic cross-checks — the trust gate.

Pure functions, no LLM, no I/O. The LLM proposes an `Extraction`; these
rules decide whether it becomes events or goes to human review. Any issue
means review with *zero* events emitted — "misfiled is worse than
unextracted".

Phase 1 stance (see docs/adr-0001-phase1-routing.md): any multi-line
document routes to review. Per-line acceptance of clean multi-line
documents is Phase 2+ work, if real documents earn it.
"""

from __future__ import annotations

from records.core import (
    AMBIGUOUS,
    REGISTRY,
    RENEWAL_ALREADY_ACCEPTED,
    RENEWAL_PROPOSED,
    UNKNOWN_DOC_TYPE,
    Decision,
    DocTypeSchema,
    DocTypeVerdict,
    DomainEvent,
    Extraction,
    FieldExtraction,
    LinkResult,
    PolicyFiled,
    RenewalProposed,
    Shape,
    conflicting_policy_filed,
    event_for_fields,
    has_event_vocabulary,
)

# Sanity band for renewal premium vs prior year (plan 1.3): start at ±40%.
RENEWAL_BAND = 0.40
# Money tolerance for sum-of-lines vs stated_total (float pennies).
MONEY_TOLERANCE = 0.01
# Below this, a doc-type verdict is resolved but untrusted (old repo default).
DOC_TYPE_CONFIDENCE_THRESHOLD = 0.75
# A field scored below this is sent to review even if present — the old
# repo's `route_for_review` low-confidence rule (REASON_LOW_CONFIDENCE),
# reconciled here at 2R.5. 0.80 keeps clean extractions out of the queue
# while catching the genuinely shaky ones.
FIELD_CONFIDENCE_THRESHOLD = 0.80


def doc_type_issues(
    verdict: DocTypeVerdict,
    registry: dict[str, DocTypeSchema] | None = None,
    confidence_threshold: float = DOC_TYPE_CONFIDENCE_THRESHOLD,
) -> list[str]:
    """Classification checks run before shape or any fact. Ported semantics
    from the old repo's `classify_document` routing: "unknown", a slug the
    registry doesn't know, or below-threshold confidence all mean review —
    misfiled is worse than unextracted. At-threshold is trusted."""
    registry = REGISTRY if registry is None else registry
    issues: list[str] = []
    if verdict.doc_type == UNKNOWN_DOC_TYPE:
        issues.append(f"classification: unknown document type ({verdict.rationale or 'no rationale'})")
    elif verdict.doc_type not in registry:
        issues.append(f"classification: doc_type {verdict.doc_type!r} is not in the schema registry")
    if verdict.confidence < confidence_threshold:
        issues.append(
            f"classification: confidence {verdict.confidence:.2f} below threshold "
            f"{confidence_threshold:.2f}"
        )
    return issues


def field_issues(
    extraction: FieldExtraction,
    schema: DocTypeSchema,
    confidence_threshold: float = FIELD_CONFIDENCE_THRESHOLD,
) -> list[str]:
    """Deterministic schema checks for a flat-field extraction — the old
    repo's `route_for_review` field rules, reconciled with the new gate at
    2R.5:

    - missing_required: every required field must be present.
    - low_confidence: a present field scored below the threshold parks the
      record even though a value was read — "misfiled is worse than
      unextracted" applies to shaky values too.

    Not ported: the divergent-candidates conflict rule — the new extractor
    proposes one reading per field (no `candidates` list); if multi-reading
    extraction ever returns, that rule comes back with it."""
    issues = [
        f"{extraction.doc_type}: required field {name!r} not extracted"
        for name in schema.required
        if name not in extraction.fields
    ]
    issues += [
        f"{extraction.doc_type}: field {name!r} confidence {f.confidence:.2f} below "
        f"threshold {confidence_threshold:.2f} (value: {f.value!r})"
        for name, f in extraction.fields.items()
        if f.confidence < confidence_threshold
    ]
    return issues


def decide_fields(
    extraction: FieldExtraction,
    schema: DocTypeSchema,
    events: list[DomainEvent] | tuple = (),
    link: LinkResult | None = None,
) -> Decision:
    """Route one flat-field extraction (2R.2 vocabulary restored). Clean
    extractions emit the domain event matching the doc_type (PolicyFiled,
    NcdConfirmed); a clean proof artifact with no fact to record
    (certificate) is accepted with zero events — the stored document is the
    record. A doc_type whose event vocabulary isn't designed yet (payslip,
    passport, ... — the old repo had none for these either) parks in review.

    `events` is the replayed log, used for the overwrite guard: a
    PolicyFiled that would replace a DIFFERENT document's current state for
    the same entity routes to review instead of silently losing it.

    `link` is the entity linker's resolution (2R.3): ambiguity parks; a
    link/mint supplies the event's entity_id (else the dispatch fallback:
    policy_number, then doc_id)."""
    reasons = field_issues(extraction, schema)
    if not has_event_vocabulary(extraction.doc_type):
        reasons.append(
            f"{extraction.doc_type}: no event vocabulary for this doc_type yet — "
            "parked for human review"
        )
        return Decision(review_reasons=tuple(reasons))
    reasons += link_issues(link)
    if reasons:
        return Decision(review_reasons=tuple(reasons))

    entity_id = link.entity_id if link is not None and link.linked else None
    event = event_for_fields(extraction, schema, entity_id=entity_id)
    if event is None:
        return Decision()  # accepted; nothing to record beyond the document itself
    if isinstance(event, PolicyFiled):
        conflict = conflicting_policy_filed(event, list(events))
        if conflict is not None:
            return Decision(
                review_reasons=(
                    f"conflict: entity {event.entity_id!r} already has current state "
                    f"from document {conflict['doc_id']} — accepting this would "
                    "overwrite it (confirm from review to override)",
                )
            )
    return Decision(events=(event,))


def link_issues(link: LinkResult | None) -> list[str]:
    """Routing rule for the Link stage (2R.3): only AMBIGUOUS parks — a
    document matching multiple entities must not guess. No-match/no-
    identifier documents proceed unlinked (deliberate deviation from the
    old repo, see core/linking.py): in the event model an unlinked fact is
    harmless, and the first-quote-with-no-policy flow must keep working."""
    if link is not None and link.status == AMBIGUOUS:
        return [f"linking: ambiguous — {link.reason}"]
    return []


def line_confidence_issues(
    extraction: Extraction, confidence_threshold: float = FIELD_CONFIDENCE_THRESHOLD
) -> list[str]:
    """The low-confidence rule for the quote path (2R.5): in the old repo a
    renewal_quote was a flat record and went through the same
    `route_for_review` confidence check as everything else — the new
    line-based model keeps that protection per priced line."""
    return [
        f"{line.product}: annual_premium confidence {line.annual_premium.confidence:.2f} "
        f"below threshold {confidence_threshold:.2f}"
        for line in extraction.lines
        if line.annual_premium is not None
        and line.annual_premium.confidence < confidence_threshold
    ]


def shape_issues(shape: Shape) -> list[str]:
    """Shape checks run before any fact is accepted."""
    issues: list[str] = []
    if shape.unsure:
        issues.append("shape: classifier unsure")
        return issues  # unsure means nothing else about the shape is trusted
    if shape.line_count is None or shape.line_count < 1:
        issues.append("shape: no product lines identified")
    elif shape.line_count > 1:
        issues.append(f"shape: multi-line document (line_count={shape.line_count})")
    if shape.renewal_status == RENEWAL_ALREADY_ACCEPTED:
        issues.append("shape: renewal already accepted — not a future offer")
    elif shape.renewal_status != RENEWAL_PROPOSED:
        issues.append(f"shape: unknown renewal_status ({shape.renewal_status!r})")
    return issues


def cross_check_issues(extraction: Extraction, prior_year_premium: float | None = None) -> list[str]:
    """Deterministic validate-stage checks (plan 1.3)."""
    issues: list[str] = []

    if extraction.shape.line_count is not None and extraction.shape.line_count != len(extraction.lines):
        issues.append(
            f"shape/extraction mismatch: shape says {extraction.shape.line_count} line(s), "
            f"extraction has {len(extraction.lines)}"
        )

    premiums = [
        line.annual_premium.value
        for line in extraction.lines
        if line.annual_premium is not None and isinstance(line.annual_premium.value, (int, float))
    ]

    # Sum-of-lines vs stated_total, where both visible.
    if extraction.stated_total is not None and premiums:
        total = extraction.stated_total.value
        if isinstance(total, (int, float)) and abs(sum(premiums) - total) > MONEY_TOLERANCE:
            issues.append(
                f"cross-check: sum of line premiums ({sum(premiums):.2f}) != stated_total ({total:.2f})"
            )

    # Renewal premium vs prior year outside the sanity band.
    if prior_year_premium is not None and prior_year_premium > 0:
        for line, premium in zip(extraction.lines, premiums):
            delta = (premium - prior_year_premium) / prior_year_premium
            if abs(delta) > RENEWAL_BAND:
                issues.append(
                    f"cross-check: {line.product} renewal premium {premium:.2f} is "
                    f"{delta:+.1%} vs prior year {prior_year_premium:.2f} (band ±{RENEWAL_BAND:.0%})"
                )

    return issues


def decide(
    extraction: Extraction,
    prior_year_premium: float | None = None,
    link: LinkResult | None = None,
) -> Decision:
    """Route one extraction: events, or review reasons — never both.

    Order matters: shape first ("shape checks before facts"); cross-checks
    and link checks still run so the review queue shows every problem at
    once. `link` is the entity linker's resolution (2R.3): ambiguity parks;
    a link carries its entity_id onto the emitted events so renewal_offers
    can pair offer with current policy.
    """
    reasons = (
        shape_issues(extraction.shape)
        + cross_check_issues(extraction, prior_year_premium)
        + line_confidence_issues(extraction)
        + link_issues(link)
    )
    if reasons:
        return Decision(review_reasons=tuple(reasons))

    entity_id = link.entity_id if link is not None and link.linked else None

    # Clean single-line proposed renewal: emit one RenewalProposed.
    events = []
    for line in extraction.lines:
        if line.annual_premium is None:
            return Decision(review_reasons=(f"{line.product}: no premium extracted",))
        events.append(
            RenewalProposed(
                doc_id=extraction.doc_id,
                product=line.product,
                annual_premium=float(line.annual_premium.value),
                provenance=line.annual_premium,
                renewal_date=str(line.renewal_date.value) if line.renewal_date else None,
                entity_id=entity_id,
            )
        )
    return Decision(events=tuple(events))
