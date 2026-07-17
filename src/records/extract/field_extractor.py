"""Registry-driven flat-field extraction — for non-quote document types.

Ported stance from the old repo's PROVENANCE_SYSTEM_PROMPT, simplified to
the new model: the doc_type is already decided (doc_classifier.py) when
this runs, so extraction extracts against that one schema's canonical
fields and never re-judges the type. Every value carries evidence
(confidence, verbatim snippet, page); absent fields are omitted, never
guessed.

Quote-like types don't come here — they use `extractor.extract_lines`
(ProductLine + stated_total, the MultiCover model).
"""

from __future__ import annotations

import json

from records.core import DocTypeSchema, Field, FieldExtraction
from records.extract.extractor import ExtractionError
from records.extract.llm import LLMClient, LLMResponse, strip_fences

FIELDS_SYSTEM_PROMPT = """You are the extraction agent in a personal-records ingestion pipeline. This document has already been classified as the type named below — your job is ONLY to extract its canonical fields, with evidence for every value.

For each canonical field listed below that you can find in the document, emit an evidence object:
{"value": <the extracted value>, "confidence": <0.0-1.0>, "source_text": "<exact verbatim snippet the value came from>", "source_page": <1-based page, or null>}

Rules:
- "confidence" is your genuine certainty the value is correct AND complete. If the text is ambiguous, abbreviated, or garbled, score it low (< 0.8).
- Do NOT invent values. If a field is absent, omit its key entirely — never fabricate a low-confidence guess. Some canonical fields are commonly absent and that's not an error.
- Amounts are plain numbers (no currency symbols) in the document's stated currency. Dates are ISO strings (YYYY-MM-DD).
- Only extract the canonical fields listed — ignore everything else.

Respond with a single valid JSON object — no markdown fences, no surrounding text:

{
  "fields": {
    "<canonical field name>": <evidence object>,
    ...
  }
}"""


def _schema_summary(schema: DocTypeSchema) -> str:
    return json.dumps(
        {
            "doc_type": schema.doc_type,
            "description": schema.description,
            "canonical_fields": list(schema.canonical_fields),
            "required": list(schema.required),
        },
        indent=2,
    )


def extract_fields(
    document_text: str, doc_id: str, schema: DocTypeSchema, llm: LLMClient
) -> tuple[FieldExtraction, LLMResponse]:
    """Extract one document against its already-classified schema. Returns
    the FieldExtraction plus the raw LLM response (for telemetry).
    Unparseable responses raise ExtractionError; fields outside the schema's
    canonical set are dropped (the schema is the contract)."""
    system = FIELDS_SYSTEM_PROMPT + f"\n\n## DOCUMENT TYPE\n{_schema_summary(schema)}"
    response = llm.complete(
        system=system,
        user_content=(
            f"Document content:\n\n{document_text}\n\n"
            "Extract this document's canonical fields with per-field evidence and confidence."
        ),
        max_tokens=4096,
    )
    try:
        data = json.loads(strip_fences(response.text))
        canonical = set(schema.canonical_fields)
        fields = {
            name: Field(
                value=entry["value"],
                confidence=float(entry.get("confidence", 0.0)),
                source_text=str(entry.get("source_text", "")),
                source_page=entry.get("source_page"),
            )
            for name, entry in data["fields"].items()
            if name in canonical and isinstance(entry, dict) and "value" in entry
        }
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ExtractionError(f"unparseable field extraction for {doc_id}: {exc}") from exc
    return FieldExtraction(doc_id=doc_id, doc_type=schema.doc_type, fields=fields), response
