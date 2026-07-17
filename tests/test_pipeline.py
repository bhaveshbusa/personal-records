"""End-to-end pipeline with the fake LLM: ingest → events or review →
confirm → calendar. Offline, no keys, synthetic fixtures only."""

import json
import tempfile
import unittest
from pathlib import Path

from records import pipeline
from records.core import (
    DocumentDiscarded,
    NcdConfirmed,
    PolicyFiled,
    RenewalAccepted,
    RenewalProposed,
    current_policies,
    renewal_calendar,
    replay,
)
from records.extract import FakeLLMClient
from records.review import queue

EXAMPLES = Path(__file__).parent.parent / "examples"

DOC_TYPE_QUOTE = json.dumps(
    {"doc_type": "renewal_quote", "confidence": 0.95, "rationale": "renewal offer wording"}
)
DOC_TYPE_SCHEDULE = json.dumps(
    {"doc_type": "policy_schedule", "confidence": 0.95, "rationale": "schedule header"}
)
DOC_TYPE_UNKNOWN = json.dumps(
    {"doc_type": "unknown", "confidence": 0.9, "rationale": "marketing leaflet, no record"}
)
SCHEDULE_EXTRACTION = json.dumps(
    {
        "fields": {
            "policy_end_date": {"value": "2026-10-14", "confidence": 0.95, "source_text": "15 October 2025 to 14 October 2026", "source_page": 1},
            "annual_premium": {"value": 352.40, "confidence": 0.95, "source_text": "Annual premium (including IPT) ... £352.40", "source_page": 1},
            "vehicle_registration": {"value": "XY19 ZAB", "confidence": 0.95, "source_text": "registration XY19 ZAB", "source_page": 1},
            "provider": {"value": "SwiftSure Insurance Ltd", "confidence": 0.9, "source_text": "SwiftSure Insurance Ltd", "source_page": 1},
            "not_in_schema": {"value": "dropped", "confidence": 0.9, "source_text": "x", "source_page": 1},
        }
    }
)

