"""Shape classification — the pipeline's first LLM judgment.

A separate call from extraction on purpose (ported stance from the old
repo): the shape verdict decides how the document may be interpreted
*before* extraction commits to it, so a bad guess is caught by shape
routing instead of silently distorting the extraction.

Fail-safe by construction: any malformed or implausible response collapses
to `Shape(unsure=True)`, which the rules route to review. The classifier
can never force a document through.
"""

from __future__ import annotations

import json

from records.core import RENEWAL_ALREADY_ACCEPTED, RENEWAL_PROPOSED, Shape
from records.extract.llm import LLMClient, LLMResponse, strip_fences

SHAPE_SYSTEM_PROMPT = """You are the document-shape classifier in a personal-records ingestion pipeline. You do NOT extract field values — you only identify the document's shape, which is checked before any extracted fact is accepted.

Determine:

1. "line_count": how many distinct product lines (separately priced covers, e.g. motor, home) this document describes. A single-policy document is line_count 1. A multi-cover bundle listing motor AND home cover is line_count 2.
2. "renewal_status":
   - "proposed" — the document offers a renewal the customer must actively accept (a quote or offer).
   - "already_accepted" — the renewal will proceed without customer action, e.g. an auto-renewal invitation stating the policy will renew automatically unless the customer objects. An auto-renewal invitation for the current period is NOT a future offer.
3. "unsure": set true if you cannot confidently determine the shape. Never force a fit — a misfiled document is worse than an unextracted one. When "unsure" is true the other fields are ignored.

Respond with a single valid JSON object — no markdown fences, no surrounding text:

{
  "line_count": <int>,
  "renewal_status": "proposed" | "already_accepted",
  "unsure": <bool>,
  "rationale": "<one sentence: what in the document indicated this shape>"
}"""

_VALID_STATUSES = {RENEWAL_PROPOSED, RENEWAL_ALREADY_ACCEPTED}


def classify_shape(document_text: str, llm: LLMClient) -> tuple[Shape, LLMResponse]:
    """Classify one document's shape. Returns the Shape plus the raw LLM
    response (for telemetry). Any parse or plausibility failure → unsure."""
    response = llm.complete(
        system=SHAPE_SYSTEM_PROMPT,
        user_content=f"Document content:\n\n{document_text}\n\nClassify this document's shape.",
        max_tokens=512,
    )
    try:
        data = json.loads(strip_fences(response.text))
        line_count = data["line_count"]
        renewal_status = data["renewal_status"]
        unsure = bool(data.get("unsure", False))
        if not isinstance(line_count, int) or line_count < 1 or renewal_status not in _VALID_STATUSES:
            return Shape(unsure=True), response
        return Shape(line_count=line_count, renewal_status=renewal_status, unsure=unsure), response
    except (json.JSONDecodeError, KeyError, TypeError):
        return Shape(unsure=True), response
