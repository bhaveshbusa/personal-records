"""Eval harness (2R.6) — per-stage scoring of the full ingestion pipeline.

Rebuilt from the old repo's `eval_runner.py` onto the new pipeline. The old
runner scored doc-type resolution, field-*name* drift, and required-field
coverage over real documents; two of those are now structurally impossible
(the extractor drops non-canonical keys; `field_issues` enforces required
fields), so the new harness scores what can still be wrong:

  1. classification — did the doc_type classifier pick the case's type?
  2. field accuracy — do extracted values match the fixture's ground truth?
     (fixtures are synthetic twins, so ground truth is known — value
     accuracy, not just name accuracy)
  3. routing        — did the document land where it should (accepted /
     review / stored), for the reasons expected?

Each case runs in its own fresh temp root — the same stance as the old
`eval_coverage.py` building its own scratch index: re-running the evals
never depends on (or pollutes) ingested state, and cases cannot interact
through dedup, prior-year premiums, or entity linking.

Scoring is pure (`score_case` takes an `IngestResult`); only `run_case`
touches the pipeline. Tests drive the whole harness offline through
`FakeLLMClient`; `records eval` drives it with the real Anthropic client —
that run is the actual measurement, per-stage, of the live model.

Usage:  records eval            (from the repo root; needs ANTHROPIC_API_KEY)
        records eval --cases path/to/cases.json --out results.csv
"""

from __future__ import annotations

import csv
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from records import pipeline
from records.core import Extraction, FieldExtraction
from records.extract import LLMClient

DEFAULT_CASES_PATH = Path("evals") / "cases.json"


@dataclass(frozen=True)
class EvalCase:
    """One fixture + its ground truth, loaded from the manifest."""

    name: str
    file: Path
    expected_doc_type: str
    expected_outcome: str  # "accepted" | "review" | "stored"
    expected_fields: dict = field(default_factory=dict)  # flat path: name -> value
    expected_lines: tuple = ()  # quote path: ({product, annual_premium, renewal_date?}, ...)
    expected_stated_total: float | None = None
    expect_reason_contains: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseScore:
    """Per-stage verdicts for one case. `fields_expected` of 0 means the
    case declares no ground-truth values (wording, evidence, unknown) and
    is excluded from the field-accuracy denominator."""

    name: str
    file: str
    expected_doc_type: str
    predicted_doc_type: str
    classification_ok: bool
    fields_expected: int
    fields_matched: int
    field_mismatches: tuple[str, ...]
    expected_outcome: str
    actual_outcome: str
    routing_ok: bool
    review_reasons: tuple[str, ...] = ()
    error: str = ""

    @property
    def passed(self) -> bool:
        return (
            self.classification_ok
            and self.routing_ok
            and self.fields_matched == self.fields_expected
            and not self.error
        )


