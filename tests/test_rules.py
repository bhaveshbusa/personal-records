"""Shape rules, cross-checks, and routing decisions (pure, offline)."""

import unittest

from records.core import (
    RENEWAL_ALREADY_ACCEPTED,
    RENEWAL_PROPOSED,
    Extraction,
    Field,
    ProductLine,
    RenewalProposed,
    Shape,
)
from records.review import cross_check_issues, decide, shape_issues


def single_motor(premium: float = 350.00, confidence: float = 0.9) -> Extraction:
    return Extraction(
        doc_id="doc-1",
        shape=Shape(line_count=1, renewal_status=RENEWAL_PROPOSED),
        lines=(ProductLine("motor", Field(premium, confidence, "Premium: £{:.2f}".format(premium))),),
        stated_total=Field(premium, confidence, "Total: £{:.2f}".format(premium)),
    )


class TestShapeIssues(unittest.TestCase):
    def test_clean_single_line_proposed_has_no_issues(self):
        self.assertEqual(shape_issues(Shape(line_count=1, renewal_status=RENEWAL_PROPOSED)), [])

    def test_unsure_is_terminal(self):
        issues = shape_issues(Shape(line_count=1, renewal_status=RENEWAL_PROPOSED, unsure=True))
        self.assertEqual(len(issues), 1)
        self.assertIn("unsure", issues[0])

    def test_multi_line_flagged(self):
        issues = shape_issues(Shape(line_count=2, renewal_status=RENEWAL_PROPOSED))
        self.assertTrue(any("multi-line" in i for i in issues))

    def test_already_accepted_flagged(self):
        issues = shape_issues(Shape(line_count=1, renewal_status=RENEWAL_ALREADY_ACCEPTED))
        self.assertTrue(any("already accepted" in i for i in issues))

    def test_missing_line_count_and_status_flagged(self):
        issues = shape_issues(Shape())
        self.assertEqual(len(issues), 2)


class TestCrossChecks(unittest.TestCase):
    def test_clean_extraction_passes(self):
        self.assertEqual(cross_check_issues(single_motor()), [])

    def test_sum_of_lines_vs_stated_total_mismatch(self):
        e = single_motor()
        bad = Extraction(e.doc_id, e.shape, e.lines, stated_total=Field(999.99, 0.9))
        issues = cross_check_issues(bad)
        self.assertTrue(any("stated_total" in i for i in issues))

    def test_shape_extraction_line_count_mismatch(self):
        e = single_motor()
        bad = Extraction(e.doc_id, Shape(line_count=2, renewal_status=RENEWAL_PROPOSED), e.lines, e.stated_total)
        issues = cross_check_issues(bad)
        self.assertTrue(any("mismatch" in i for i in issues))

    def test_renewal_band_inside_passes_outside_fails(self):
        # prior 300 → 350 is +16.7%: inside ±40%.
        self.assertEqual(cross_check_issues(single_motor(350.00), prior_year_premium=300.00), [])
        # prior 200 → 350 is +75%: outside.
        issues = cross_check_issues(single_motor(350.00), prior_year_premium=200.00)
        self.assertTrue(any("band" in i for i in issues))
        # Drops are checked too: prior 700 → 350 is -50%.
        issues = cross_check_issues(single_motor(350.00), prior_year_premium=700.00)
        self.assertTrue(any("band" in i for i in issues))


class TestDecide(unittest.TestCase):
    def test_clean_single_line_emits_renewal_proposed(self):
        decision = decide(single_motor(350.00), prior_year_premium=320.00)
        self.assertTrue(decision.accepted)
        self.assertEqual(len(decision.events), 1)
        event = decision.events[0]
        self.assertIsInstance(event, RenewalProposed)
        self.assertEqual(event.product, "motor")
        self.assertEqual(event.annual_premium, 350.00)
        self.assertIn("£350.00", event.provenance.source_text)

    def test_no_prior_year_still_flows(self):
        self.assertTrue(decide(single_motor()).accepted)

    def test_any_issue_means_zero_events(self):
        e = single_motor()
        bad = Extraction(e.doc_id, Shape(unsure=True), e.lines, e.stated_total)
        decision = decide(bad)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.events, ())

    def test_line_without_premium_routes_to_review(self):
        e = Extraction(
            doc_id="doc-2",
            shape=Shape(line_count=1, renewal_status=RENEWAL_PROPOSED),
            lines=(ProductLine("motor", None),),
        )
        decision = decide(e)
        self.assertFalse(decision.accepted)
        self.assertIn("no premium", decision.review_reasons[0])


if __name__ == "__main__":
    unittest.main()
