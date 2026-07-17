"""Agentic query router — intent classification in front of deterministic tools.

The one LLM call in the query path classifies *what kind of question* this
is and *which product* it's about; a deterministic tool then answers from
the event log, and a deterministic formatter renders the reply with
provenance. A misclassified intent can pick the wrong tool, but it can
never fabricate a fact — the tools only read the log.

Fail-safe: any unparseable or unknown classification becomes intent
"unknown", answered with what the system *can* do rather than a guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from records.core import current_policies, replay
from records.extract import LLMClient, record_llm_usage, strip_fences
from records.query.coverage_answerer import answer_coverage_question
from records.query.quote_profile import export_profile, profile_completeness, quote_profile
from records.query.tools import (
    missing_info_answer,
    premium_answer,
    premium_history_answer,
    quote_comparison_answer,
    renewal_answer,
)
from records.query.wording_documents import current_wording_document
from records.query.wording_index import search
from records.review import queue
from records.store import list_documents

INTENT_RENEWAL = "renewal_date"
INTENT_PREMIUM = "premium"
INTENT_HISTORY = "premium_history"
INTENT_MISSING = "missing_info"
INTENT_COVERAGE = "coverage"
INTENT_COMPARISON = "quote_comparison"
INTENT_PROFILE = "quote_profile"
INTENT_UNKNOWN = "unknown"

_VALID_INTENTS = {
    INTENT_RENEWAL,
    INTENT_PREMIUM,
    INTENT_HISTORY,
    INTENT_MISSING,
    INTENT_COVERAGE,
    INTENT_COMPARISON,
    INTENT_PROFILE,
}

INTENT_SYSTEM_PROMPT = """You classify one question about a user's personal records into an intent and an optional product filter. You do NOT answer the question — deterministic tools do that from the user's own data.

Intents:
- "renewal_date": when a policy renews / expires / is due ("when does my car insurance renew?")
- "premium": what a policy currently costs ("how much is my home insurance?")
- "premium_history": how a cost changed over time ("did my premium go up?", "compare my renewals")
- "missing_info": what records or fields are missing or awaiting review ("what am I missing?", "anything stuck?")
- "coverage": whether the policy covers something — a policy-wording question ("am I covered for a cracked windscreen?", "can I drive other cars?", "does my policy include a courtesy car?")
- "quote_comparison": a renewal quote vs the current policy ("how does this quote compare to my current policy?", "is my renewal offer higher than what I pay now?")
- "quote_profile": getting quote-ready / switching ("get me ready to re-quote", "export my details for a comparison site")
- "unknown": none of the above plausibly fits. Never force a fit.

"product" is a short lowercase slug ("motor", "home", ...) if the question names one ("car" → "motor"), else null.

Respond with a single valid JSON object — no markdown fences, no surrounding text:

