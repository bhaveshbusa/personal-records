"""Trust status — the provenance ladder stamped on every fact (ported from
the old repo's `trust.py`, reconciled with the typed model at 2R.5).

Not a confidence score (that's the extractor's float, per `Field`). Trust is
a three-state ladder describing how a value came to be in the system, low to
high:

    interpreted < extracted < verified

`extracted` is the default the moment the deterministic rules accept an
LLM extraction — the LLM read it, a human hasn't looked. `verified` is
earned only when a human confirms from the review queue. `interpreted` is
an LLM's answer synthesised from retrieved evidence (the coverage Q&A
response) — never a stored fact, always the weakest rung.

Reconciliation with the new model (the 2R.5 decision): the old repo stamped
trust per *field*; here trust lives per *event*. The new review queue
confirms whole extractions — there is no per-field confirm path — so
per-field trust strings could never disagree within one event, and field
granularity is already carried by each `Field`'s confidence + source_text.
One string per event is the honest resolution. `min_trust` survives for
answers composed from several events: record-level trust is the weakest
link.
"""

from __future__ import annotations

TRUST_VERIFIED = "verified"
TRUST_EXTRACTED = "extracted"
TRUST_INTERPRETED = "interpreted"

# Low -> high, so `min` over this rank picks the weakest link.
_RANK = {TRUST_INTERPRETED: 0, TRUST_EXTRACTED: 1, TRUST_VERIFIED: 2}


def min_trust(trust_values) -> str:
    """Record-level trust = the least-trusted contributing fact. Defaults to
    `extracted` (the neutral pending state) when there is nothing to rank;
    unknown strings rank as `extracted` rather than crashing a projection."""
    values = list(trust_values)
    if not values:
        return TRUST_EXTRACTED
    return min(values, key=lambda t: _RANK.get(t, _RANK[TRUST_EXTRACTED]))
