"""Eval harness (2R.6) — offline tests of the harness itself.

The harness's real runs use a live LLM; these tests drive it with scripted
FakeLLMClient responses to prove the machinery: manifest coverage of every
registry doc_type, per-stage scoring (a perfect run scores 100%; a
misclassification is charged to every downstream stage), lenient value
matching, degradation on error, and the CSV report.
"""

import json
import tempfile
import unittest
from pathlib import Path

from records import evals
from records.core import RAW_EVIDENCE, REFERENCE_TEXT, REGISTRY
from records.extract import FakeLLMClient

CASES_PATH = Path(__file__).parent.parent / "evals" / "cases.json"


def perfect_responses(case: evals.EvalCase) -> list[str]:
    """The canned LLM responses an ideal model would give for one case,
    built from the case's own ground truth."""
    responses = [
        json.dumps(
            {"doc_type": case.expected_doc_type, "confidence": 0.95, "rationale": "eval"}
        )
    ]
    schema = REGISTRY.get(case.expected_doc_type)
    if schema is None or schema.role in (REFERENCE_TEXT, RAW_EVIDENCE):
        return responses  # unknown / stored-as-is: classification is the only call

    def entry(value):
        return {"value": value, "confidence": 0.95, "source_text": str(value), "source_page": 1}

    if schema.quote_like:
        responses.append(
            json.dumps(
                {
                    "line_count": len(case.expected_lines),
                    "renewal_status": "proposed",
                    "unsure": False,
                    "rationale": "eval",
                }
            )
        )
        lines = []
        for expected in case.expected_lines:
            line = {"product": expected["product"], "annual_premium": entry(expected["annual_premium"])}
            if "renewal_date" in expected:
                line["renewal_date"] = entry(expected["renewal_date"])
            lines.append(line)
        total = (
            entry(case.expected_stated_total) if case.expected_stated_total is not None else None
        )
        responses.append(json.dumps({"lines": lines, "stated_total": total}))
    else:
        fields = {name: entry(value) for name, value in case.expected_fields.items()}
        responses.append(json.dumps({"fields": fields}))
    return responses


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.cases = evals.load_cases(CASES_PATH)

    def test_every_registry_doc_type_has_a_case(self):
        covered = {c.expected_doc_type for c in self.cases}
        missing = set(REGISTRY) - covered
        self.assertEqual(missing, set(), f"registry doc_types without an eval case: {missing}")

    def test_unknown_and_multicover_golden_cases_present(self):
        names = {c.name for c in self.cases}
        self.assertIn("multicover_golden", names)
        self.assertIn("unknown_leaflet", {c.name for c in self.cases if c.expected_doc_type == "unknown"})

    def test_every_fixture_file_exists_and_is_synthetic(self):
        for case in self.cases:
            self.assertTrue(case.file.is_file(), f"{case.name}: missing fixture {case.file}")
            if case.file.parent.name == "fixtures":  # eval-owned twins carry the marker
                self.assertIn(
                    "synthetic example",
                    case.file.read_text(encoding="utf-8"),
                    f"{case.name}: fixture must be marked synthetic",
                )