{"intent": "<one of the eight>", "product": "<slug or null>"}"""


@dataclass(frozen=True)
class Answer:
    text: str
    intent: str
    sources: tuple[str, ...] = ()


def classify_intent(question: str, llm: LLMClient) -> tuple[str, str | None, object]:
    response = llm.complete(system=INTENT_SYSTEM_PROMPT, user_content=question, max_tokens=256)
    try:
        data = json.loads(strip_fences(response.text))
        intent = data["intent"]
        product = data.get("product") or None
        if intent not in _VALID_INTENTS:
            return INTENT_UNKNOWN, None, response
        return intent, product, response
    except (json.JSONDecodeError, KeyError, TypeError):
        return INTENT_UNKNOWN, None, response


def _fmt_renewal(result: dict, product: str | None) -> str:
    if not result["found"]:
        target = f"'{product}'" if product else "any product"
        return f"No confirmed renewal records for {target} yet. Ingest a document first."
    lines = []
    for r in result["rows"]:
        when = r["renewal_date"] or "an unknown date"
        days = f" ({r['days_left']} days)" if r["days_left"] is not None else ""
        lines.append(
            f"{r['product']}: renews {when}{days} — status {r['status']}, "
            f"£{r['annual_premium']:.2f} [{r['state']}] (source: {r['doc_id'][:12]}…)"
        )
    return "\n".join(lines)


def _fmt_premium(result: dict, product: str | None) -> str:
    if not result["found"]:
        target = f"'{product}'" if product else "any product"
        return f"No premium on record for {target} yet."
    return "\n".join(
        f"{p['product']}: £{p['annual_premium']:.2f} [{p['state']}] (source: {p['doc_id'][:12]}…)"
        for p in result["premiums"]
    )


def _fmt_history(result: dict, product: str | None) -> str:
    if not result["found"]:
        target = f"'{product}'" if product else "any product"
        return f"No premium history for {target} yet."
    lines = []
    for p in result["products"]:
        if not p["steps"]:
            lines.append(
                f"{p['product']}: one observation only — £{p['latest_premium']:.2f}; "
                "no year-on-year change to compute yet."
            )
            continue
        for step in p["steps"]:
            pct = f"{step['change_pct']:+.1f}%" if step["change_pct"] is not None else "n/a"
            lines.append(
                f"{p['product']}: £{step['from']:.2f} → £{step['to']:.2f} ({pct}) "
                f"(sources: {step['sources'][0][:8]}…, {step['sources'][1][:8]}…)"
            )
    return "\n".join(lines)


def _policy_facts(events: list) -> dict:
    """Key facts of the current policy, as context for the coverage LLM call.
    Optional on purpose — a deliberate deviation from the old repo, which
    refused with "no policy on file": in the event model the wording itself
    is the evidence a coverage answer cites, so a user who has ingested
    only their wording still gets a cited answer, with fewer personal facts
    in the prompt."""
    rows = current_policies(events)
    if not rows:
        return {}
    row = rows[0]  # single-policy default; multi-policy selection is 2R.5+ work

    def value(name):
        field = row["fields"].get(name)
        return field.value if field else None

    return {
        "provider": row["provider"],
        "cover_level": value("cover_level"),
        "compulsory_excess": value("compulsory_excess"),
        "voluntary_excess": value("voluntary_excess"),
    }


def _coverage(question: str, llm: LLMClient, *, root: Path | None) -> Answer:
    """COVERAGE route: deterministic BM25 retrieval from the wording on
    file, one LLM call to interpret the retrieved clauses, deterministic
    citation post-check (in `coverage_answerer`) before anything is shown.
    Refuses honestly at every point evidence is missing: no wording on
    file, nothing relevant retrieved, or citations that don't check out."""
    wording_doc = current_wording_document(root=root)
    if wording_doc is None:
        return Answer(
            "No policy wording on file — ingest your policy wording (or IPID) document "
            "and I can answer coverage questions from it, with citations.",
            INTENT_COVERAGE,
        )

    chunks = [chunk for chunk, _score in search(question, wording_doc["doc_id"], root=root)]
    result, response = answer_coverage_question(question, _policy_facts(replay(root=root)), chunks, llm)
    if response is not None:
        record_llm_usage("answer_coverage", response, root=root)

    sections = sorted({c["section_ref"] for c in result["citations"] if c.get("section_ref")})
    text = result["answer"]
    if result["citations"]:
        cites = "; ".join(
            f"§{c['section_ref']}: “{c['quote']}”" if c.get("section_ref") else f"“{c['quote']}”"
            for c in result["citations"]
        )
        text += (
            f"\n  cited wording ({', '.join(sections) or 'unsectioned'}): {cites}"
            f"\n  (source: {wording_doc['doc_id'][:12]}… [{result['trust']}])"
        )
    if result["conditions"]:
        text += "\n  conditions: " + "; ".join(result["conditions"])
    sources = (wording_doc["doc_id"],) if result["citations"] else ()
    return Answer(text, INTENT_COVERAGE, sources)


