"""Event log round-trip + projections (offline).

The projection/dispatch regression cases are ported from the old repo's
`test_event_spine.py` (2R.2), rebuilt on the typed event model — including
the MultiCover mitigation: document-scoped discards that retract one
document's facts without deleting same-entity state backed by another.
"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from records.core import (
    DocumentDiscarded,
    Field,
    FieldExtraction,
    NcdConfirmed,
    PolicyCorrected,
    PolicyFiled,
    REGISTRY,
    RenewalAccepted,
    RenewalProposed,
    append,
    conflicting_policy_filed,
    current_policies,
    event_for_fields,
    has_event_vocabulary,
    renewal_calendar,
    renewal_offers,
    replay,
)


def proposed(product="motor", premium=350.0, renewal_date="2026-09-01", doc_id="doc-1",
             entity_id=None):
    return RenewalProposed(
        doc_id=doc_id,
        product=product,
        annual_premium=premium,
        provenance=Field(premium, 0.9, f"Premium: £{premium:.2f}"),
        renewal_date=renewal_date,
        entity_id=entity_id,
    )


def filed(doc_id="d1", entity_id="POL-1", valid_to="2026-06-01", premium=500.0,
          policy_number=None):
    fields = {"annual_premium": Field(premium, 0.95, f"£{premium:.2f}")}
    if policy_number or entity_id:
        fields["policy_number"] = Field(policy_number or entity_id, 0.95, "Policy number")
    return PolicyFiled(
        doc_id=doc_id,
        doc_type="policy_schedule",
        entity_id=entity_id,
        fields=fields,
        valid_from="2025-06-01",
        valid_to=valid_to,
        provider="SwiftSure",
    )


class TestEventLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_append_replay_round_trip_typed(self):
        event = proposed()
        envelope = append(event, root=self.root)
        self.assertEqual(envelope["event_type"], "RenewalProposed")
        replayed = replay(root=self.root)
        self.assertEqual(replayed, [event])  # frozen dataclass equality, provenance included

    def test_replay_order_is_append_order(self):
        append(proposed(premium=350.0), root=self.root)
        append(RenewalAccepted("doc-2", "motor", 350.0, "2026-09-01"), root=self.root)
        replayed = replay(root=self.root)
        self.assertIsInstance(replayed[0], RenewalProposed)
        self.assertIsInstance(replayed[1], RenewalAccepted)

    def test_empty_log_replays_empty(self):
        self.assertEqual(replay(root=self.root), [])

    def test_non_event_rejected(self):
        with self.assertRaises(TypeError):
            append({"event_type": "handcrafted"}, root=self.root)  # type: ignore[arg-type]

    def test_restored_event_types_round_trip_typed(self):
        # 2R.2 vocabulary: fields dicts rehydrate to typed Field values.
        events = [
            filed(),
            NcdConfirmed(
                doc_id="d-ncd",
                doc_type="ncd_letter",
                entity_id="POL-1",
                fields={"ncd_years": Field(5, 0.95, "5 years NCD")},
                provider="SwiftSure",
            ),
            DocumentDiscarded(doc_id="d1", reason="misextracted"),
            PolicyCorrected(
                doc_id="d1c", doc_type="policy_schedule", entity_id="POL-1",
                fields={"annual_premium": Field(512.0, 1.0, "£512.00")},
            ),
        ]
        for event in events:
            append(event, root=self.root)
        self.assertEqual(replay(root=self.root), events)


class TestRenewalCalendar(unittest.TestCase):
    TODAY = date(2026, 8, 15)

    def test_latest_event_per_product_wins(self):
        events = [
            proposed(premium=350.0),
            RenewalAccepted("doc-2", "motor", 340.0, "2026-09-01"),
            proposed(product="home", premium=200.0, renewal_date="2026-08-20", doc_id="doc-3"),
        ]
        calendar = renewal_calendar(events, today=self.TODAY)
        self.assertEqual(len(calendar), 2)
        motor = next(r for r in calendar if r["product"] == "motor")
        self.assertEqual(motor["state"], "RenewalAccepted")
        self.assertEqual(motor["annual_premium"], 340.0)

    def test_status_bands_and_sort(self):
        events = [
            proposed(product="expired", renewal_date="2026-08-01"),
            proposed(product="soon", renewal_date="2026-08-20"),
            proposed(product="ok", renewal_date="2026-12-01"),
            proposed(product="undated", renewal_date=None),
        ]
        calendar = renewal_calendar(events, today=self.TODAY)
        by_product = {r["product"]: r["status"] for r in calendar}
        self.assertEqual(by_product["expired"], "expired")
        self.assertEqual(by_product["soon"], "due_soon")
        self.assertEqual(by_product["ok"], "ok")
        self.assertEqual(by_product["undated"], "unknown")
        # Soonest first; undated last.
        self.assertEqual([r["product"] for r in calendar], ["expired", "soon", "ok", "undated"])

    def test_calendar_rows_carry_evidence_doc_id(self):
        calendar = renewal_calendar([proposed(doc_id="evidence-42")], today=self.TODAY)
        self.assertEqual(calendar[0]["doc_id"], "evidence-42")

    def test_discard_folds_away_all_products_backed_by_the_document(self):
        # The MultiCover mitigation on the renewal path: one discarded
        # document retracts every product row it backed — and only those.
        events = [
            proposed(product="motor", doc_id="d-multi"),
            proposed(product="home", premium=200.0, doc_id="d-multi"),
            proposed(product="pet", premium=80.0, doc_id="d-other"),
            DocumentDiscarded(doc_id="d-multi", reason="bundle total misread"),
        ]
        calendar = renewal_calendar(events, today=self.TODAY)
        self.assertEqual([r["product"] for r in calendar], ["pet"])

    def test_discard_latest_document_reveals_previous_active_renewal(self):
        # A discarded later document is retracted from history before the
        # latest-per-product fold. The prior proposal remains valid evidence
        # and must become visible again.
        events = [
            proposed(product="motor", premium=378.90, doc_id="d-proposal"),
            RenewalAccepted("d-multicover", "motor", 412.50, "2026-09-01"),
            RenewalAccepted("d-multicover", "home", 238.20, "2026-09-01"),
            DocumentDiscarded(doc_id="d-multicover", reason="wrong renewal document"),
        ]

        calendar = renewal_calendar(events, today=self.TODAY)

        self.assertEqual(len(calendar), 1)
        self.assertEqual(calendar[0]["product"], "motor")
        self.assertEqual(calendar[0]["state"], "RenewalProposed")
        self.assertEqual(calendar[0]["annual_premium"], 378.90)
        self.assertEqual(calendar[0]["doc_id"], "d-proposal")


class CurrentPoliciesTests(unittest.TestCase):
    """Ported from the old repo's ProjectionTests."""

    def test_latest_wins_per_entity(self):
        events = [
            filed(doc_id="d1", valid_to="2026-06-01"),
            filed(doc_id="d1b", valid_to="2027-06-01"),  # same entity re-filed later
        ]
        current = current_policies(events)
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["valid_to"], "2027-06-01")

    def test_policy_corrected_folds_like_filed(self):
        events = [
            filed(doc_id="d1", premium=500.0),
            PolicyCorrected(
                doc_id="d1", doc_type="policy_schedule", entity_id="POL-1",
                fields={"annual_premium": Field(512.0, 1.0, "£512.00")},
            ),
        ]
        current = current_policies(events)
        self.assertEqual(current[0]["state"], "PolicyCorrected")
        self.assertEqual(current[0]["fields"]["annual_premium"].value, 512.0)

    def test_discard_matching_backing_document_removes_entity(self):
        events = [filed(doc_id="d1"), DocumentDiscarded(doc_id="d1")]
        self.assertEqual(current_policies(events), [])

    def test_discard_of_other_document_keeps_policy(self):
        # Old repo's real-data regression (2026-07-08): discarding a
        # misextracted renewal invitation (same entity, different document)
        # must NOT delete the policy filed from the schedule.
        events = [
            filed(doc_id="d_schedule"),
            proposed(doc_id="d_renewal", premium=2219.70, entity_id="POL-1"),
            DocumentDiscarded(doc_id="d_renewal", reason="record-level semantic error"),
        ]
        current = current_policies(events)
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["doc_id"], "d_schedule")

    def test_renewal_proposed_is_invisible_to_current_policies(self):
        self.assertEqual(current_policies([proposed(entity_id="POL-1")]), [])

    def test_fields_carry_typed_provenance_through(self):
        current = current_policies([filed()])
        premium = current[0]["fields"]["annual_premium"]
        self.assertIsInstance(premium, Field)
        self.assertIn("£500.00", premium.source_text)


