"""Entity linker — deterministic Link stage (offline, zero-LLM).

Ported from the old repo's `test_entity_linker.py` (2R.3), adapted to the
typed LinkResult. One deliberate routing change is covered here and in
test_rules: no-match documents are accepted UNLINKED (the linker reports
NO_MATCH; only AMBIGUOUS parks for review) — see core/linking.py.
"""

import unittest

from records.core import (
    AMBIGUOUS,
    Field,
    LINKED,
    NEW_ENTITY,
    NO_IDENTIFIERS,
    NO_MATCH,
    PolicyFiled,
    known_entities,
    link_document,
)
from records.core.linking import LINK_NEW_ENTITY, LINK_POLICY_NUMBER, LINK_VEHICLE_REG

KNOWN_ENTITIES = [
    {"entity_id": "POL-1", "policy_number": "POL-1", "vehicle_registration": "AB12 CDE"},
    {"entity_id": "POL-2", "policy_number": "POL-2", "vehicle_registration": "XY99 ZZZ"},
]


class EntityLinkerTests(unittest.TestCase):
    def test_exact_policy_number_match_wins(self):
        out = link_document(
            {"policy_number": "POL-1", "vehicle_registration": "XY99 ZZZ"},
            KNOWN_ENTITIES,
            doc_type="certificate",
        )
        self.assertEqual(out.entity_id, "POL-1")
        self.assertEqual(out.method, LINK_POLICY_NUMBER)
        self.assertEqual(out.status, LINKED)
        self.assertTrue(out.linked)

    def test_vehicle_registration_match_when_no_policy_number(self):
        out = link_document({"vehicle_registration": "AB12 CDE"}, KNOWN_ENTITIES, doc_type="certificate")
        self.assertEqual(out.entity_id, "POL-1")
        self.assertEqual(out.method, LINK_VEHICLE_REG)

    def test_vehicle_registration_normalised_case_and_spacing(self):
        out = link_document({"vehicle_registration": "ab12cde"}, KNOWN_ENTITIES, doc_type="certificate")
        self.assertEqual(out.entity_id, "POL-1")
        self.assertEqual(out.method, LINK_VEHICLE_REG)

    def test_typed_field_identifiers_compare_on_raw_value(self):
        out = link_document(
            {"policy_number": Field("POL-2", 0.95, "Policy number POL-2")},
            KNOWN_ENTITIES,
            doc_type="renewal_quote",
        )
        self.assertEqual(out.entity_id, "POL-2")

    def test_policy_schedule_establishes_new_entity_when_no_match(self):
        out = link_document(
            {"policy_number": "POL-9", "vehicle_registration": "NEW1 REG"},
            KNOWN_ENTITIES,
            doc_type="policy_schedule",
        )
        self.assertEqual(out.status, NEW_ENTITY)
        self.assertEqual(out.entity_id, "POL-9")
        self.assertEqual(out.method, LINK_NEW_ENTITY)
        self.assertTrue(out.linked)

    def test_new_entity_keys_on_normalised_reg_without_policy_number(self):
        out = link_document({"vehicle_registration": "new1 reg"}, KNOWN_ENTITIES, doc_type="policy_schedule")
        self.assertEqual(out.status, NEW_ENTITY)
        self.assertEqual(out.entity_id, "NEW1REG")

    def test_non_establishing_type_with_no_match_reports_no_match(self):
        # Old repo parked this for review; new routing accepts it unlinked
        # (rules only park AMBIGUOUS) — the status is still reported.
        out = link_document({"vehicle_registration": "NEW1 REG"}, KNOWN_ENTITIES, doc_type="certificate")
        self.assertEqual(out.status, NO_MATCH)
        self.assertIsNone(out.entity_id)
        self.assertFalse(out.linked)

    def test_no_fields_to_match_on_reports_no_identifiers(self):
        out = link_document({}, KNOWN_ENTITIES, doc_type="claim_evidence")
        self.assertEqual(out.status, NO_IDENTIFIERS)
        self.assertIsNone(out.method)

    def test_new_entity_type_without_identifier_stays_unlinked(self):
        # policy_schedule would normally mint a new entity, but with neither
        # a policy_number nor a vehicle_registration there is nothing to key
        # it on — dispatch falls back to doc_id downstream.
        out = link_document({}, KNOWN_ENTITIES, doc_type="policy_schedule")
        self.assertEqual(out.status, NO_IDENTIFIERS)
        self.assertIsNone(out.entity_id)

    def test_ambiguous_vehicle_registration_match(self):
        duplicated = KNOWN_ENTITIES + [
            {"entity_id": "POL-3", "policy_number": "POL-3", "vehicle_registration": "AB12 CDE"},
        ]
        out = link_document({"vehicle_registration": "AB12 CDE"}, duplicated, doc_type="certificate")
        self.assertEqual(out.status, AMBIGUOUS)
        self.assertIsNone(out.entity_id)
        self.assertIn("vehicle_registration", out.reason)

    def test_ambiguous_policy_number_match(self):
        duplicated = KNOWN_ENTITIES + [
            {"entity_id": "POL-1-dup", "policy_number": "POL-1", "vehicle_registration": "ZZ00 ZZZ"},
        ]
        out = link_document({"policy_number": "POL-1"}, duplicated, doc_type="certificate")
        self.assertEqual(out.status, AMBIGUOUS)
        self.assertIn("policy_number", out.reason)


class KnownEntitiesTests(unittest.TestCase):
    def test_built_from_current_policies_projection(self):
        events = [
            PolicyFiled(
                doc_id="d1",
                doc_type="policy_schedule",
                entity_id="POL-1",
                fields={
                    "policy_number": Field("POL-1", 0.95, "POL-1"),
                    "vehicle_registration": Field("AB12 CDE", 0.95, "AB12 CDE"),
                },
            )
        ]
        entities = known_entities(events)
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["entity_id"], "POL-1")
        self.assertEqual(entities[0]["policy_number"], "POL-1")
        self.assertEqual(entities[0]["vehicle_registration"], "AB12 CDE")

    def test_unpriced_identifierless_policy_still_appears(self):
        events = [PolicyFiled(doc_id="d1", doc_type="policy_schedule", entity_id="d1")]
        entities = known_entities(events)
        self.assertEqual(entities[0]["policy_number"], None)


if __name__ == "__main__":
    unittest.main()