class ScoringTest(unittest.TestCase):
    def test_perfect_run_scores_100_on_every_stage(self):
        cases = evals.load_cases(CASES_PATH)
        llm = FakeLLMClient([r for case in cases for r in perfect_responses(case)])
        scores = evals.run_evals(cases, llm)
        self.assertEqual(llm.responses, [])  # every scripted call consumed
        for score in scores:
            self.assertTrue(
                score.passed,
                f"{score.name}: mismatches={score.field_mismatches} "
                f"outcome={score.actual_outcome} reasons={score.review_reasons} err={score.error}",
            )
        totals = evals.summarize(scores)
        self.assertEqual(totals["classification_correct"], totals["cases"])
        self.assertEqual(totals["routing_correct"], totals["cases"])
        self.assertEqual(totals["fields_matched"], totals["fields_expected"])
        self.assertGreater(totals["fields_expected"], 0)
        self.assertEqual(totals["errors"], 0)

    def test_misclassification_is_charged_to_every_downstream_stage(self):
        # The schedule case classified as a certificate: wrong type, wrong
        # path, wrong routing — and its declared fields score as unread.
        cases = {c.name: c for c in evals.load_cases(CASES_PATH)}
        case = cases["policy_schedule"]
        llm = FakeLLMClient(
            [
                json.dumps({"doc_type": "certificate", "confidence": 0.95, "rationale": "wrong"}),
                json.dumps({"fields": {}}),  # nothing extracted down the wrong path
            ]
        )
        score = evals.run_case(case, llm)
        self.assertFalse(score.classification_ok)
        self.assertFalse(score.routing_ok)  # required policy_number missing → review, not accepted
        self.assertEqual(score.fields_matched, 0)
        self.assertEqual(len(score.field_mismatches), score.fields_expected)
        self.assertFalse(score.passed)

    def test_wrong_value_and_missing_field_are_separate_mismatches(self):
        cases = {c.name: c for c in evals.load_cases(CASES_PATH)}
        case = cases["payslip"]
        responses = perfect_responses(case)
        extraction = json.loads(responses[1])
        extraction["fields"]["net_pay"]["value"] = 9999.99  # wrong value
        del extraction["fields"]["gross_pay"]  # missing field
        llm = FakeLLMClient([responses[0], json.dumps(extraction)])
        score = evals.run_case(case, llm)
        self.assertEqual(score.fields_matched, score.fields_expected - 2)
        mismatches = "\n".join(score.field_mismatches)
        self.assertIn("net_pay", mismatches)
        self.assertIn("not extracted", mismatches)
        self.assertTrue(score.routing_ok)  # still parked (no event vocabulary) as expected

    def test_pipeline_crash_degrades_the_case_not_the_run(self):
        cases = evals.load_cases(CASES_PATH)[:2]
        # First case consumes garbage-free responses; second starves the fake.
        llm = FakeLLMClient(perfect_responses(cases[0]))
        scores = evals.run_evals(cases, llm)
        self.assertTrue(scores[0].passed)
        self.assertFalse(scores[1].passed)
        self.assertEqual(scores[1].actual_outcome, "error")
        self.assertTrue(scores[1].error)

    def test_multicover_golden_expects_review_with_zero_events(self):
        cases = {c.name: c for c in evals.load_cases(CASES_PATH)}
        case = cases["multicover_golden"]
        llm = FakeLLMClient(perfect_responses(case))
        score = evals.run_case(case, llm)
        self.assertTrue(score.passed)
        self.assertEqual(score.actual_outcome, "review")
        self.assertIn("multi-line", "\n".join(score.review_reasons))


class ValueMatchingTest(unittest.TestCase):
    def test_numeric_lenience(self):
        self.assertTrue(evals.values_match(378.90, "£378.90"))
        self.assertTrue(evals.values_match(1192.99, "1,192.99"))
        self.assertTrue(evals.values_match(62, "62 mm"))
        self.assertTrue(evals.values_match("900000042", 900000042))
        self.assertFalse(evals.values_match(378.90, 378.80))

    def test_string_lenience(self):
        self.assertTrue(evals.values_match("XY19 ZAB", "xy19zab"))
        self.assertTrue(evals.values_match("pass", "PASS"))
        self.assertTrue(evals.values_match("SwiftSure Insurance Ltd", "swiftsure insurance ltd"))
        self.assertFalse(evals.values_match("2026-10-14", "2026-10-15"))
        self.assertFalse(evals.values_match("buy", "sell"))


class ReportTest(unittest.TestCase):
    def test_csv_and_report_cover_every_case(self):
        cases = evals.load_cases(CASES_PATH)
        llm = FakeLLMClient([r for case in cases for r in perfect_responses(case)])
        scores = evals.run_evals(cases, llm)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "results.csv"
            evals.write_csv(scores, out)
            rows = out.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(rows), len(cases) + 1)  # header + one row per case
        report = evals.format_report(scores)
        self.assertIn("classification accuracy", report)
        self.assertIn("extraction field accuracy", report)
        self.assertIn("routing correctness", report)
        for case in cases:
            self.assertIn(case.name, report)


if __name__ == "__main__":
    unittest.main()