class ConflictingPolicyFiledTests(unittest.TestCase):
    """Ported: the chronofix.2 overwrite guard."""

    def test_detects_different_document_same_entity(self):
        events = [filed(doc_id="d1")]
        conflict = conflicting_policy_filed(filed(doc_id="d2"), events)
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["doc_id"], "d1")
        self.assertEqual(conflict["entity_id"], "POL-1")

    def test_allows_refiling_same_document(self):
        events = [filed(doc_id="d1")]
        self.assertIsNone(conflicting_policy_filed(filed(doc_id="d1"), events))

    def test_none_when_no_prior_state(self):
        self.assertIsNone(conflicting_policy_filed(filed(doc_id="d1"), []))


class RenewalOffersTests(unittest.TestCase):
    """Ported: offer/current-policy pairing with year-on-year delta."""

    def test_offer_pairs_with_current_policy_and_computes_delta(self):
        events = [
            filed(doc_id="d1", premium=500.0),
            proposed(doc_id="d2", premium=560.0, entity_id="POL-1"),
        ]
        offers = renewal_offers(events)
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer["entity_id"], "POL-1")
        self.assertEqual(offer["current_policy_doc_id"], "d1")
        self.assertTrue(offer["premium_change"]["comparable"])
        self.assertEqual(offer["premium_change"]["delta"], 60.0)
        self.assertEqual(offer["premium_change"]["pct_change"], 12.0)

    def test_offer_with_no_current_policy_is_not_comparable(self):
        offers = renewal_offers([proposed(doc_id="d2", entity_id="POL-9")])
        self.assertEqual(len(offers), 1)
        self.assertFalse(offers[0]["premium_change"]["comparable"])
        self.assertIsNone(offers[0]["current_policy_doc_id"])

    def test_unlinked_offer_is_not_comparable_until_entity_linking(self):
        offers = renewal_offers([filed(), proposed(doc_id="d2")])  # entity_id=None
        self.assertFalse(offers[0]["premium_change"]["comparable"])
        self.assertIn("2R.3", offers[0]["premium_change"]["reason"])

    def test_discarded_offer_is_excluded_but_policy_survives(self):
        # The false "+62.7% renewal shock": a MultiCover total misextracted
        # as a motor quote. The discard retracts the offer from the
        # projection while the log keeps both events.
        events = [
            filed(doc_id="d_schedule"),
            proposed(doc_id="d_renewal", premium=2219.70, entity_id="POL-1"),
        ]
        self.assertEqual(len(renewal_offers(events)), 1)
        events.append(
            DocumentDiscarded(doc_id="d_renewal", reason="quoted_premium was the multi-policy total")
        )
        self.assertEqual(renewal_offers(events), [])
        self.assertEqual(len(current_policies(events)), 1)  # policy untouched


class DispatchTests(unittest.TestCase):
    """Ported: which doc types become which facts (event_for_confirmed_record)."""

    @staticmethod
    def _extraction(doc_type, fields):
        return FieldExtraction(doc_id="d9", doc_type=doc_type, fields=fields)

    def test_policy_schedule_dispatches_to_policy_filed(self):
        extraction = self._extraction(
            "policy_schedule",
            {
                "policy_number": Field("POL-1", 0.95, "Policy number POL-1"),
                "policy_end_date": Field("2026-06-01", 0.95, "to 01 June 2026"),
                "period_start_date": Field("2025-06-01", 0.9, "from 01 June 2025"),
                "provider": Field("SwiftSure", 0.9, "SwiftSure"),
            },
        )
        event = event_for_fields(extraction, REGISTRY["policy_schedule"])
        self.assertIsInstance(event, PolicyFiled)
        self.assertEqual(event.entity_id, "POL-1")  # uses policy_number
        self.assertEqual(event.valid_from, "2025-06-01")  # from schema valid_from_source
        self.assertEqual(event.valid_to, "2026-06-01")
        self.assertEqual(event.provider, "SwiftSure")

    def test_entity_id_falls_back_to_doc_id(self):
        extraction = self._extraction(
            "policy_schedule", {"policy_end_date": Field("2026-06-01", 0.95)}
        )
        event = event_for_fields(extraction, REGISTRY["policy_schedule"])
        self.assertEqual(event.entity_id, "d9")

    def test_ncd_letter_dispatches_to_ncd_confirmed(self):
        extraction = self._extraction(
            "ncd_letter",
            {"ncd_years": Field(5, 0.95, "5 years"), "policy_number": Field("POL-1", 0.9)},
        )
        event = event_for_fields(extraction, REGISTRY["ncd_letter"])
        self.assertIsInstance(event, NcdConfirmed)
        self.assertEqual(event.entity_id, "POL-1")

    def test_certificate_dispatches_to_no_event(self):
        extraction = self._extraction("certificate", {"policy_number": Field("POL-1", 0.9)})
        self.assertIsNone(event_for_fields(extraction, REGISTRY["certificate"]))
        self.assertTrue(has_event_vocabulary("certificate"))

    def test_unmapped_doc_type_raises_loudly(self):
        # An unmapped doc_type must fail loudly, not silently no-op — the
        # registry and the dispatch map have drifted apart (old-repo test).
        extraction = self._extraction("eye_prescription", {})
        self.assertFalse(has_event_vocabulary("eye_prescription"))
        with self.assertRaises(ValueError):
            event_for_fields(extraction, REGISTRY["eye_prescription"])


if __name__ == "__main__":
    unittest.main()
