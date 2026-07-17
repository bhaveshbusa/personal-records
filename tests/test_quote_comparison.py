"""Phase 2R.5 gate, end-to-end and offline: file a policy schedule, ingest a
renewal quote that links to it, then `ask("how does this quote compare to my
current policy?")` answers deterministically — the delta comes from the
renewal_offers projection, never the LLM. The one LLM call classifies
intent, and is canned here."""

import json
import tempfile
import unittest
from pathlib import Path

from records import pipeline
from records.core import replay
from records.extract import FakeLLMClient
from records.query import ask, quote_comparison_answer

EXAMPLES = Path(__file__).parent.parent / "examples"

DOC_TYPE_SCHEDULE = json.dumps(
    {"doc_type": "policy_schedule", "confidence": 0.95, "rationale": "schedule header"}
)
DOC_TYPE_QUOTE = json.dumps(
    {"doc_type": "renewal_quote", "confidence": 0.95, "rationale": "renewal offer"}
)
MOTOR_SHAPE = json.dumps(
    {"line_count": 1, "renewal_status": "proposed", "unsure": False, "rationale": "single motor quote"}
)
SCHEDULE_POL77 = json.dumps(
    {
        "fields": {
            "policy_end_date": {"value": "2026-10-14", "confidence": 0.95, "source_text": "to 14 October 2026", "source_page": 1},
            "annual_premium": {"value": 352.40, "confidence": 0.95, "source_text": "£352.40", "source_page": 1},
            "vehicle_registration": {"value": "XY19 ZAB", "confidence": 0.95, "source_text": "XY19 ZAB", "source_page": 1},
            "policy_number": {"value": "POL-77", "confidence": 0.95, "source_text": "Policy number POL-77", "source_page": 1},
        }
    }
)
QUOTE_LINKED = json.dumps(
    {
        "lines": [
            {"product": "motor", "annual_premium": {"value": 378.90, "confidence": 0.95, "source_text": "£378.90", "source_page": 1}}
        ],
        "stated_total": None,
        "identifiers": {
            "policy_number": {"value": "POL-77", "confidence": 0.95, "source_text": "POL-77", "source_page": 1}
        },
    }
)


def intent_json(intent, product=None):
    return json.dumps({"intent": intent, "product": product})


class QuoteComparisonGateTest(unittest.TestCase):
    QUESTION = "how does this quote compare to my current policy?"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _file_schedule_and_quote(self):
        r1 = pipeline.ingest(
            EXAMPLES / "motor_policy_schedule.txt",
            FakeLLMClient([DOC_TYPE_SCHEDULE, SCHEDULE_POL77]),
            root=self.root,
        )
        self.assertEqual(r1.outcome, "accepted")
        quote_doc = self.root / "renewal_2026.txt"
        quote_doc.write_text("SwiftSure renewal quote — motor annual premium £378.90, policy POL-77")
        r2 = pipeline.ingest(
            quote_doc, FakeLLMClient([DOC_TYPE_QUOTE, MOTOR_SHAPE, QUOTE_LINKED]), root=self.root
        )
        self.assertEqual(r2.outcome, "accepted")
        return r1.doc_id, r2.doc_id

    def test_gate_comparison_answers_deterministically_with_provenance(self):
        schedule_id, quote_id = self._file_schedule_and_quote()
        llm = FakeLLMClient([intent_json("quote_comparison", "motor")])
        answer = ask(self.QUESTION, llm, root=self.root)

        self.assertEqual(answer.intent, "quote_comparison")
        self.assertIn("quoted £378.90", answer.text)
        self.assertIn("current £352.40", answer.text)
        self.assertIn("+£26.50", answer.text)
        self.assertIn("+7.5%", answer.text)
        self.assertIn("[extracted]", answer.text)  # trust of the weakest side (2R.5)
        self.assertIn(quote_id[:12], answer.text)
        self.assertIn(schedule_id[:12], answer.text)
        self.assertEqual(set(answer.sources), {quote_id, schedule_id})
        self.assertEqual(len(llm.calls), 1)  # intent only — the maths is deterministic

    def test_unlinked_quote_reports_not_comparable_not_a_guess(self):
        quote_doc = self.root / "first_quote.txt"
        quote_doc.write_text("SwiftSure quote — motor £378.90")
        unlinked = json.loads(QUOTE_LINKED)
        unlinked["identifiers"] = {}
        pipeline.ingest(
            quote_doc,
            FakeLLMClient([DOC_TYPE_QUOTE, MOTOR_SHAPE, json.dumps(unlinked)]),
            root=self.root,
        )
        answer = ask(self.QUESTION, FakeLLMClient([intent_json("quote_comparison")]), root=self.root)
        self.assertIn("not comparable", answer.text)
        self.assertNotIn("vs current", answer.text)

    def test_no_quote_on_file_explains_what_to_ingest(self):
        answer = ask(self.QUESTION, FakeLLMClient([intent_json("quote_comparison")]), root=self.root)
        self.assertIn("No live renewal quote", answer.text)
        self.assertEqual(answer.sources, ())


class QuoteComparisonToolTests(unittest.TestCase):
    """The tool is a filter/shape over renewal_offers — projection tests own
    the delta maths; these own product filtering and source collection."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        gate = QuoteComparisonGateTest("test_gate_comparison_answers_deterministically_with_provenance")
        gate.root = self.root
        gate._file_schedule_and_quote()
        self.events = replay(root=self.root)

    def test_product_filter(self):
        self.assertTrue(quote_comparison_answer(self.events, "motor")["found"])
        self.assertFalse(quote_comparison_answer(self.events, "boat")["found"])

    def test_sources_include_both_sides_of_the_comparison(self):
        result = quote_comparison_answer(self.events)
        offer = result["offers"][0]
        self.assertEqual(
            set(result["sources"]), {offer["doc_id"], offer["current_policy_doc_id"]}
        )


class QuoteProfileAskTest(unittest.TestCase):
    """The quote_profile route: deterministic fold + export, one intent call."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_ask_quote_profile_exports_and_reports_completeness(self):
        pipeline.ingest(
            EXAMPLES / "motor_policy_schedule.txt",
            FakeLLMClient([DOC_TYPE_SCHEDULE, SCHEDULE_POL77]),
            root=self.root,
        )
        answer = ask(
            "get me ready to re-quote",
            FakeLLMClient([intent_json("quote_profile", "motor")]),
            root=self.root,
        )
        self.assertEqual(answer.intent, "quote_profile")
        self.assertIn("fields populated", answer.text)
        exports = self.root / "exports"
        self.assertTrue((exports / "quote_profile.md").exists())
        self.assertTrue((exports / "quote_profile.json").exists())
        profile = json.loads((exports / "quote_profile.json").read_text())
        self.assertEqual(profile["cover"]["current_premium"]["value"], 352.40)
        self.assertTrue(answer.sources)  # provenance doc_ids collected


if __name__ == "__main__":
    unittest.main()
