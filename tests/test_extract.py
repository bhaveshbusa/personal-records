"""Extraction stages against the fake LLM client (offline, no keys)."""

import json
import tempfile
import unittest
from pathlib import Path

from records.core import RENEWAL_ALREADY_ACCEPTED, RENEWAL_PROPOSED, Shape
from records.extract import (
    ExtractionError,
    FakeLLMClient,
    classify_shape,
    extract_lines,
    record_llm_usage,
)
from records.review import decide

EXAMPLES = Path(__file__).parent.parent / "examples"

MULTICOVER_SHAPE_RESPONSE = json.dumps(
    {
        "line_count": 2,
        "renewal_status": "already_accepted",
        "unsure": False,
        "rationale": "Lists motor and home covers; renews automatically unless contacted.",
    }
)

MULTICOVER_EXTRACTION_RESPONSE = json.dumps(
    {
        "lines": [
            {
                "product": "motor",
                "annual_premium": {"value": 412.50, "confidence": 0.95, "source_text": "Motor insurance (Vauxhall Corsa, reg AB12 CDE) ....... £412.50", "source_page": 1},
                "renewal_date": {"value": "2026-09-01", "confidence": 0.9, "source_text": "due for renewal on 01 September 2026", "source_page": 1},
            },
            {
                "product": "home",
                "annual_premium": {"value": 238.20, "confidence": 0.95, "source_text": "Home insurance (buildings & contents) ................ £238.20", "source_page": 1},
            },
        ],
        "stated_total": {"value": 650.70, "confidence": 0.95, "source_text": "Total amount payable ................................. £650.70", "source_page": 1},
    }
)


class TestClassifyShape(unittest.TestCase):
    def test_valid_response_parses(self):
        llm = FakeLLMClient([MULTICOVER_SHAPE_RESPONSE])
        shape, _ = classify_shape("doc text", llm)
        self.assertEqual(shape, Shape(line_count=2, renewal_status=RENEWAL_ALREADY_ACCEPTED))

    def test_fenced_json_is_tolerated(self):
        llm = FakeLLMClient(["```json\n" + MULTICOVER_SHAPE_RESPONSE + "\n```"])
        shape, _ = classify_shape("doc text", llm)
        self.assertEqual(shape.line_count, 2)

    def test_malformed_json_collapses_to_unsure(self):
        shape, _ = classify_shape("doc text", FakeLLMClient(["not json at all"]))
        self.assertEqual(shape, Shape(unsure=True))

    def test_implausible_values_collapse_to_unsure(self):
        for bad in (
            {"line_count": 0, "renewal_status": "proposed"},
            {"line_count": "two", "renewal_status": "proposed"},
            {"line_count": 1, "renewal_status": "maybe"},
            {"renewal_status": "proposed"},
        ):
            shape, _ = classify_shape("doc", FakeLLMClient([json.dumps(bad)]))
            self.assertEqual(shape, Shape(unsure=True), msg=f"should be unsure: {bad}")


class TestExtractLines(unittest.TestCase):
    SHAPE = Shape(line_count=2, renewal_status=RENEWAL_ALREADY_ACCEPTED)

    def test_valid_response_builds_extraction_with_provenance(self):
        llm = FakeLLMClient([MULTICOVER_EXTRACTION_RESPONSE])
        extraction, _ = extract_lines("doc text", "doc-1", self.SHAPE, llm)
        self.assertEqual(len(extraction.lines), 2)
        motor = extraction.lines[0]
        self.assertEqual(motor.annual_premium.value, 412.50)
        self.assertIn("£412.50", motor.annual_premium.source_text)
        self.assertEqual(motor.renewal_date.value, "2026-09-01")
        self.assertEqual(extraction.stated_total.value, 650.70)
        self.assertEqual(extraction.shape, self.SHAPE)

    def test_absent_optional_fields_are_none(self):
        llm = FakeLLMClient([json.dumps({"lines": [{"product": "motor"}], "stated_total": None})])
        extraction, _ = extract_lines("doc", "doc-1", Shape(1, RENEWAL_PROPOSED), llm)
        self.assertIsNone(extraction.lines[0].annual_premium)
        self.assertIsNone(extraction.stated_total)

    def test_malformed_response_raises_extraction_error(self):
        for bad in ("not json", json.dumps({"nope": []})):
            with self.assertRaises(ExtractionError):
                extract_lines("doc", "doc-1", self.SHAPE, FakeLLMClient([bad]))


class TestMultiCoverEndToEnd(unittest.TestCase):
    """The golden path through the LLM stages: synthetic twin text → shape →
    extraction → decide → review queue, zero events."""

    def test_twin_document_routes_to_review(self):
        text = (EXAMPLES / "multicover_renewal_invitation.txt").read_text()
        llm = FakeLLMClient([MULTICOVER_SHAPE_RESPONSE, MULTICOVER_EXTRACTION_RESPONSE])
        shape, _ = classify_shape(text, llm)
        extraction, _ = extract_lines(text, "golden-multicover", shape, llm)
        decision = decide(extraction, prior_year_premium=400.00)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.events, ())
        # The document text actually reached the model both times.
        self.assertIn("MULTICOVER", llm.calls[0]["user_content"])
        self.assertIn("MULTICOVER", llm.calls[1]["user_content"])


class TestTelemetry(unittest.TestCase):
    def test_usage_is_priced_and_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            llm = FakeLLMClient([MULTICOVER_SHAPE_RESPONSE])
            _, response = classify_shape("doc", llm)
            entry = record_llm_usage("classify_shape", response, root=root)
            self.assertEqual(entry["operation"], "classify_shape")
            self.assertGreater(entry["cost_usd"], 0)
            lines = (root / "telemetry.jsonl").read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["input_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
