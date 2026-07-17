"""Document-type classification — the pipeline's first LLM judgment (2R.1).

Ported from the old repo's `doc_classifier.py` + Bedrock classification
prompt, onto the new model: decides which schema-registry type a document
is (or "unknown") *before* shape or extraction runs, so extraction never
guesses the type — it extracts against the single schema this stage
resolved. A deliberate stage boundary, not merged into extraction to save
tokens: a bad type guess is caught by review routing instead of silently
distorting the extraction.

Fail-safe by construction: any malformed response collapses to
`DocTypeVerdict("unknown", 0.0)`, which the rules route to review.
Threshold/registry routing itself lives in `review.rules.doc_type_issues`
(deterministic, no LLM).
"""

from __future__ import annotations

import json

from records.core import REGISTRY, UNKNOWN_DOC_TYPE, DocTypeSchema, DocTypeVerdict
from records.extract.llm import LLMClient, LLMResponse, strip_fences

DOC_TYPE_SYSTEM_PROMPT = """You classify a document against a fixed registry of known document types. You do not extract any fields — only identify the type.

For each candidate type below you're given its role and a one-sentence description. Pick the single best-matching type slug, or "unknown" if none plausibly match.

Marketing material, instructional guides, and promotional literature (e.g. a "what to do in an accident" leaflet, a product brochure, a how-to-claim flyer) are NOT automatically a specific registry type just because they share its vocabulary — a leaflet that mentions claims is not automatically claim_evidence, and a brochure that mentions cover is not automatically a policy record. Prefer "unknown" over forcing a fit: only pick a specific type when the document's actual purpose and content genuinely match that type's description, not merely its topic.

Respond with a single valid JSON object — no markdown fences, no surrounding text:

{
  "doc_type": "<slug from the registry, or \\"unknown\\">",
  "confidence": <0.0-1.0, your genuine certainty in this classification>,
  "rationale": "<one sentence: what in the document indicated this type>"
}"""


def _registry_summary(registry: dict[str, DocTypeSchema]) -> str:
    return json.dumps(
        {dt: {"role": s.role, "description": s.description} for dt, s in registry.items()},
        indent=2,
    )


def classify_doc_type(
    document_text: str,
    llm: LLMClient,
    registry: dict[str, DocTypeSchema] | None = None,
) -> tuple[DocTypeVerdict, LLMResponse]:
    """Classify one document's type against the registry. Returns the verdict
    plus the raw LLM response (for telemetry). Any parse or plausibility
    failure → "unknown" at confidence 0.0 — never force a fit."""
    registry = REGISTRY if registry is None else registry
    system = DOC_TYPE_SYSTEM_PROMPT + f"\n\n## KNOWN DOCUMENT TYPES\n{_registry_summary(registry)}"
    response = llm.complete(
        system=system,
        user_content=f"Document content:\n\n{document_text}\n\nClassify this document.",
        max_tokens=512,
    )
    try:
        data = json.loads(strip_fences(response.text))
        doc_type = data["doc_type"]
        confidence = float(data["confidence"])
        if not isinstance(doc_type, str) or not doc_type or not (0.0 <= confidence <= 1.0):
            return DocTypeVerdict(UNKNOWN_DOC_TYPE, 0.0, "implausible classifier response"), response
        return DocTypeVerdict(doc_type, confidence, str(data.get("rationale", ""))), response
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return DocTypeVerdict(UNKNOWN_DOC_TYPE, 0.0, "unparseable classifier response"), response