def load_cases(path: str | Path) -> list[EvalCase]:
    path = Path(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for raw in manifest["cases"]:
        cases.append(
            EvalCase(
                name=raw["name"],
                file=(path.parent / raw["file"]).resolve(),
                expected_doc_type=raw["expected_doc_type"],
                expected_outcome=raw["expected_outcome"],
                expected_fields=raw.get("expected_fields", {}),
                expected_lines=tuple(raw.get("expected_lines", ())),
                expected_stated_total=raw.get("expected_stated_total"),
                expect_reason_contains=tuple(raw.get("expect_reason_contains", ())),
            )
        )
    return cases


def _norm(value) -> str:
    """Comparison key for non-numeric values: casefold, drop ALL whitespace
    (so 'XY19 ZAB' == 'xy19zab') and currency commas."""
    return "".join(str(value).split()).casefold().replace(",", "")


def _as_number(value) -> float | None:
    """Lenient numeric parse: 12, '12', '£1,192.99', '62 mm' → float."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lstrip("£$€").replace(",", "")
    for token in (text, text.split()[0] if text.split() else ""):
        try:
            return float(token)
        except ValueError:
            continue
    return None


def values_match(expected, actual) -> bool:
    """Lenient on formatting, strict on value."""
    expected_num = _as_number(expected)
    if expected_num is not None:
        actual_num = _as_number(actual)
        return actual_num is not None and abs(expected_num - actual_num) < 0.005
    return _norm(expected) == _norm(actual)


def _score_flat_fields(case: EvalCase, extraction) -> tuple[int, list[str]]:
    """Ground-truth check for the flat-field path. A wrong classification
    sends the document down another path (or none); its declared fields
    then score as unread — extraction accuracy is downstream of
    classification, exactly as in production."""
    extracted = extraction.fields if isinstance(extraction, FieldExtraction) else {}
    matched, mismatches = 0, []
    for name, expected in case.expected_fields.items():
        entry = extracted.get(name)
        if entry is None:
            mismatches.append(f"{name}: not extracted (expected {expected!r})")
        elif values_match(expected, entry.value):
            matched += 1
        else:
            mismatches.append(f"{name}: {entry.value!r} != expected {expected!r}")
    return matched, mismatches


def _score_lines(case: EvalCase, extraction) -> tuple[int, list[str]]:
    """Ground-truth check for the quote path: lines matched by product,
    each declared value (annual_premium, renewal_date) scored separately,
    plus the document-level stated_total when declared."""
    lines = (
        {line.product: line for line in extraction.lines}
        if isinstance(extraction, Extraction)
        else {}
    )
    matched, mismatches = 0, []
    for expected_line in case.expected_lines:
        product = expected_line["product"]
        line = lines.get(product)
        for key in ("annual_premium", "renewal_date"):
            if key not in expected_line:
                continue
            expected = expected_line[key]
            entry = getattr(line, key, None)
            if entry is None:
                mismatches.append(f"{product}.{key}: not extracted (expected {expected!r})")
            elif values_match(expected, entry.value):
                matched += 1
            else:
                mismatches.append(f"{product}.{key}: {entry.value!r} != expected {expected!r}")
    if case.expected_stated_total is not None:
        total = getattr(extraction, "stated_total", None) if isinstance(extraction, Extraction) else None
        if total is not None and values_match(case.expected_stated_total, total.value):
            matched += 1
        else:
            got = total.value if total is not None else None
            mismatches.append(f"stated_total: {got!r} != expected {case.expected_stated_total!r}")
    return matched, mismatches


def expected_field_count(case: EvalCase) -> int:
    n = len(case.expected_fields)
    n += sum(1 for line in case.expected_lines for key in ("annual_premium", "renewal_date") if key in line)
    n += 1 if case.expected_stated_total is not None else 0
    return n


def score_case(case: EvalCase, result: pipeline.IngestResult) -> CaseScore:
    """Pure per-stage scoring of one pipeline result against ground truth."""
    predicted = result.doc_type or ""
    classification_ok = predicted == case.expected_doc_type

    flat_matched, flat_mismatches = _score_flat_fields(case, result.extraction)
    line_matched, line_mismatches = _score_lines(case, result.extraction)

    reasons_text = "\n".join(result.review_reasons)
    routing_ok = result.outcome == case.expected_outcome and all(
        needle in reasons_text for needle in case.expect_reason_contains
    )

    return CaseScore(
        name=case.name,
        file=case.file.name,
        expected_doc_type=case.expected_doc_type,
        predicted_doc_type=predicted,
        classification_ok=classification_ok,
        fields_expected=expected_field_count(case),
        fields_matched=flat_matched + line_matched,
        field_mismatches=tuple(flat_mismatches + line_mismatches),
        expected_outcome=case.expected_outcome,
        actual_outcome=result.outcome,
        routing_ok=routing_ok,
        review_reasons=result.review_reasons,
    )


def run_case(case: EvalCase, llm: LLMClient) -> CaseScore:
    """Ingest one fixture in a fresh, throwaway data root and score it. Any
    crash degrades the case to a scored failure — the run always completes
    (the old runner's stance)."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = pipeline.ingest(case.file, llm, root=Path(tmp))
        return score_case(case, result)
    except Exception as exc:  # noqa: BLE001 — one bad case must not sink the report
        return CaseScore(
            name=case.name,
            file=case.file.name,
            expected_doc_type=case.expected_doc_type,
            predicted_doc_type="",
            classification_ok=False,
            fields_expected=expected_field_count(case),
            fields_matched=0,
            field_mismatches=(),
            expected_outcome=case.expected_outcome,
            actual_outcome="error",
            routing_ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_evals(cases: list[EvalCase], llm: LLMClient) -> list[CaseScore]:
    return [run_case(case, llm) for case in cases]


def summarize(scores: list[CaseScore]) -> dict:
    """Per-stage aggregates. Classification and routing are per-case;
    field accuracy is micro-averaged over every declared ground-truth value."""
    field_cases = [s for s in scores if s.fields_expected]
    expected = sum(s.fields_expected for s in field_cases)
    matched = sum(s.fields_matched for s in field_cases)
    return {
        "cases": len(scores),
        "classification_correct": sum(s.classification_ok for s in scores),
        "routing_correct": sum(s.routing_ok for s in scores),
        "fields_expected": expected,
        "fields_matched": matched,
        "field_cases": len(field_cases),
        "passed": sum(s.passed for s in scores),
        "errors": sum(1 for s in scores if s.error),
    }


CSV_FIELDS = [
    "case", "file", "expected_doc_type", "predicted_doc_type", "classification_ok",
    "fields_expected", "fields_matched", "field_mismatches",
    "expected_outcome", "actual_outcome", "routing_ok", "review_reasons", "error", "passed",
]


def write_csv(scores: list[CaseScore], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for s in scores:
            writer.writerow(
                {
                    "case": s.name,
                    "file": s.file,
                    "expected_doc_type": s.expected_doc_type,
                    "predicted_doc_type": s.predicted_doc_type,
                    "classification_ok": int(s.classification_ok),
                    "fields_expected": s.fields_expected,
                    "fields_matched": s.fields_matched,
                    "field_mismatches": " | ".join(s.field_mismatches),
                    "expected_outcome": s.expected_outcome,
                    "actual_outcome": s.actual_outcome,
                    "routing_ok": int(s.routing_ok),
                    "review_reasons": " | ".join(s.review_reasons),
                    "error": s.error,
                    "passed": int(s.passed),
                }
            )


def format_report(scores: list[CaseScore]) -> str:
    """The per-stage report the 2R.6 gate asks for, as printable text."""
    lines = []
    for s in scores:
        verdict = "PASS" if s.passed else "FAIL"
        lines.append(
            f"  {verdict}  {s.name:<24} {s.expected_doc_type:>23} -> {s.predicted_doc_type or '?':<23}"
            f" {s.actual_outcome:<9} fields {s.fields_matched}/{s.fields_expected}"
        )
        for m in s.field_mismatches:
            lines.append(f"          field: {m}")
        if not s.routing_ok:
            lines.append(f"          routing: expected {s.expected_outcome!r}, got {s.actual_outcome!r}")
            for reason in s.review_reasons:
                lines.append(f"            - {reason}")
        if s.error:
            lines.append(f"          error: {s.error}")

    t = summarize(scores)

    def pct(n: int, d: int) -> str:
        return f"{n}/{d} ({n / d * 100:.0f}%)" if d else "n/a"

    lines += [
        "",
        "=" * 64,
        "  Per-stage results",
        f"    classification accuracy : {pct(t['classification_correct'], t['cases'])}",
        f"    extraction field accuracy: {pct(t['fields_matched'], t['fields_expected'])}"
        f"  [{t['field_cases']} case(s) with ground truth]",
        f"    routing correctness     : {pct(t['routing_correct'], t['cases'])}",
        f"  Overall: {pct(t['passed'], t['cases'])} cases pass all stages"
        + (f"  ({t['errors']} errored)" if t["errors"] else ""),
        "=" * 64,
    ]
    return "\n".join(lines)
