"""Coverage answerer — "am I covered for X?" answered with a mandatory
clause citation (ported from the old repo's `coverage_answerer.py` +
`bedrock_client.COVERAGE_SYSTEM_PROMPT`, 2R.4; the LLM call now goes through
the `LLMClient` port instead of Bedrock).

Governing rule: no retrieved basis → refuse, never guess. Enforced twice,
deliberately:

  1. Upstream, in `wording_index.search` — below `MIN_SCORE_FLOOR` it returns
     no chunks at all, so this module never even asks the LLM to answer
     something nothing supports.
  2. Here, as a deterministic post-check on what the LLM comes back with —
     every `citation.quote` must appear verbatim in its cited chunk's text,
     and `citations` must be non-empty unless the verdict is
     `cannot_determine`. The LLM proposes a verdict and picks its evidence;
     it does not get to self-certify that the evidence actually says what it
     claims. A violation is swapped for the same honest refusal a human
     would want instead of a fabricated citation.

`trust` is always "interpreted" — a synthesised answer is never auto-trusted
like an extracted field, whether it succeeds or refuses. The vocabulary is
the one trust ladder in `core/trust.py` (reconciled at 2R.5).
"""

from __future__ import annotations

import json

from records.core import TRUST_INTERPRETED
from records.extract import LLMClient, LLMResponse, strip_fences

VERDICT_COVERED = "covered"
VERDICT_NOT_COVERED = "not_covered"
VERDICT_CONDITIONAL = "conditional"
VERDICT_CANNOT_DETERMINE = "cannot_determine"

VALID_VERDICTS = {VERDICT_COVERED, VERDICT_NOT_COVERED, VERDICT_CONDITIONAL, VERDICT_CANNOT_DETERMINE}

REFUSAL_ANSWER = "I can't find this in your policy wording."

COVERAGE_SYSTEM_PROMPT = """You answer motor insurance coverage questions using ONLY the policy wording clauses provided below. You have no other insurance knowledge to draw on — if the provided clauses don't address the question, say so; never guess or reason from outside knowledge.

## YOUR TASK
Given the question, the policyholder's key policy facts, and a set of retrieved wording clauses (each with a chunk_id and section_ref), pick exactly one verdict:

- "covered": a retrieved clause directly addresses this question and says the policy pays out / includes it, unconditionally.
- "not_covered": a retrieved clause directly addresses this question and says it is excluded / not paid / not included.
- "conditional": a retrieved clause directly addresses this question but the answer depends on stated conditions (age, add-ons, approved repairer, named driver, time limits, etc.) — list every condition you find.
- "cannot_determine": reserved ONLY for when the retrieved clauses genuinely do not address this question at all — there is nothing here to interpret, not even indirectly.

**Do not use "cannot_determine" as a hedge.** If a retrieved clause speaks to the question — even if it takes some interpretation, uses different wording than the question, or requires you to combine what's covered with what's excluded — you MUST commit to "covered", "not_covered", or "conditional". Only fall back to "cannot_determine" when none of the retrieved clauses are actually about this topic (e.g. the question asks about a jetski and the clauses are all about cars).

Every claim you make MUST be backed by a citation: the exact chunk_id, its section_ref, and a quote. The quote MUST be copied character-for-character from that chunk's "text" field — do not paraphrase, summarize, reword, or fix wording even slightly. Citations are checked mechanically downstream: the quote string must appear as an exact substring of the cited chunk's text, or the whole answer is thrown away and replaced with a refusal. If you cannot find an exact substring of the chunk text that supports your verdict, that clause does not actually support it — reconsider your verdict instead of inventing a quote. If verdict is "cannot_determine", citations must be an empty list.

Respond with a single valid JSON object — no markdown fences, no surrounding text:

{
  "verdict": "covered" | "not_covered" | "conditional" | "cannot_determine",
  "answer": "<1-3 sentence plain-English answer>",
  "citations": [{"chunk_id": "<id>", "section_ref": "<ref>", "quote": "<verbatim quote from that chunk>"}],
  "conditions": ["<condition 1>", "..."]
}"""


def _refusal(reason: str, answer: str = REFUSAL_ANSWER) -> dict:
    return {
        "verdict": VERDICT_CANNOT_DETERMINE,
        "answer": answer,
        "citations": [],
        "conditions": [],
        "trust": TRUST_INTERPRETED,
        "refusal_reason": reason,
    }


def _citations_valid(citations: list[dict], chunk_by_id: dict) -> bool:
    if not citations:
        return False
    for citation in citations:
        chunk = chunk_by_id.get(citation.get("chunk_id"))
        if chunk is None:
            return False
        quote = citation.get("quote") or ""
        if not quote or quote not in chunk["text"]:
            return False
    return True


def _user_content(question: str, policy_facts: dict, chunks: list[dict]) -> str:
    clause_summary = [
        {"chunk_id": c["chunk_id"], "section_ref": c["section_ref"], "heading": c.get("heading"), "text": c["text"]}
        for c in chunks
    ]
    return (
        f"Question: {question}\n\n"
        f"Policyholder's key facts: {json.dumps(policy_facts or {}, default=str)}\n\n"
        f"Retrieved wording clauses:\n{json.dumps(clause_summary, indent=2)}"
    )


def answer_coverage_question(
    question: str, policy_facts: dict, chunks: list[dict], llm: LLMClient
) -> tuple[dict, LLMResponse | None]:
    """Answer a coverage question against already-retrieved wording chunks
    (from `wording_index.search`). Returns ({"verdict", "answer",
    "citations", "conditions", "trust", "refusal_reason"}, llm_response);
    the response is None when the refusal happened before any LLM call, and
    is returned so the caller can record telemetry (the caller owns
    telemetry, as everywhere else)."""
    if not chunks:
        # No retrieved basis at all — refuse before even asking the LLM.
        return _refusal("no_retrieval"), None

    response = llm.complete(
        system=COVERAGE_SYSTEM_PROMPT,
        user_content=_user_content(question, policy_facts, chunks),
        max_tokens=1024,
    )
    try:
        raw = json.loads(strip_fences(response.text))
    except json.JSONDecodeError:
        return _refusal("unparseable_response"), response
    if not isinstance(raw, dict):
        return _refusal("unparseable_response"), response

    verdict = raw.get("verdict")
    if verdict not in VALID_VERDICTS:
        return _refusal("invalid_verdict"), response

    if verdict == VERDICT_CANNOT_DETERMINE:
        return _refusal("llm_cannot_determine", answer=raw.get("answer") or REFUSAL_ANSWER), response

    chunk_by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
    citations = raw.get("citations") or []
    if not _citations_valid(citations, chunk_by_id):
        return _refusal("citation_check_failed"), response

    return {
        "verdict": verdict,
        "answer": raw.get("answer", ""),
        "citations": citations,
        "conditions": raw.get("conditions") or [],
        "trust": TRUST_INTERPRETED,
        "refusal_reason": None,
    }, response
