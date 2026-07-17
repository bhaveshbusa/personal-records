"""Golden regression: the MultiCover failure must stay impossible.

The founding failure (old repo, 2026-07-08): a MultiCover motor+home
auto-renewal invitation was extracted as a single-policy motor quote — the
bundle total (£650.70) mapped into the premium field with confidence 1.0
and correct provenance, producing a false +62.7% renewal delta against the
prior-year motor premium (£400). Schema-valid but reality-invalid;
field-level confidence cannot catch this class.

Synthetic twin: examples/multicover_renewal_invitation.txt.
Expected outcome, permanently: review queue, zero renewal events.
"""

import unittest

from records.core import (
    RENEWAL_ALREADY_ACCEPTED,
    RENEWAL_PROPOSED,
    Extraction,
    Field,
    ProductLine,
    Shape,
)
from records.review import decide

PRIOR_YEAR_MOTOR_PREMIUM = 400.00


def multicover_twin() -> Extraction:
    """The correct extraction of the synthetic twin under the new model."""
    return Extraction(
        doc_id="golden-multicover",
        shape=Shape(line_count=2, renewal_status=RENEWAL_ALREADY_ACCEPTED),
        lines=(
            ProductLine("motor", Field(412.50, 0.95, "Motor insurance ... £412.50")),
            ProductLine("home", Field(238.20, 0.95, "Home insurance ... £238.20")),
        ),
        stated_total=Field(650.70, 0.95, "Total amount payable ... £650.70"),
    )


class TestGoldenMultiCover(unittest.TestCase):
    def test_twin_routes_to_review_never_events(self):
        decision = decide(multicover_twin(), prior_year_premium=PRIOR_YEAR_MOTOR_PREMIUM)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.events, ())
        reasons = "\n".join(decision.review_reasons)
        self.assertIn("multi-line", reasons)
        self.assertIn("already accepted", reasons)

    def test_old_failure_mode_is_caught_even_if_shape_misses(self):
        """The historical misread: one motor line carrying the bundle total
        with confidence 1.0. Even if the classifier wrongly says n=1 and
        proposed, the deterministic layers still refuse it."""
        misread = Extraction(
            doc_id="golden-multicover-misread",
            shape=Shape(line_count=1, renewal_status=RENEWAL_PROPOSED),
            lines=(ProductLine("motor", Field(650.70, 1.0, "Total amount payable ... £650.70")),),
            stated_total=None,
        )
        decision = decide(misread, prior_year_premium=PRIOR_YEAR_MOTOR_PREMIUM)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.events, ())
        # 650.70 vs 400.00 = +62.7% — outside the ±40% band.
        self.assertIn("+62.7%", "\n".join(decision.review_reasons))


if __name__ == "__main__":
    unittest.main()
