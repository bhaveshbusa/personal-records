"""Query layer: intent router + deterministic tools over the event log,
plus the wording Q&A stack (chunker → BM25 index → cited coverage answers,
2R.4)."""

from records.query.coverage_answerer import answer_coverage_question
from records.query.quote_profile import export_profile, quote_profile
from records.query.router import Answer, ask, classify_intent
from records.query.tools import (
    missing_info_answer,
    premium_answer,
    premium_history_answer,
    quote_comparison_answer,
    renewal_answer,
)
from records.query.wording_chunker import chunk_wording, index_wording
from records.query.wording_documents import current_wording_document
from records.query.wording_index import search

__all__ = [
    "Answer",
    "answer_coverage_question",
    "ask",
    "chunk_wording",
    "classify_intent",
    "current_wording_document",
    "export_profile",
    "index_wording",
    "missing_info_answer",
    "premium_answer",
    "premium_history_answer",
    "quote_comparison_answer",
    "quote_profile",
    "renewal_answer",
    "search",
]
