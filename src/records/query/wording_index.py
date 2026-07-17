"""Wording retrieval — hand-rolled BM25 over chunked policy wording (ported
from the old repo's `wording_index.py`, 2R.4).

Deterministic and offline-testable on purpose: the coverage answerer must be
able to refuse rather than guess when nothing relevant comes back, and that
decision has to be reproducible without an LLM or a network call. No
embeddings — BM25 over the fixture already ranks the right section first for
direct and near-direct phrasing; only reach for embeddings if the eval
(2R.6) shows BM25 failing on adversarial phrasing.

Chunks are read from the same JSONL file `wording_chunker.index_wording`
writes to — this module never writes to it.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from records.query.wording_chunker import chunks_path

K1 = 1.5
B = 0.75

DEFAULT_K = 4

# Below this BM25 score, a result is noise, not a match — search() drops it,
# which is what lets the coverage answerer refuse instead of citing an
# unrelated clause.
MIN_SCORE_FLOOR = 0.5

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "for", "and", "or", "not", "no", "do", "does",
    "did", "if", "it", "this", "that", "i", "you", "your", "my", "we",
    "our", "as", "by", "with", "at", "from", "will", "am", "me",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Terms that describe the retrieval task rather than the subject the user is
# asking about.  Strict callers use the remaining terms as a deterministic
# lexical relevance check after BM25 ranking.
_QUERY_BOILERPLATE = {
    "what",
    "which",
    "policy",
    "wording",
    "clause",
    "clauses",
    "relevant",
    "cover",
    "covered",
    "coverage",
}


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _substantive_query_terms(question: str) -> set[str]:
    """Return distinct topic terms, excluding retrieval boilerplate."""
    return set(_tokenize(question)) - _QUERY_BOILERPLATE


def _passes_substantive_term_gate(
    substantive_terms: set[str], doc_tokens: list[str]
) -> bool:
    """Require one topic match for one-term queries, otherwise require two.

    A query containing no substantive terms cannot establish relevance.
    """
    required_matches = min(2, len(substantive_terms))
    if required_matches == 0:
        return False
    return len(substantive_terms.intersection(doc_tokens)) >= required_matches


def _load_chunks(wording_version: str, path: Path) -> list[dict]:
    if not path.exists():
        return []
    chunks = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("wording_version") == wording_version:
                chunks.append(chunk)
    return chunks


def _bm25_scores(query_tokens: list[str], doc_tokens_list: list[list[str]]) -> list[float]:
    n_docs = len(doc_tokens_list)
    if n_docs == 0 or not query_tokens:
        return [0.0] * n_docs

    avg_doc_len = sum(len(doc) for doc in doc_tokens_list) / n_docs
    doc_freq = Counter()
    for doc in doc_tokens_list:
        doc_freq.update(set(doc))
    idf = {term: math.log((n_docs - freq + 0.5) / (freq + 0.5) + 1) for term, freq in doc_freq.items()}

    scores = []
    for doc in doc_tokens_list:
        term_freq = Counter(doc)
        doc_len = len(doc)
        score = 0.0
        for term in query_tokens:
            freq = term_freq.get(term, 0)
            if freq == 0:
                continue
            numerator = idf.get(term, 0.0) * freq * (K1 + 1)
            denominator = freq + K1 * (1 - B + B * doc_len / avg_doc_len)
            score += numerator / denominator
        scores.append(score)
    return scores


def search(
    question: str,
    wording_version: str,
    k: int = DEFAULT_K,
    *,
    root: Path | None = None,
    min_score_floor: float = MIN_SCORE_FLOOR,
    strict: bool = False,
) -> list[tuple[dict, float]]:
    """Rank chunks of `wording_version` against `question` with BM25.
    Returns up to `k` (chunk, score) pairs, highest score first, with any
    result below `min_score_floor` dropped — an empty list is the signal the
    upstream answerer refuses on, so it must mean "nothing relevant", not
    "nothing above zero".

    With ``strict=True``, results must also match substantive query terms:
    one distinct term for a one-term query, or at least two for a multi-term
    query. Retrieval boilerplate such as "policy wording clauses" is ignored.
    The default remains the original BM25-only behaviour for the LLM-backed
    CLI coverage flow."""
    chunks = _load_chunks(wording_version, chunks_path(root))
    if not chunks:
        return []

    query_tokens = _tokenize(question)
    doc_tokens_list = [_tokenize(f"{chunk.get('heading') or ''} {chunk['text']}") for chunk in chunks]
    scores = _bm25_scores(query_tokens, doc_tokens_list)

    ranked = sorted(
        zip(chunks, scores, doc_tokens_list), key=lambda item: item[1], reverse=True
    )
    if strict:
        substantive_terms = _substantive_query_terms(question)
        ranked = [
            item
            for item in ranked
            if _passes_substantive_term_gate(substantive_terms, item[2])
        ]
    return [
        (chunk, score)
        for chunk, score, _doc_tokens in ranked[:k]
        if score >= min_score_floor
    ]