def _fmt_comparison(result: dict, product: str | None) -> str:
    if not result["found"]:
        target = f"'{product}'" if product else "any product"
        return (
            f"No live renewal quote on record for {target} — ingest a renewal "
            "quote and I can compare it to your current policy."
        )
    lines = []
    for offer in result["offers"]:
        change = offer["premium_change"]
        if change["comparable"]:
            sign = "+" if change["delta"] >= 0 else ""
            lines.append(
                f"{offer['product']}: quoted £{change['latest']:.2f} vs current "
                f"£{change['previous']:.2f} — {sign}£{change['delta']:.2f} "
                f"({sign}{change['pct_change']}%) [{offer['trust']}] "
                f"(quote: {offer['doc_id'][:12]}…, current: {offer['current_policy_doc_id'][:12]}…)"
            )
        else:
            lines.append(
                f"{offer['product']}: quoted £{offer['quoted_premium']:.2f} — not comparable: "
                f"{change['reason']} (quote: {offer['doc_id'][:12]}…)"
            )
    return "\n".join(lines)


def _fmt_missing(result: dict) -> str:
    if result["empty"]:
        return "No records at all yet — ingest your first document."
    lines = []
    for gap in result["gaps"]:
        lines.append(f"{gap['product']}: {gap['gap']} (source: {gap['doc_id'][:12]}…)")
    for item in result["pending_review"]:
        reasons = "; ".join(item["reasons"])
        lines.append(f"awaiting review: {item['doc_id'][:12]}… — {reasons}")
    return "\n".join(lines) if lines else "Nothing missing: all records complete, review queue empty."


def ask(
    question: str, llm: LLMClient, *, root: Path | None = None, today: date | None = None
) -> Answer:
    """Answer one question from the user's own records, with provenance."""
    intent, product, response = classify_intent(question, llm)
    record_llm_usage("classify_intent", response, root=root)
    events = replay(root=root)

    if intent == INTENT_RENEWAL:
        result = renewal_answer(events, product, today)
        return Answer(_fmt_renewal(result, product), intent, tuple(result["sources"]))
    if intent == INTENT_PREMIUM:
        result = premium_answer(events, product, today)
        return Answer(_fmt_premium(result, product), intent, tuple(result["sources"]))
    if intent == INTENT_HISTORY:
        result = premium_history_answer(events, product)
        sources = tuple(s for p in result["products"] for s in p["sources"])
        return Answer(_fmt_history(result, product), intent, sources)
    if intent == INTENT_MISSING:
        result = missing_info_answer(events, queue.list_pending(root=root), today)
        return Answer(_fmt_missing(result), intent)
    if intent == INTENT_COVERAGE:
        return _coverage(question, llm, root=root)
    if intent == INTENT_COMPARISON:
        result = quote_comparison_answer(events, product)
        return Answer(_fmt_comparison(result, product), intent, tuple(result["sources"]))
    if intent == INTENT_PROFILE:
        profile = quote_profile(events, list_documents(root=root), today)
        paths = export_profile(profile, root=root)
        populated, total = profile_completeness(profile)
        sources = tuple(
            {
                s
                for group in profile.values()
                for entry in group.values()
                for s in (entry["source_doc_id"] if isinstance(entry["source_doc_id"], list) else [entry["source_doc_id"]])
                if s
            }
        )
        return Answer(
            f"Your quote-ready profile has {populated} of {total} fields populated.\n"
            f"  written to: {paths['markdown_path']}\n"
            f"  and:        {paths['json_path']}",
            intent,
            sources,
        )

    return Answer(
        "I couldn't map that question to your records. I can answer: when something renews, "
        "what it costs, how a premium changed over time, whether your policy covers something "
        "(from your policy wording), how a renewal quote compares to your current policy, "
        "a quote-ready profile for switching, and what's missing or awaiting review.",
        INTENT_UNKNOWN,
    )
