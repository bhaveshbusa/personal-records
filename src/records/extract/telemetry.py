"""LLM cost telemetry — disposable observability, never domain truth.

Ported idea from the old repo's `events.py` (drain_usage/cost_from_usage):
every LLM call's token usage is priced and appended to `telemetry.jsonl` —
a separate stream from `domain_events.jsonl` by design. Telemetry can be
deleted wholesale; domain events cannot.

Prices are indicative (USD per token, per model family) — good enough for
"what does a document cost to ingest", not for billing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from records.config import data_dir
from records.extract.llm import LLMResponse

# USD per token (input, output), keyed by substring of the model name.
_PRICE_TABLE = {
    "sonnet": (3.00 / 1_000_000, 15.00 / 1_000_000),
    "haiku": (1.00 / 1_000_000, 5.00 / 1_000_000),
    "opus": (5.00 / 1_000_000, 25.00 / 1_000_000),
}
_DEFAULT_PRICE = _PRICE_TABLE["sonnet"]


def _rates_for(model: str) -> tuple[float, float]:
    model = (model or "").lower()
    for key, rates in _PRICE_TABLE.items():
        if key in model:
            return rates
    return _DEFAULT_PRICE


def record_llm_usage(operation: str, response: LLMResponse, *, root: Path | None = None) -> dict:
    """Price one LLM call and append it to the telemetry stream."""
    in_rate, out_rate = _rates_for(response.model)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,  # e.g. "classify_shape", "extract_lines"
        "model": response.model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost_usd": round(response.input_tokens * in_rate + response.output_tokens * out_rate, 6),
    }
    path = (root or data_dir()) / "telemetry.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry
