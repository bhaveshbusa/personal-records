"""Trust ladder + propagation (2R.5): the old repo's `test_trust.py` ported,
plus the trust-propagation cases deferred from 2R.2 — auto-accepted facts
enter the log as `extracted`, human-confirmed facts as `verified`, and
projections carry the stamp through. Offline, fake LLM only."""

import json
import tempfile
import unittest
from pathlib import Path

from records import pipeline
from records.core import (
    TRUST_EXTRACTED,
    TRUST_INTERPRETED,
    TRUST_VERIFIED,
    Field,
    current_policies,
    min_trust,
    renewal_calendar,
    renewal_offers,
    replay,
)
from records.extract import FakeLLMClient
from records.review.rules import FIELD_CONFIDENCE_THRESHOLD, field_issues, line_confidence_issues

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


def schedule_extraction(premium=352.40, confidence=0.95, policy_number="POL-77"):
    return json.dumps(
        {
            "fields": {
                "policy_end_date": {"value": "2026-10-14", "confidence": confidence, "source_text": "to 14 October 2026", "source_page": 1},
                "annual_premium": {"value": premium, "confidence": confidence, "source_text": f"£{premium:.2f}", "source_page": 1},
                "vehicle_registration": {"value": "XY19 ZAB", "confidence": confidence, "source_text": "XY19 ZAB", "source_page": 1},
                "policy_number": {"value": policy_number, "confidence": confidence, "source_text": policy_number, "source_page": 1},
            }
        }
    )


def quote_extraction(premium=378.90, confidence=0.95, **identifiers):
    return json.dumps(
        {
            "lines": [
                {"product": "motor", "annual_premium": {"value": premium, "confidence": confidence, "source_text": f"£{premium:.2f}", "source_page": 1}}
            ],
            "stated_total": None,
            "identifiers": {
                name: {"value": value, "confidence": 0.95, "source_text": str(value), "source_page": 1}
                for name, value in identifiers.items()
            },
        }
    )


class MinTrustTests(unittest.TestCase):
    """Ported verbatim from the old repo — record-level trust is always the
    weakest contributing fact."""

    def test_all_verified_is_verified(self):
        self.assertEqual(min_trust([TRUST_VERIFIED, TRUST_VERIFIED]), TRUST_VERIFIED)

    def test_one_extracted_pulls_the_record_down(self):
        self.assertEqual(min_trust([TRUST_VERIFIED, TRUST_EXTRACTED, TRUST_VERIFIED]), TRUST_EXTRACTED)

    def test_one_interpreted_pulls_below_extracted(self):
        self.assertEqual(min_trust([TRUST_VERIFIED, TRUST_EXTRACTED, TRUST_INTERPRETED]), TRUST_INTERPRETED)

    def test_empty_defaults_to_extracted(self):
        self.assertEqual(min_trust([]), TRUST_EXTRACTED)

    def test_single_value_passes_through(self):
        self.assertEqual(min_trust([TRUST_INTERPRETED]), TRUST_INTERPRETED)


