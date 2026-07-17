"""Doc-type classification (2R.1) — ported from the old repo's
test_doc_classifier.py onto the new model.

The LLM wrapper (`classify_doc_type`) and the deterministic routing
(`doc_type_issues`) are separate now, so the old `classify_document` tests
split accordingly: fail-safe parsing tests hit the wrapper with a fake LLM;
threshold/registry routing tests hit the pure rule directly. Offline, pure
stdlib.
"""

import json
import unittest

from records.core import REGISTRY, ROLES, UNKNOWN_DOC_TYPE, DocTypeVerdict
from records.extract import FakeLLMClient, classify_doc_type
from records.review import doc_type_issues
from records.review.rules import DOC_TYPE_CONFIDENCE_THRESHOLD


def _response(doc_type, confidence, rationale="fixture"):
    return json.dumps({"doc_type": doc_type, "confidence": confidence, "rationale": rationale})


class RegistryTests(unittest.TestCase):
    def test_all_thirteen_old_repo_types_are_restored(self):
        self.assertEqual(
            set(REGISTRY),
            {
                "policy_schedule",
                "renewal_quote",
                "share_contract_note",
                "certificate",
                "ncd_letter",
                "policy_wording",
                "claim_evidence",
                "eye_prescription",
                "payslip",
                "council_tax_bill",
                "utility_bill",
                "passport",
                "vehicle_mot_certificate",
            },
        )

    def test_schemas_are_internally_consistent(self):
        for doc_type, schema in REGISTRY.items():
            self.assertEqual(schema.doc_type, doc_type)
            self.assertIn(schema.role, ROLES)
            self.assertTrue(schema.description)
            canonical = set(schema.canonical_fields)
            self.assertTrue(set(schema.required) <= canonical, doc_type)
            for source in (schema.valid_from_source, schema.valid_to_source, schema.provider_field):
                if source is not None:
                    self.assertIn(source, canonical, doc_type)

    def test_unknown_is_never_a_registry_type(self):
        self.assertNotIn(UNKNOWN_DOC_TYPE, REGISTRY)

    def test_only_quote_like_type_is_renewal_quote(self):
        self.assertEqual(
            [dt for dt, s in REGISTRY.items() if s.quote_like], ["renewal_quote"]
        )


class ClassifyDocTypeTests(unittest.TestCase):
    def test_well_formed_response_becomes_verdict(self):
        llm = FakeLLMClient([_response("policy_schedule", 0.95, "schedule header")])
        verdict, response = classify_doc_type("POLICY SCHEDULE ...", llm)
        self.assertEqual(verdict, DocTypeVerdict("policy_schedule", 0.95, "schedule header"))
        self.assertEqual(response.model, "fake")

    def test_registry_summary_is_in_the_prompt(self):
        llm = FakeLLMClient([_response("payslip", 0.9)])
        classify_doc_type("payslip text", llm)
        system = llm.calls[0]["system"]
        for doc_type in REGISTRY:
            self.assertIn(doc_type, system)

    def test_unparseable_response_fails_safe_to_unknown(self):
        llm = FakeLLMClient(["the model rambled instead of emitting JSON"])
        verdict, _ = classify_doc_type("whatever", llm)
        self.assertEqual(verdict.doc_type, UNKNOWN_DOC_TYPE)
        self.assertEqual(verdict.confidence, 0.0)

    def test_out_of_range_confidence_fails_safe_to_unknown(self):
        llm = FakeLLMClient([_response("certificate", 1.7)])
        verdict, _ = classify_doc_type("whatever", llm)
        self.assertEqual(verdict.doc_type, UNKNOWN_DOC_TYPE)

    def test_fenced_json_is_tolerated(self):
        llm = FakeLLMClient(["```json\n" + _response("passport", 0.9) + "\n```"])
        verdict, _ = classify_doc_type("passport page", llm)
        self.assertEqual(verdict.doc_type, "passport")


class DocTypeIssuesTests(unittest.TestCase):
    """Routing semantics ported one-for-one from the old classify_document."""

    def test_confident_known_type_needs_no_review(self):
        self.assertEqual(doc_type_issues(DocTypeVerdict("policy_schedule", 0.95)), [])

    def test_below_threshold_confidence_routes_to_review(self):
        issues = doc_type_issues(DocTypeVerdict("certificate", 0.5))
        self.assertTrue(any("below threshold" in i for i in issues))

    def test_confidence_exactly_at_threshold_is_trusted(self):
        self.assertEqual(
            doc_type_issues(DocTypeVerdict("policy_schedule", DOC_TYPE_CONFIDENCE_THRESHOLD)), []
        )

    def test_unknown_doc_type_routes_to_review(self):
        issues = doc_type_issues(DocTypeVerdict(UNKNOWN_DOC_TYPE, 0.9))
        self.assertTrue(any("unknown document type" in i for i in issues))

    def test_doc_type_outside_registry_routes_to_review(self):
        # High confidence but the slug isn't in the registry — misfiled is
        # worse than unextracted, so this still needs review.
        issues = doc_type_issues(DocTypeVerdict("some_new_type", 0.99))
        self.assertTrue(any("not in the schema registry" in i for i in issues))

    def test_custom_threshold_is_respected(self):
        self.assertEqual(
            doc_type_issues(DocTypeVerdict("policy_schedule", 0.6), confidence_threshold=0.5), []
        )

    def test_forced_fit_type_still_routes_by_confidence_alone(self):
        # The prompt prefers "unknown" over a forced fit — that's the model's
        # judgment call, not a rule enforced here. A specific type at high
        # confidence is enough to skip review.
        self.assertEqual(doc_type_issues(DocTypeVerdict("certificate", 0.85)), [])


if __name__ == "__main__":
    unittest.main()
