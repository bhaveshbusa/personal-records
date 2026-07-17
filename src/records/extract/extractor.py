"""Provenance-aware extraction — the pipeline's second LLM stage.

The shape is already decided when this runs (classifier.py); extraction
only extracts, per product line, and never re-judges the shape. Every field
carries evidence: value, confidence, verbatim source snippet, page.

Ported stance from the old repo's PROVENANCE_SYSTEM_PROMPT: honest
confidence, no invented values, absent fields omitted rather than guessed.
Simplified to the new model: lines + document-level stated_total (kept
structurally separate — the MultiCover lesson).
"""

from __future__ import annotations

import json

from records.core import Extraction, Field, ProductLine, Shape
from records.extract.llm import LLMClient, LLMResponse, strip_fences


class ExtractionError(Exception):
    """The LLM response could not be parsed into an Extraction. Callers
    route the document to review — never retry-and-hope silently."""


EXTRACTION_SYSTEM_PROMPT = """You are the extraction agent in a personal-records ingestion pipeline. The document's shape has already been classified — your job is ONLY to extract its facts, with evidence for every value.

For each product line (separately priced cover: motor, home, ...) extract:
- "product": short lowercase slug ("motor", "home", ...)
- "annual_premium": the annual price for THIS line alone — never a bundle or document total
- "renewal_date": ISO date (YYYY-MM-DD) the cover renews or starts, if stated

Separately extract:
- "stated_total": the document-level total amount payable, if the document states one. This is NOT a line premium — never map a total into a line.
- "identifiers": document-level identity fields, if stated: "policy_number", "vehicle_registration", "provider". These identify WHICH policy the document is about — they are not facts or amounts.

Every extracted value is an evidence object:
{"value": <number or "YYYY-MM-DD" string>, "confidence": <0.0-1.0>, "source_text": "<exact verbatim snippet the value came from>", "source_page": <1-based page, or null>}

Rules:
- "confidence" is your genuine certainty the value is correct AND complete. If the text is ambiguous, abbreviated, or garbled, score it low (< 0.8).
- Do NOT invent values. If something is absent, omit the key entirely — never fabricate a low-confidence guess.
- Amounts are plain numbers (no currency symbols) in the document's stated currency.

Respond with a single valid JSON object — no markdown fences, no surrounding text:

{
  "lines": [
    {"product": "<slug>", "annual_premium": <evidence object>, "renewal_date": <evidence object>},
    ...
  ],
  "stated_total": <evidence object, or null if the document states no total>,
  "identifiers": {"policy_number": <evidence object>, "vehicle_registration": <evidence object>, "provider": <evidence object>}
}

Omit any identifier the document does not state; "identifiers" may be an empty object."""


def _field(data: dict | None) -> Field | None:
    if not isinstance(data, dict) or "value" not in data:
        return None
    return Field(
        value=data["value"],
        confidence=float(data.get("confidence", 0.0)),
        source_text=str(data.get("source_text", "")),
        source_page=data.get("source_page"),
    )


def extract_lines(
    document_text: str, doc_id: str, shape: Shape, llm: LLMClient
) -> tuple[Extraction, LLMResponse]:
    """Extract one document against its already-classified shape. Returns the
    Extraction plus the raw LLM response (for telemetry). Unparseable
    responses raise ExtractionError."""
    response = llm.complete(
        system=EXTRACTION_SYSTEM_PROMPT,
        user_content=f"Document content:\n\n{document_text}\n\nExtract this document with per-field evidence and confidence.",
        max_tokens=4096,
    )
    try:
        data = json.loads(strip_fences(response.text))
        lines = tuple(
            ProductLine(
                product=str(line["product"]),
                annual_premium=_field(line.get("annual_premium")),
                renewal_date=_field(line.get("renewal_date")),
            )
            for line in data["lines"]
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ExtractionError(f"unparseable extraction response for {doc_id}: {exc}") from exc
    raw_identifiers = data.get("identifiers")
    identifiers = (
        {
            name: parsed
            for name, entry in raw_identifiers.items()
            if (parsed := _field(entry)) is not None
        }
        if isinstance(raw_identifiers, dict)
        else {}
    )
    return (
        Extraction(
            doc_id=doc_id,
            shape=shape,
            lines=lines,
            stated_total=_field(data.get("stated_total")),
            identifiers=identifiers,
        ),
        response,
    )