MOTOR_SHAPE = json.dumps(
    {"line_count": 1, "renewal_status": "proposed", "unsure": False, "rationale": "single motor quote"}
)
MOTOR_EXTRACTION = json.dumps(
    {
        "lines": [
            {
                "product": "motor",
                "annual_premium": {"value": 378.90, "confidence": 0.95, "source_text": "Annual premium for the coming year ................... £378.90", "source_page": 1},
                "renewal_date": {"value": "2026-10-14", "confidence": 0.9, "source_text": "expires on\n14 October 2026", "source_page": 1},
            }
        ],
        "stated_total": {"value": 378.90, "confidence": 0.9, "source_text": "£378.90", "source_page": 1},
    }
)
MULTICOVER_SHAPE = json.dumps(
    {"line_count": 2, "renewal_status": "already_accepted", "unsure": False, "rationale": "motor+home, renews automatically"}
)
MULTICOVER_EXTRACTION = json.dumps(
    {
        "lines": [
            {
                "product": "motor",
                "annual_premium": {"value": 412.50, "confidence": 0.95, "source_text": "Motor insurance ... £412.50", "source_page": 1},
                "renewal_date": {"value": "2026-09-01", "confidence": 0.9, "source_text": "renewal on 01 September 2026", "source_page": 1},
            },
            {
                "product": "home",
                "annual_premium": {"value": 238.20, "confidence": 0.95, "source_text": "Home insurance ... £238.20", "source_page": 1},
            },
        ],
        "stated_total": {"value": 650.70, "confidence": 0.95, "source_text": "Total amount payable ... £650.70", "source_page": 1},
    }
)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_clean_single_policy_flows_to_events(self):
        llm = FakeLLMClient([DOC_TYPE_QUOTE, MOTOR_SHAPE, MOTOR_EXTRACTION])
        result = pipeline.ingest(EXAMPLES / "motor_renewal_quote.txt", llm, root=self.root)
        self.assertEqual(result.outcome, "accepted")
        events = replay(root=self.root)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], RenewalProposed)
        self.assertEqual(events[0].annual_premium, 378.90)
        self.assertIn("£378.90", events[0].provenance.source_text)  # provenance survives to the log
        calendar = renewal_calendar(events)
        self.assertEqual(calendar[0]["renewal_date"], "2026-10-14")

    def test_duplicate_ingest_never_calls_the_llm(self):
        llm = FakeLLMClient([DOC_TYPE_QUOTE, MOTOR_SHAPE, MOTOR_EXTRACTION])
        pipeline.ingest(EXAMPLES / "motor_renewal_quote.txt", llm, root=self.root)
        again = FakeLLMClient([])
        result = pipeline.ingest(EXAMPLES / "motor_renewal_quote.txt", again, root=self.root)
        self.assertEqual(result.outcome, "duplicate")
        self.assertEqual(again.calls, [])
        self.assertEqual(len(replay(root=self.root)), 1)  # no double events

    def test_multicover_routes_to_review_then_confirm_emits_accepted_events(self):
        llm = FakeLLMClient([DOC_TYPE_QUOTE, MULTICOVER_SHAPE, MULTICOVER_EXTRACTION])
        result = pipeline.ingest(EXAMPLES / "multicover_renewal_invitation.txt", llm, root=self.root)
        self.assertEqual(result.outcome, "review")
        self.assertEqual(replay(root=self.root), [])  # zero events before a human confirms
        self.assertEqual(len(queue.list_pending(root=self.root)), 1)

        events = pipeline.confirm(result.doc_id, root=self.root)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(isinstance(e, RenewalAccepted) for e in events))
        self.assertEqual(queue.list_pending(root=self.root), [])
        products = {row["product"] for row in renewal_calendar(replay(root=self.root))}
        self.assertEqual(products, {"motor", "home"})

    def test_unparseable_extraction_routes_to_review_without_extraction(self):
        llm = FakeLLMClient([DOC_TYPE_QUOTE, MOTOR_SHAPE, "the model rambled instead of emitting JSON"])
        result = pipeline.ingest(EXAMPLES / "motor_renewal_quote.txt", llm, root=self.root)
        self.assertEqual(result.outcome, "review")
        item = queue.list_pending(root=self.root)[0]
        self.assertIsNone(item["extraction"])
        with self.assertRaises(ValueError):
            pipeline.confirm(result.doc_id, root=self.root)  # nothing to confirm

    def test_prior_year_band_uses_event_log(self):
        # Year 1: motor at £378.90 accepted.
        llm = FakeLLMClient([DOC_TYPE_QUOTE, MOTOR_SHAPE, MOTOR_EXTRACTION])
        pipeline.ingest(EXAMPLES / "motor_renewal_quote.txt", llm, root=self.root)
        # Year 2: same product at £650.70 (+71.7%) — outside the ±40% band.
        year2_doc = self.root / "motor_renewal_2027.txt"
        year2_doc.write_text("SwiftSure renewal quote 2027: motor annual premium £650.70")
        year2_extraction = json.dumps(
            {
                "lines": [
                    {"product": "motor", "annual_premium": {"value": 650.70, "confidence": 0.95, "source_text": "£650.70", "source_page": 1}}
                ],
                "stated_total": None,
            }
        )
        result = pipeline.ingest(year2_doc, FakeLLMClient([DOC_TYPE_QUOTE, MOTOR_SHAPE, year2_extraction]), root=self.root)
        self.assertEqual(result.outcome, "review")
        self.assertTrue(any("band" in r for r in result.review_reasons))

    def test_telemetry_recorded_per_llm_call(self):
        llm = FakeLLMClient([DOC_TYPE_QUOTE, MOTOR_SHAPE, MOTOR_EXTRACTION])
        pipeline.ingest(EXAMPLES / "motor_renewal_quote.txt", llm, root=self.root)
        lines = (self.root / "telemetry.jsonl").read_text().strip().splitlines()
        self.assertEqual([json.loads(l)["operation"] for l in lines], ["classify_doc_type", "classify_shape", "extract_lines"])