class TrustPropagationTests(unittest.TestCase):
    """Deferred from 2R.2: the old repo's trust-string event tests, redone on
    the typed model — trust is per event, stamped by the path the fact took."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_auto_accepted_schedule_is_extracted(self):
        llm = FakeLLMClient([DOC_TYPE_SCHEDULE, schedule_extraction()])
        result = pipeline.ingest(EXAMPLES / "motor_policy_schedule.txt", llm, root=self.root)
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(replay(root=self.root)[0].trust, TRUST_EXTRACTED)

    def test_confirmed_schedule_is_verified(self):
        # A conflicting second schedule parks; the human confirm stamps verified.
        llm = FakeLLMClient([DOC_TYPE_SCHEDULE, schedule_extraction()])
        pipeline.ingest(EXAMPLES / "motor_policy_schedule.txt", llm, root=self.root)
        rival = self.root / "rival.txt"
        rival.write_text("SwiftSure schedule POL-77 reissued, premium £399.99")
        second = pipeline.ingest(
            rival, FakeLLMClient([DOC_TYPE_SCHEDULE, schedule_extraction(premium=399.99)]), root=self.root
        )
        self.assertEqual(second.outcome, "review")
        events = pipeline.confirm(second.doc_id, root=self.root)
        self.assertEqual(events[0].trust, TRUST_VERIFIED)

    def test_confirmed_renewal_is_verified(self):
        doc = self.root / "shaky_quote.txt"
        doc.write_text("SwiftSure renewal quote — motor annual premium £378.90 (smudged scan)")
        low_confidence = quote_extraction(confidence=0.55)
        result = pipeline.ingest(
            doc, FakeLLMClient([DOC_TYPE_QUOTE, MOTOR_SHAPE, low_confidence]), root=self.root
        )
        self.assertEqual(result.outcome, "review")  # low confidence parks (2R.5 gate rule)
        events = pipeline.confirm(result.doc_id, root=self.root)
        self.assertEqual(events[0].trust, TRUST_VERIFIED)

    def test_projections_carry_trust_through(self):
        llm = FakeLLMClient([DOC_TYPE_SCHEDULE, schedule_extraction()])
        pipeline.ingest(EXAMPLES / "motor_policy_schedule.txt", llm, root=self.root)
        quote_doc = self.root / "renewal.txt"
        quote_doc.write_text("SwiftSure renewal quote — motor annual premium £378.90")
        pipeline.ingest(
            quote_doc,
            FakeLLMClient([DOC_TYPE_QUOTE, MOTOR_SHAPE, quote_extraction(policy_number="POL-77")]),
            root=self.root,
        )
        events = replay(root=self.root)
        self.assertEqual(current_policies(events)[0]["trust"], TRUST_EXTRACTED)
        self.assertEqual(renewal_calendar(events)[0]["trust"], TRUST_EXTRACTED)
        offer = renewal_offers(events)[0]
        self.assertTrue(offer["premium_change"]["comparable"])
        self.assertEqual(offer["trust"], TRUST_EXTRACTED)  # weakest side of the comparison

    def test_old_logs_without_trust_replay_as_extracted(self):
        # Schema evolution: events appended before 2R.5 have no trust key —
        # the dataclass default must apply, not a crash.
        llm = FakeLLMClient([DOC_TYPE_SCHEDULE, schedule_extraction()])
        pipeline.ingest(EXAMPLES / "motor_policy_schedule.txt", llm, root=self.root)
        log = self.root / "domain_events.jsonl"
        envelope = json.loads(log.read_text().strip())
        del envelope["data"]["trust"]
        log.write_text(json.dumps(envelope) + "\n")
        self.assertEqual(replay(root=self.root)[0].trust, TRUST_EXTRACTED)


class LowConfidenceGateTests(unittest.TestCase):
    """The old `route_for_review` REASON_LOW_CONFIDENCE rule, reconciled into
    the new gate (2R.5): shaky values park even when present."""

    def test_flat_field_below_threshold_parks(self):
        from records.core import REGISTRY, FieldExtraction

        extraction = FieldExtraction(
            doc_id="d1",
            doc_type="policy_schedule",
            fields={
                "policy_end_date": Field("2026-10-14", 0.95, "to 14 Oct 2026"),
                "annual_premium": Field(352.40, 0.55, "£352.40 (smudged)"),
                "vehicle_registration": Field("XY19 ZAB", 0.95, "XY19 ZAB"),
            },
        )
        issues = field_issues(extraction, REGISTRY["policy_schedule"])
        self.assertEqual(len(issues), 1)
        self.assertIn("'annual_premium'", issues[0])
        self.assertIn("below threshold", issues[0])

    def test_at_threshold_is_trusted(self):
        from records.core import REGISTRY, FieldExtraction

        extraction = FieldExtraction(
            doc_id="d1",
            doc_type="policy_schedule",
            fields={
                "policy_end_date": Field("2026-10-14", FIELD_CONFIDENCE_THRESHOLD, "x"),
                "annual_premium": Field(352.40, FIELD_CONFIDENCE_THRESHOLD, "x"),
                "vehicle_registration": Field("XY19 ZAB", FIELD_CONFIDENCE_THRESHOLD, "x"),
            },
        )
        self.assertEqual(field_issues(extraction, REGISTRY["policy_schedule"]), [])

    def test_line_premium_below_threshold_parks(self):
        from records.core import Extraction, ProductLine, Shape

        extraction = Extraction(
            doc_id="d1",
            shape=Shape(line_count=1, renewal_status="proposed"),
            lines=(ProductLine("motor", annual_premium=Field(378.90, 0.55, "£378.90")),),
        )
        issues = line_confidence_issues(extraction)
        self.assertEqual(len(issues), 1)
        self.assertIn("below threshold", issues[0])


if __name__ == "__main__":
    unittest.main()
