"""Staged ingestion pipeline: Intake → Classify → Extract → Validate → Route.

The orchestration layer — the only module that touches store, extract,
review, and the event log together. Each stage stays independently
testable; this file just walks a document through them:

  1. Intake        — evidence into the document store (content-hash dedup);
                     PDF/text converted to plain text for the LLM.
  2. Classify type — LLM proposes the doc_type against the schema registry
                     (judgment call #1); unknown/low-confidence → review.
                     reference_text/raw_evidence types stop here — stored.
  3. Classify shape— quote-like types only: LLM proposes the Shape (#2).
  4. Extract       — LLM proposes facts with provenance (#3): per-line for
                     quote-like types, flat canonical fields otherwise.
  5. Link          — deterministic, zero-LLM (2R.3): match the document's
                     identifiers to the policy entity it evidences
                     (`core.linking`); ambiguity → review.
  6. Validate/     — deterministic rules (`review.decide` / `decide_fields`)
     Route           turn the proposal into domain events, or park it in
                     the review queue.

"AI proposes, system verifies, human confirms." The LLM appears at most
three times, every time as a proposer.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from records.core import (
    RAW_EVIDENCE,
    REFERENCE_TEXT,
    REGISTRY,
    DocumentDiscarded,
    Extraction,
    FieldExtraction,
    append,
    current_policies,
    known_entities,
    link_document,
    renewal_calendar,
    replay,
)
from records.extract import (
    ExtractionError,
    LLMClient,
    classify_doc_type,
    classify_shape,
    extract_fields,
    extract_lines,
    record_llm_usage,
)
from records.review import decide, decide_fields, doc_type_issues, queue
from records.store import put_document, update_document


class IntakeError(Exception):
    """The file could not be turned into text for extraction."""


@dataclass(frozen=True)
class IngestResult:
    doc_id: str
    outcome: str  # "duplicate" | "accepted" | "review" | "stored"
    doc_type: str | None = None
    events: tuple = ()
    review_reasons: tuple[str, ...] = ()
    extraction: Extraction | FieldExtraction | None = None


def _document_text(file_bytes: bytes, media_type: str) -> str:
    """Intake conversion: what the LLM actually reads."""
    if media_type == "application/pdf":
        from pypdf import PdfReader  # lazy: text-only ingests skip the import

        reader = PdfReader(BytesIO(file_bytes))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    elif media_type.startswith("text/"):
        text = file_bytes.decode("utf-8", errors="replace")
    else:
        raise IntakeError(f"unsupported media type: {media_type} (PDF and text only for now)")
    if not text.strip():
        raise IntakeError("no extractable text (scanned image PDF? OCR is not supported yet)")
    return text


def _paged_text(file_bytes: bytes, media_type: str, *, fallback: str) -> str:
    """Text for the wording chunker, with page boundaries preserved: PDF
    pages are re-joined with the chunker's PAGE_BREAK (form-feed) so a
    citation can say which page its clause started on. `_document_text`
    joins pages with blank lines instead — friendlier for LLM prompts, so
    both exist. Non-PDF input has no pages; the already-extracted text is
    returned as one page."""
    if media_type != "application/pdf":
        return fallback
    from pypdf import PdfReader  # lazy, as in _document_text

    from records.query.wording_chunker import PAGE_BREAK  # noqa: PLC0415

    reader = PdfReader(BytesIO(file_bytes))
    return PAGE_BREAK.join(page.extract_text() or "" for page in reader.pages)


def _prior_year_premium(product: str, events: list, entity_id: str | None = None) -> float | None:
    """Baseline for the ±40% renewal sanity band. Prefers the linked
    entity's current policy premium (a filed schedule is a better prior
    than the last quote seen — 2R.3); falls back to the latest renewal
    event for the product."""
    if entity_id is not None:
        for row in current_policies(events):
            if row["entity_id"] == entity_id:
                premium = row["fields"].get("annual_premium")
                if premium is not None and isinstance(premium.value, (int, float)):
                    return float(premium.value)
                break  # entity found but unpriced: fall through to calendar
    for row in renewal_calendar(events):
        if row["product"] == product:
            return row["annual_premium"]
    return None


def ingest(path: str | Path, llm: LLMClient, *, root: Path | None = None) -> IngestResult:
    """Walk one document through the pipeline. Returns what happened; all
    writes (evidence, telemetry, events or queue item) are done on return."""
    path = Path(path)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    file_bytes = path.read_bytes()

    record, is_duplicate = put_document(file_bytes, path.name, media_type=media_type, root=root)
    doc_id = record["doc_id"]
    if is_duplicate:
        return IngestResult(doc_id=doc_id, outcome="duplicate")

    try:
        text = _document_text(file_bytes, media_type)
    except IntakeError as exc:
        queue.add(doc_id, (f"intake: {exc}",), None, root=root)
        return IngestResult(doc_id=doc_id, outcome="review", review_reasons=(f"intake: {exc}",))

    # Stage 2 — doc_type against the schema registry (2R.1).
    verdict, response = classify_doc_type(text, llm)
    record_llm_usage("classify_doc_type", response, root=root)
    type_issues = tuple(doc_type_issues(verdict))
    if type_issues:
        queue.add(doc_id, type_issues, None, root=root)
        return IngestResult(
            doc_id=doc_id, outcome="review", doc_type=verdict.doc_type, review_reasons=type_issues
        )

    schema = REGISTRY[verdict.doc_type]

    # reference_text / raw_evidence: nothing to extract — the document
    # itself is the record. The doc_type is persisted onto the document
    # record because these types emit no events: the store is the only
    # place the query layer can find "the policy wording on file" (2R.4).
    if schema.role in (REFERENCE_TEXT, RAW_EVIDENCE):
        update_document(doc_id, doc_type=verdict.doc_type, root=root)
        if schema.role == REFERENCE_TEXT:
            # 2R.4 wording hook: chunk + index for coverage Q&A. Synchronous
            # and zero-LLM — deterministic chunking over the text we already
            # extracted (re-paged for PDFs so citations keep page numbers).
            from records.query.wording_chunker import index_wording  # noqa: PLC0415 — read-side module; the one ingest→query crossing

            chunks = index_wording(
                _paged_text(file_bytes, media_type, fallback=text), doc_id, root=root
            )
            update_document(doc_id, chunk_count=len(chunks), root=root)
        return IngestResult(doc_id=doc_id, outcome="stored", doc_type=verdict.doc_type)

    if schema.quote_like:
        # Stages 3-5, renewal path: shape → per-line extraction → decide.
        shape, response = classify_shape(text, llm)
        record_llm_usage("classify_shape", response, root=root)

        try:
            extraction, response = extract_lines(text, doc_id, shape, llm)
            record_llm_usage("extract_lines", response, root=root)
        except ExtractionError as exc:
            reasons = (f"extraction: {exc}",)
            queue.add(doc_id, reasons, None, root=root)
            return IngestResult(
                doc_id=doc_id, outcome="review", doc_type=verdict.doc_type, review_reasons=reasons
            )

        # Link stage (2R.3): resolve which policy entity the quote renews.
        log_events = replay(root=root)
        link = link_document(
            extraction.identifiers, known_entities(log_events), doc_type=verdict.doc_type
        )
        prior = (
            _prior_year_premium(
                extraction.lines[0].product,
                log_events,
                entity_id=link.entity_id if link.linked else None,
            )
            if len(extraction.lines) == 1
            else None
        )
        decision = decide(extraction, prior_year_premium=prior, link=link)
    else:
        # Stages 4-5, flat-field path: registry-driven extraction → decide.
        try:
            extraction, response = extract_fields(text, doc_id, schema, llm)
            record_llm_usage("extract_fields", response, root=root)
        except ExtractionError as exc:
            reasons = (f"extraction: {exc}",)
            queue.add(doc_id, reasons, None, root=root)
            return IngestResult(
                doc_id=doc_id, outcome="review", doc_type=verdict.doc_type, review_reasons=reasons
            )
        # Link stage (2R.3): flat-field docs link on their extracted
        # identity fields (policy_number / vehicle_registration).
        log_events = replay(root=root)
        link = link_document(
            extraction.fields, known_entities(log_events), doc_type=verdict.doc_type
        )
        decision = decide_fields(extraction, schema, events=log_events, link=link)

    if decision.accepted:
        for event in decision.events:
            append(event, root=root)
        return IngestResult(
            doc_id=doc_id,
            # Accepted with zero events: a clean proof artifact (e.g.
            # certificate) — the stored document is the record.
            outcome="accepted" if decision.events else "stored",
            doc_type=verdict.doc_type,
            events=decision.events,
            extraction=extraction,
        )

    queue.add(doc_id, decision.review_reasons, extraction, root=root)
    return IngestResult(
        doc_id=doc_id,
        outcome="review",
        doc_type=verdict.doc_type,
        review_reasons=decision.review_reasons,
        extraction=extraction,
    )


def confirm(doc_id: str, *, root: Path | None = None) -> tuple:
    """Human confirms a queued item: its events enter the log, linked to
    the entity their identifiers resolve to (2R.3)."""
    events = queue.confirm(doc_id, root=root, events=replay(root=root))
    for event in events:
        append(event, root=root)
    return tuple(events)


def discard(doc_id: str, *, reason: str = "", root: Path | None = None) -> DocumentDiscarded:
    """Retract a document's facts (event-sourced correction): append a
    DocumentDiscarded event; every projection folds that document's facts
    away. The log keeps both the facts and the retraction; the document
    itself stays in the store as evidence."""
    event = DocumentDiscarded(doc_id=doc_id, reason=reason)
    append(event, root=root)
    return event
