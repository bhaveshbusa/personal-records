"""Quote-ready profile (2R.5): the old repo's `test_quote_profile.py` ported
onto typed events — fold precedence (NCD letter beats schedule), null-safe
empty state, per-entry trust/provenance, and the JSON/Markdown exports."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from records.core import (
    TRUST_EXTRACTED,
    TRUST_VERIFIED,
    Field,
    NcdConfirmed,
    PolicyFiled,
    RenewalProposed,
)
from records.query.quote_profile import (
    export_profile,
    profile_completeness,
    quote_profile,
    to_json,
    to_markdown,
)

TODAY = date(2026, 7, 13)


def f(value, confidence=0.95):
    return Field(value, confidence, str(value))


class QuoteProfileTests(unittest.TestCase):
    def setUp(self):
        self.events = [
            PolicyFiled(
                doc_id="schedule-1",
                doc_type="policy_schedule",
                entity_id="POL-1",
                fields={
                    "vehicle_registration": f("AB12 CDE"),
                    "cover_level": f("Comprehensive"),
                    "compulsory_excess": f(250),
                    "voluntary_excess": f(100),
                    "annual_premium": f(512.30),
                    "period_start_date": f("2025-08-19"),
                    "policy_end_date": f("2026-08-18"),
                    "ncd_years": f(4),
                    "named_drivers": f("A. Customer"),
                },
                valid_from="2025-08-19",
                valid_to="2026-08-18",
                provider="Aviva",
                trust=TRUST_VERIFIED,
            ),
            NcdConfirmed(
                doc_id="ncd-1",
                doc_type="ncd_letter",
                entity_id="POL-1",
                fields={"ncd_years": f(5)},
                trust=TRUST_VERIFIED,
            ),
            RenewalProposed(
                doc_id="renewal-1",
                product="motor",
                annual_premium=560.0,
                provenance=f(560.0),
                renewal_date="2026-08-19",
                entity_id="POL-1",
            ),
        ]
        self.documents = [
            {"doc_id": "claim-1", "doc_type": "claim_evidence", "ingested_at": "2024-03-02T10:00:00+00:00"}
        ]

    def test_fold_and_ncd_event_precedence(self):
        profile = quote_profile(self.events, self.documents, TODAY)
        self.assertEqual(profile["vehicle"]["registration"]["value"], "AB12 CDE")
        self.assertEqual(profile["cover"]["current_provider"]["value"], "Aviva")
        self.assertEqual(profile["history"]["ncd_years"]["value"], 5)  # letter beats schedule's 4
        self.assertEqual(profile["history"]["ncd_years"]["source_doc_id"], "ncd-1")
        self.assertEqual(profile["history"]["ncd_years"]["source_kind"], "NcdConfirmed")
        self.assertEqual(profile["history"]["claims_last_5_years"]["value"]["count"], 1)
        self.assertEqual(profile["history"]["claims_last_5_years"]["basis"], "documents on file")
        self.assertEqual(len(profile["history"]["premium_history"]["value"]), 2)

    def test_trust_propagates_per_entry(self):
        profile = quote_profile(self.events, self.documents, TODAY)
        self.assertEqual(profile["vehicle"]["registration"]["trust"], TRUST_VERIFIED)
        self.assertEqual(profile["history"]["ncd_years"]["trust"], TRUST_VERIFIED)
        # History mixes a verified filing and an extracted quote → weakest wins.
        self.assertEqual(profile["history"]["premium_history"]["trust"], TRUST_EXTRACTED)

    def test_claims_older_than_five_years_are_excluded(self):
        old_claim = [
            {"doc_id": "claim-0", "doc_type": "claim_evidence", "ingested_at": "2019-01-01T00:00:00+00:00"}
        ]
        profile = quote_profile(self.events, old_claim, TODAY)
        self.assertEqual(profile["history"]["claims_last_5_years"]["value"]["count"], 0)

    def test_missing_fields_are_present_as_null(self):
        profile = quote_profile([], [], TODAY)
        self.assertIsNone(profile["vehicle"]["registration"]["value"])
        self.assertIsNone(profile["cover"]["cover_level"]["value"])
        self.assertIsNone(profile["history"]["ncd_years"]["value"])

    def test_completeness_counts_leaves(self):
        populated, total = profile_completeness(quote_profile(self.events, self.documents, TODAY))
        self.assertGreater(populated, 0)
        self.assertGreater(total, populated)  # make/model unknown in this fixture

    def test_json_and_markdown_exports(self):
        profile = quote_profile(self.events, self.documents, TODAY)
        self.assertEqual(json.loads(to_json(profile))["vehicle"]["registration"]["value"], "AB12 CDE")
        markdown = to_markdown(profile)
        self.assertIn("## Your vehicle", markdown)
        self.assertIn("## Your cover", markdown)
        self.assertIn("## Your history", markdown)
        self.assertIn("AB12 CDE", markdown)
        self.assertIn("trust: verified; source: schedule-1", markdown)

        with tempfile.TemporaryDirectory() as tmp:
            paths = export_profile(profile, root=Path(tmp))
            self.assertTrue(Path(paths["json_path"]).exists())
            self.assertTrue(Path(paths["markdown_path"]).exists())
            self.assertIn("exports", paths["json_path"])


if __name__ == "__main__":
    unittest.main()
