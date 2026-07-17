"""Query layer: deterministic tools + intent router (offline, fake LLM)."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from records.core import Field, RenewalAccepted, RenewalProposed, append
from records.extract import FakeLLMClient
from records.query import (
    ask,
    classify_intent,
    missing_info_answer,
    premium_history_answer,
    renewal_answer,
)

TODAY = date(2026, 8, 15)


def proposed(product="motor", premium=350.0, renewal_date="2026-09-01", doc_id="doc-1"):
    return RenewalProposed(
        doc_id=doc_id,
        product=product,
        annual_premium=premium,
        provenance=Field(premium, 0.9, f"£{premium:.2f}"),
        renewal_date=renewal_date,
    )


def intent_json(intent, product=None):
    return json.dumps({"intent": intent, "product": product})


class TestTools(unittest.TestCase):
    def test_renewal_answer_filters_by_product(self):
        events = [proposed(), proposed(product="home", renewal_date="2026-12-01", doc_id="doc-2")]
        result = renewal_answer(events, "motor", TODAY)
        self.assertTrue(result["found"])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["sources"], ["doc-1"])

    def test_renewal_answer_unknown_product_not_found(self):
        self.assertFalse(renewal_answer([proposed()], "boat", TODAY)["found"])

    def test_premium_history_computes_deltas_across_the_log(self):
        events = [
            proposed(premium=400.0, doc_id="y1"),
            RenewalAccepted("y2", "motor", 500.0, "2027-09-01"),
        ]
        result = premium_history_answer(events, "motor")
        step = result["products"][0]["steps"][0]
        self.assertEqual(step["change_pct"], 25.0)
        self.assertEqual(step["sources"], ["y1", "y2"])

    def test_missing_info_reports_gaps_and_pending(self):
        events = [proposed(renewal_date=None)]
        pending = [{"doc_id": "stuck-1", "reasons": ["shape: multi-line"], "queued_at": "2026-07-13T00:00:00"}]
        result = missing_info_answer(events, pending, TODAY)
        self.assertFalse(result["empty"])
        self.assertEqual(result["gaps"][0]["gap"], "no renewal date on record")
        self.assertEqual(result["pending_review"][0]["doc_id"], "stuck-1")

    def test_missing_info_empty_state(self):
        self.assertTrue(missing_info_answer([], [], TODAY)["empty"])


class TestClassifyIntent(unittest.TestCase):
    def test_valid_intent_parses(self):
        intent, product, _ = classify_intent(
            "when does my car insurance renew?",
            FakeLLMClient([intent_json("renewal_date", "motor")]),
        )
        self.assertEqual((intent, product), ("renewal_date", "motor"))

    def test_malformed_or_invented_intents_collapse_to_unknown(self):
        for bad in ("not json", intent_json("buy_me_insurance", "motor")):
            intent, product, _ = classify_intent("?", FakeLLMClient([bad]))
            self.assertEqual((intent, product), ("unknown", None))


class TestAskEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        append(proposed(doc_id="evidence-1"), root=self.root)

    def test_ask_renewal_answers_with_provenance(self):
        answer = ask(
            "when does my car insurance renew?",
            FakeLLMClient([intent_json("renewal_date", "motor")]),
            root=self.root,
            today=TODAY,
        )
        self.assertIn("2026-09-01", answer.text)
        self.assertIn("evidence-1"[:12], answer.text)
        self.assertEqual(answer.sources, ("evidence-1",))

    def test_ask_unknown_intent_explains_capabilities(self):
        answer = ask("write me a poem", FakeLLMClient([intent_json("unknown")]), root=self.root)
        self.assertEqual(answer.intent, "unknown")
        self.assertIn("couldn't map", answer.text)

    def test_ask_records_telemetry(self):
        ask("?", FakeLLMClient([intent_json("premium", None)]), root=self.root, today=TODAY)
        lines = (self.root / "telemetry.jsonl").read_text().strip().splitlines()
        self.assertEqual(json.loads(lines[0])["operation"], "classify_intent")


if __name__ == "__main__":
    unittest.main()