class DocTypeRoutingTest(unittest.TestCase):
    """Phase 2R.1 gate: docs classify to their types; unknown → review."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_unknown_doc_routes_to_review_before_any_extraction(self):
        leaflet = self.root / "accident_guide_leaflet.txt"
        leaflet.write_text("WHAT TO DO IN AN ACCIDENT — a handy guide from SwiftSure!")
        llm = FakeLLMClient([DOC_TYPE_UNKNOWN])
        result = pipeline.ingest(leaflet, llm, root=self.root)
        self.assertEqual(result.outcome, "review")
        self.assertEqual(result.doc_type, "unknown")
        self.assertEqual(len(llm.calls), 1)  # no shape/extraction calls after unknown
        self.assertTrue(any("classification" in r for r in result.review_reasons))
        self.assertEqual(replay(root=self.root), [])

    def test_low_confidence_classification_routes_to_review(self):
        doc = self.root / "maybe_certificate.txt"
        doc.write_text("Certificate-ish text")
        low = json.dumps({"doc_type": "certificate", "confidence": 0.5, "rationale": "unsure"})
        result = pipeline.ingest(doc, FakeLLMClient([low]), root=self.root)
        self.assertEqual(result.outcome, "review")
        self.assertTrue(any("below threshold" in r for r in result.review_reasons))

    def test_policy_schedule_files_policy_and_projects_current_record(self):
        # 2R.2 gate, part 1: file policy_schedule → PolicyFiled →
        # current-policy projection.
        llm = FakeLLMClient([DOC_TYPE_SCHEDULE, SCHEDULE_EXTRACTION])
        result = pipeline.ingest(EXAMPLES / "motor_policy_schedule.txt", llm, root=self.root)
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(result.doc_type, "policy_schedule")
        events = replay(root=self.root)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIsInstance(event, PolicyFiled)
        # No policy_number extracted: the linker mints the entity from the
        # normalised vehicle registration (2R.3) — stable across re-ingests.
        self.assertEqual(event.entity_id, "XY19ZAB")
        self.assertEqual(event.valid_to, "2026-10-14")  # from schema valid_to_source
        self.assertEqual(event.provider, "SwiftSure Insurance Ltd")
        self.assertEqual(event.fields["annual_premium"].value, 352.40)
        self.assertIn("£352.40", event.fields["annual_premium"].source_text)  # provenance survives
        self.assertNotIn("not_in_schema", event.fields)  # schema is the contract
        current = current_policies(events)
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["doc_id"], result.doc_id)

    def test_discard_folds_filed_policy_away(self):
        # 2R.2 gate, part 2: discard a doc → its events fold away (and the
        # log keeps both the fact and the retraction).
        llm = FakeLLMClient([DOC_TYPE_SCHEDULE, SCHEDULE_EXTRACTION])
        result = pipeline.ingest(EXAMPLES / "motor_policy_schedule.txt", llm, root=self.root)
        self.assertEqual(len(current_policies(replay(root=self.root))), 1)
        pipeline.discard(result.doc_id, reason="wrong household's schedule", root=self.root)
        events = replay(root=self.root)
        self.assertEqual(current_policies(events), [])
        self.assertEqual(len(events), 2)  # PolicyFiled + DocumentDiscarded both kept
        self.assertIsInstance(events[-1], DocumentDiscarded)

    def test_conflicting_schedule_routes_to_review_then_confirm_overrides(self):
        # Overwrite guard: a second, different document for the same
        # policy_number parks in review; a human confirm overrides.
        with_pol = json.loads(SCHEDULE_EXTRACTION)
        with_pol["fields"]["policy_number"] = {"value": "POL-77", "confidence": 0.95, "source_text": "Policy number POL-77", "source_page": 1}
        llm = FakeLLMClient([DOC_TYPE_SCHEDULE, json.dumps(with_pol)])
        first = pipeline.ingest(EXAMPLES / "motor_policy_schedule.txt", llm, root=self.root)
        self.assertEqual(first.outcome, "accepted")

        rival = self.root / "rival_schedule.txt"
        rival.write_text("SwiftSure policy schedule POL-77 — reissued copy, premium £999.99")
        with_pol["fields"]["annual_premium"]["value"] = 999.99
        second = pipeline.ingest(rival, FakeLLMClient([DOC_TYPE_SCHEDULE, json.dumps(with_pol)]), root=self.root)
        self.assertEqual(second.outcome, "review")
        self.assertTrue(any("conflict" in r for r in second.review_reasons))
        self.assertEqual(len(replay(root=self.root)), 1)  # nothing emitted by the rival yet

        events = pipeline.confirm(second.doc_id, root=self.root)  # human override
        self.assertEqual(len(events), 1)
        current = current_policies(replay(root=self.root))
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["doc_id"], second.doc_id)
        self.assertEqual(current[0]["fields"]["annual_premium"].value, 999.99)

    def test_missing_required_field_parks_then_confirm_emits_policy_filed(self):
        incomplete = json.dumps(
            {"fields": {"annual_premium": {"value": 352.40, "confidence": 0.9, "source_text": "£352.40", "source_page": 1}}}
        )
        llm = FakeLLMClient([DOC_TYPE_SCHEDULE, incomplete])
        result = pipeline.ingest(EXAMPLES / "motor_policy_schedule.txt", llm, root=self.root)
        self.assertEqual(result.outcome, "review")
        reasons = "\n".join(result.review_reasons)
        self.assertIn("'policy_end_date'", reasons)
        self.assertIn("'vehicle_registration'", reasons)
        # A human confirming the partial extraction as-is still files it.
        events = pipeline.confirm(result.doc_id, root=self.root)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], PolicyFiled)
        self.assertIsNone(events[0].valid_to)  # missing field stays missing — no invention

    def test_ncd_letter_files_ncd_confirmed(self):
        ncd_doc = self.root / "ncd_letter.txt"
        ncd_doc.write_text("We confirm you have earned 5 years no claims discount. SwiftSure.")
        verdict = json.dumps({"doc_type": "ncd_letter", "confidence": 0.95, "rationale": "NCD confirmation"})
        extraction = json.dumps(
            {"fields": {"ncd_years": {"value": 5, "confidence": 0.95, "source_text": "5 years no claims discount", "source_page": 1}}}
        )
        result = pipeline.ingest(ncd_doc, FakeLLMClient([verdict, extraction]), root=self.root)
        self.assertEqual(result.outcome, "accepted")
        events = replay(root=self.root)
        self.assertIsInstance(events[0], NcdConfirmed)
        self.assertEqual(events[0].fields["ncd_years"].value, 5)

    def test_clean_certificate_is_stored_with_no_events(self):
        cert = self.root / "certificate.txt"
        cert.write_text("CERTIFICATE OF MOTOR INSURANCE — policy POL-77")
        verdict = json.dumps({"doc_type": "certificate", "confidence": 0.95, "rationale": "certificate"})
        extraction = json.dumps(
            {"fields": {"policy_number": {"value": "POL-77", "confidence": 0.95, "source_text": "policy POL-77", "source_page": 1}}}
        )
        result = pipeline.ingest(cert, FakeLLMClient([verdict, extraction]), root=self.root)
        self.assertEqual(result.outcome, "stored")  # the document itself is the record
        self.assertEqual(replay(root=self.root), [])
        self.assertEqual(queue.list_pending(root=self.root), [])

    def test_doc_type_without_event_vocabulary_stays_parked(self):
        # payslip etc. had no events in the old repo either — parked, and
        # confirm still refuses with a clear message (only reject works).
        payslip = self.root / "payslip.txt"
        payslip.write_text("ACME LTD payslip — net pay £2,100.00, pay date 28 June 2026")
        verdict = json.dumps({"doc_type": "payslip", "confidence": 0.95, "rationale": "payslip"})
        extraction = json.dumps(
            {
                "fields": {
                    "pay_date": {"value": "2026-06-28", "confidence": 0.95, "source_text": "28 June 2026", "source_page": 1},
                    "gross_pay": {"value": 2800.0, "confidence": 0.95, "source_text": "£2,800.00", "source_page": 1},
                    "net_pay": {"value": 2100.0, "confidence": 0.95, "source_text": "£2,100.00", "source_page": 1},
                }
            }
        )
        result = pipeline.ingest(payslip, FakeLLMClient([verdict, extraction]), root=self.root)
        self.assertEqual(result.outcome, "review")
        self.assertTrue(any("no event vocabulary" in r for r in result.review_reasons))
        with self.assertRaises(ValueError):
            pipeline.confirm(result.doc_id, root=self.root)
        queue.reject(result.doc_id, root=self.root)
        self.assertEqual(queue.list_pending(root=self.root), [])

    def test_unparseable_field_extraction_routes_to_review(self):
        llm = FakeLLMClient([DOC_TYPE_SCHEDULE, "not JSON at all"])
        result = pipeline.ingest(EXAMPLES / "motor_policy_schedule.txt", llm, root=self.root)
        self.assertEqual(result.outcome, "review")
        self.assertIsNone(queue.list_pending(root=self.root)[0]["extraction"])

    def test_reference_text_is_stored_without_extraction(self):
        wording = self.root / "policy_wording.txt"
        wording.write_text("SECTION 1 — WINDSCREEN COVER. We will pay for...")
        verdict = json.dumps({"doc_type": "policy_wording", "confidence": 0.95, "rationale": "wording"})
        llm = FakeLLMClient([verdict])
        result = pipeline.ingest(wording, llm, root=self.root)
        self.assertEqual(result.outcome, "stored")
        self.assertEqual(result.doc_type, "policy_wording")
        self.assertEqual(len(llm.calls), 1)  # classification only
        self.assertEqual(queue.list_pending(root=self.root), [])
        self.assertEqual(replay(root=self.root), [])

    def test_telemetry_for_field_path(self):
        llm = FakeLLMClient([DOC_TYPE_SCHEDULE, SCHEDULE_EXTRACTION])
        pipeline.ingest(EXAMPLES / "motor_policy_schedule.txt", llm, root=self.root)
        lines = (self.root / "telemetry.jsonl").read_text().strip().splitlines()
        self.assertEqual(
            [json.loads(l)["operation"] for l in lines], ["classify_doc_type", "extract_fields"]
        )


class EntityLinkingTest(unittest.TestCase):
    """Phase 2R.3 gate: a renewal quote links to the existing policy entity
    it renews, and the offer pairs with the current policy."""

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

    @staticmethod
    def quote_extraction(premium: float, **identifiers) -> str:
        return json.dumps(
            {
                "lines": [
                    {"product": "motor", "annual_premium": {"value": premium, "confidence": 0.95, "source_text": f"£{premium:.2f}", "source_page": 1}}
                ],
                "stated_total": None,
                "identifiers": {
                    name: {"value": value, "confidence": 0.95, "source_text": str(value), "source_page": 1}
                    for name, value in identifiers.items()
                },
            }
        )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _file_schedule(self):
        llm = FakeLLMClient([DOC_TYPE_SCHEDULE, self.SCHEDULE_POL77])
        result = pipeline.ingest(EXAMPLES / "motor_policy_schedule.txt", llm, root=self.root)
        self.assertEqual(result.outcome, "accepted")
        return result

    def _ingest_quote(self, premium: float, name="renewal_2026.txt", **identifiers):
        doc = self.root / name
        doc.write_text(f"SwiftSure renewal quote — motor annual premium £{premium:.2f}")
        llm = FakeLLMClient([DOC_TYPE_QUOTE, MOTOR_SHAPE, self.quote_extraction(premium, **identifiers)])
        return pipeline.ingest(doc, llm, root=self.root)

    def test_quote_links_to_the_policy_it_renews_and_offer_pairs(self):
        self._file_schedule()
        result = self._ingest_quote(378.90, policy_number="POL-77")
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(result.events[0].entity_id, "POL-77")

        from records.core import renewal_offers

        offers = renewal_offers(replay(root=self.root))
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["entity_id"], "POL-77")
        self.assertTrue(offers[0]["premium_change"]["comparable"])
        self.assertEqual(offers[0]["premium_change"]["previous"], 352.40)
        self.assertEqual(offers[0]["premium_change"]["delta"], 26.50)

    def test_quote_links_by_vehicle_registration_when_no_policy_number(self):
        self._file_schedule()
        result = self._ingest_quote(378.90, vehicle_registration="xy19zab")
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(result.events[0].entity_id, "POL-77")

    def test_band_baseline_comes_from_linked_current_policy(self):
        # No renewal events exist — only the filed schedule. A linked quote
        # at +84.6% must trip the band against the schedule's premium.
        self._file_schedule()
        result = self._ingest_quote(650.70, policy_number="POL-77")
        self.assertEqual(result.outcome, "review")
        self.assertTrue(any("band" in r for r in result.review_reasons))
        # ...and confirming it from review still links the emitted event.
        events = pipeline.confirm(result.doc_id, root=self.root)
        self.assertEqual(events[0].entity_id, "POL-77")

    def test_unlinked_first_quote_still_flows_clean(self):
        # The Phase 2 golden flow survives 2R.3: no policy on file, no
        # identifiers — accepted, unlinked.
        result = self._ingest_quote(378.90)
        self.assertEqual(result.outcome, "accepted")
        self.assertIsNone(result.events[0].entity_id)

    def test_second_schedule_same_reg_links_to_existing_entity_not_a_duplicate(self):
        # Renewal-year continuity: a new schedule with a NEW policy number
        # but the same registration links (by reg) to the existing entity —
        # and the overwrite guard parks it rather than silently replacing
        # the current state. Confirming keeps one entity, updated.
        self._file_schedule()
        rival = json.loads(self.SCHEDULE_POL77)
        rival["fields"]["policy_number"]["value"] = "POL-88"
        rival_doc = self.root / "second_schedule.txt"
        rival_doc.write_text("SwiftSure schedule POL-88, same vehicle XY19 ZAB")
        r = pipeline.ingest(rival_doc, FakeLLMClient([DOC_TYPE_SCHEDULE, json.dumps(rival)]), root=self.root)
        self.assertEqual(r.outcome, "review")
        self.assertTrue(any("conflict" in reason for reason in r.review_reasons))
        events = pipeline.confirm(r.doc_id, root=self.root)
        self.assertEqual(events[0].entity_id, "POL-77")  # same entity, not a twin
        from records.core import current_policies

        current = current_policies(replay(root=self.root))
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["fields"]["policy_number"].value, "POL-88")

    def test_ambiguous_link_routes_to_review(self):
        # Two entities already sharing a registration (e.g. pre-linker
        # history) — a quote matching only by reg must not guess.
        from records.core import Field, PolicyFiled, append

        for entity, doc in (("POL-77", "d1"), ("POL-88", "d2")):
            append(
                PolicyFiled(
                    doc_id=doc,
                    doc_type="policy_schedule",
                    entity_id=entity,
                    fields={
                        "policy_number": Field(entity, 0.95, entity),
                        "vehicle_registration": Field("XY19 ZAB", 0.95, "XY19 ZAB"),
                        "annual_premium": Field(352.40, 0.95, "£352.40"),
                    },
                ),
                root=self.root,
            )
        result = self._ingest_quote(378.90, vehicle_registration="XY19 ZAB")
        self.assertEqual(result.outcome, "review")
        self.assertTrue(any("ambiguous" in r for r in result.review_reasons))
        self.assertEqual(len(replay(root=self.root)), 2)  # no third event emitted


class ReviewQueueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _park_multicover(self) -> str:
        llm = FakeLLMClient([DOC_TYPE_QUOTE, MULTICOVER_SHAPE, MULTICOVER_EXTRACTION])
        return pipeline.ingest(EXAMPLES / "multicover_renewal_invitation.txt", llm, root=self.root).doc_id

    def test_reject_emits_nothing_and_clears_queue(self):
        doc_id = self._park_multicover()
        queue.reject(doc_id, root=self.root)
        self.assertEqual(queue.list_pending(root=self.root), [])
        self.assertEqual(replay(root=self.root), [])

    def test_resolved_items_cannot_be_resolved_twice(self):
        doc_id = self._park_multicover()
        queue.reject(doc_id, root=self.root)
        with self.assertRaises(KeyError):
            queue.confirm(doc_id, root=self.root)
        with self.assertRaises(KeyError):
            queue.reject(doc_id, root=self.root)


if __name__ == "__main__":
    unittest.main()
