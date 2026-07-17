"""Wording chunker — section-aware chunking of policy wording documents for
cited coverage Q&A (ported from the old repo's `wording_chunker.py`, 2R.4).

Fixed-size chunking would split a clause from its exclusions — a "what is
covered" chunk that never saw its "what is not covered" neighbour is a wrong
answer waiting to happen. So this splits on the numbered-heading structure
insurers use ("Section 4 — Windscreen damage", "4.2 What is not covered"),
keeping each subsection as its own chunk while carrying its parent section's
title in `heading` for lineage. Falls back to paragraph-block grouping only
when no heading structure is detected at all.

`chunk_wording` is pure (text in, chunks out). `index_wording` is the write
path onto the JSONL chunk store under the data directory — the old repo's
`index_pending_wording` drained an `indexing_pending` flag asynchronously;
the new pipeline classifies at ingest, so indexing is a synchronous,
zero-LLM stage: `pipeline.ingest` calls it the moment a `reference_text`
document is stored. The content-hash doc_id doubles as `wording_version` —
a re-upload with different bytes is a genuinely new version by definition
(the document store dedups identical bytes to the same doc_id already), so
no separate version scheme is needed.

Every version's chunks stay in the store; retrieval is scoped to one
`wording_version`, so "only the latest wording answers coverage questions"
is a resolution-time concern, not an indexing one — see ADR 0002
(wording versioning: issuer/policy linking + supersedence, deferred).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from records.config import data_dir

MIN_CHUNK_WORDS = 100
MAX_CHUNK_WORDS = 400

PAGE_BREAK = "\x0c"  # form-feed: how paged PDF text is joined for chunking

# "Section 4 — Windscreen damage" / "Section 4: Windscreen damage" / "Section 4 - Windscreen damage"
_SECTION_RE = re.compile(r"^Section\s+(\d+)\s*[—:-]\s*(.+)$", re.IGNORECASE)
# "4.2 What is not covered"
_SUBSECTION_RE = re.compile(r"^(\d+)\.(\d+)\s+(.+)$")
# IPID-style question headings ("What is insured?", "Where am I covered?") —
# real insurer IPIDs carry no numbering at all, just a short question per
# subsection; each match starts a new (unnumbered) heading group.
_QUESTION_RE = re.compile(r"^(?:What|Where|When|How|Are|Is|Can|Do|Does|Will|Should)\b[^?]{0,100}\?$", re.IGNORECASE)
# Sentence boundary for splitting a paragraph that has no blank-line breaks
# of its own (common in PDF-extracted text) into smaller pieces.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def chunks_path(root: Path | None = None) -> Path:
    return (root or data_dir()) / "wording_chunks.jsonl"


def _word_count(text: str) -> int:
    return len(text.split())


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _split_long_paragraph(para: str, max_words: int) -> list[str]:
    """Break a single paragraph that's already over the target on its own —
    common when extracted PDF text has no blank lines to split on, so
    `_split_paragraphs` alone hands back one multi-thousand-word blob — into
    sentence-packed pieces, so the paragraph fallback never emits a chunk
    with no upper bound."""
    sentences = [s.strip() for s in _SENTENCE_RE.split(para) if s.strip()]
    if len(sentences) <= 1:
        # No sentence boundaries either (e.g. a dense table dump) — hard-split
        # on words as a last resort so nothing is left unbounded.
        words = para.split()
        return [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)] or [para]

    pieces: list[str] = []
    current: list[str] = []
    current_words = 0
    for sentence in sentences:
        words = _word_count(sentence)
        if current and current_words + words > max_words:
            pieces.append(" ".join(current))
            current, current_words = [], 0
        current.append(sentence)
        current_words += words
    if current:
        pieces.append(" ".join(current))
    return pieces


def _group_paragraphs(paragraphs: list[str], max_words: int = MAX_CHUNK_WORDS) -> list[str]:
    """Merge short paragraphs toward the target word band, and split any
    single paragraph that's already over it on its own; never merge across a
    heading boundary (callers only pass paragraphs from one section).

    Flushing is unconditional on hitting max_words — not gated on having
    reached MIN_CHUNK_WORDS first — because gating it can let a small
    leftover immediately followed by a near-max-sized paragraph push a group
    over the target; MIN_CHUNK_WORDS is a soft aim, max_words is a hard cap.
    """
    expanded: list[str] = []
    for para in paragraphs:
        if _word_count(para) > max_words:
            expanded.extend(_split_long_paragraph(para, max_words))
        else:
            expanded.append(para)

    groups: list[str] = []
    current: list[str] = []
    current_words = 0
    for para in expanded:
        words = _word_count(para)
        if current and current_words + words > max_words:
            groups.append("\n\n".join(current))
            current, current_words = [], 0
        current.append(para)
        current_words += words
    if current:
        groups.append("\n\n".join(current))
    return groups


def _make_chunk(doc_id: str, wording_version: str, section_ref: str | None, heading: str | None, page: int, text: str, index: int) -> dict:
    return {
        "chunk_id": f"{doc_id}-{index:03d}",
        "doc_id": doc_id,
        "wording_version": wording_version,
        "section_ref": section_ref,
        "heading": heading,
        "page": page,
        "text": text.strip(),
    }


def chunk_wording(text: str, doc_id: str, wording_version: str) -> list[dict]:
    """Split wording text into section-aware chunks.

    Two passes: first walk the text line-by-line, splitting on section/
    subsection headings into (section_ref, heading, page, body) groups —
    this is what preserves section lineage and keeps "what is covered" and
    "what is not covered" apart. Second, within each group, merge/split
    paragraphs toward the 100-400 word target — never across a heading
    boundary, since that's the split that produces a wrong answer.
    """
    text_lines = text.replace(PAGE_BREAK, f"\n{PAGE_BREAK}\n").split("\n")

    groups: list[dict] = []
    section_title: str | None = None
    question_count = 0
    page = 1
    current = {"section_ref": None, "heading": None, "page": page, "lines": []}

    def flush_current(next_ref, next_heading):
        nonlocal current
        if any(line.strip() for line in current["lines"]):
            groups.append(current)
        current = {"section_ref": next_ref, "heading": next_heading, "page": page, "lines": []}

    for line in text_lines:
        stripped = line.strip()
        if stripped == PAGE_BREAK:
            page += 1
            continue
        section_match = _SECTION_RE.match(stripped)
        if section_match:
            section_title = section_match.group(2).strip()
            flush_current(section_match.group(1), section_title)
            continue
        subsection_match = _SUBSECTION_RE.match(stripped)
        if subsection_match:
            sec_num, sub_num, sub_title = subsection_match.groups()
            heading = f"Section {sec_num} — {section_title} / {sub_title.strip()}" if section_title else sub_title.strip()
            flush_current(f"{sec_num}.{sub_num}", heading)
            continue
        if _QUESTION_RE.match(stripped):
            # IPID-style documents carry no numbering at all — each question
            # is its own flat, unnumbered heading (e.g. "What is insured?").
            question_count += 1
            flush_current(f"q{question_count}", stripped)
            continue
        current["lines"].append(line)
    flush_current(None, None)  # final flush; the fresh `current` it creates is discarded

    has_headings = any(g["section_ref"] is not None for g in groups)

    chunks: list[dict] = []
    if not has_headings:
        # No heading structure detected anywhere in the document -> plain
        # paragraph-block grouping is the best we can do.
        for para_group in _group_paragraphs(_split_paragraphs(text.replace(PAGE_BREAK, "\n"))):
            chunks.append(_make_chunk(doc_id, wording_version, None, None, 1, para_group, len(chunks)))
        return chunks

    for group in groups:
        body = "\n".join(group["lines"]).strip()
        if not body:
            continue
        for para_group in _group_paragraphs(_split_paragraphs(body)):
            chunks.append(
                _make_chunk(doc_id, wording_version, group["section_ref"], group["heading"], group["page"], para_group, len(chunks))
            )
    return chunks


def _append_chunks(chunks: list[dict], path: Path) -> None:
    if not chunks:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk) + "\n")


def _already_indexed(wording_version: str, path: Path) -> bool:
    if not path.exists():
        return False
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            if chunk.get("wording_version") == wording_version:
                return True
    return False


def index_wording(text: str, doc_id: str, *, root: Path | None = None) -> list[dict]:
    """Chunk one wording document and append its chunks to the chunk store.
    Idempotent per version: a doc_id whose chunks are already in the store is
    skipped (returns the empty list), so a re-run never duplicates search
    results. The pipeline's content-hash dedup makes this a belt-and-braces
    guard rather than the primary defence."""
    path = chunks_path(root)
    if _already_indexed(doc_id, path):
        return []
    chunks = chunk_wording(text, doc_id, doc_id)
    _append_chunks(chunks, path)
    return chunks
