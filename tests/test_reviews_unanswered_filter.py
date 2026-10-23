"""Increment 1 (Issue #71): the `unanswered` filter on GET /api/reviews.

Exercises the real server-side filtering function with row dicts shaped exactly like
`_review_inbox_rows()` produces, so the salon scoping, the unanswered filter, the newest-first
ordering and the pagination combination are all proven without touching the network or a
database.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from gbp.api.server import _reviews_page  # noqa: E402

SALON_A = "loc-101"
SALON_B = "loc-202"


def query(**kw):
    """Mirror production: values arrive from parse_qs as single-item lists."""
    return {key: [str(value)] for key, value in kw.items()}


def row(review_id, location, status, date, stars=5, business="Salon A"):
    return {"review_id": review_id, "location_id": location, "location_resource_id": location,
            "status": status, "draft_status": None if status == "no_draft" else status,
            "has_draft": status != "no_draft", "stars": stars, "review_date": date,
            "business_name": business, "comment": "c", "reviewer_name": "r", "language": "en"}


def rows():
    return [
        row(1, SALON_A, "no_draft", "2026-01-01T00:00:00Z"),
        row(2, SALON_A, "draft", "2026-03-01T00:00:00Z"),
        row(3, SALON_A, "approved", "2026-02-01T00:00:00Z"),
        row(4, SALON_B, "no_draft", "2026-04-01T00:00:00Z", business="Salon B"),
        row(5, SALON_B, "sent", "2025-12-01T00:00:00Z", business="Salon B"),
    ]


class UnansweredFilterTests(unittest.TestCase):
    def test_unanswered_keeps_reviews_without_an_accepted_reply(self):
        out = _reviews_page(rows(), query(unanswered="1", page_size="50"))
        self.assertEqual({i["review_id"] for i in out["items"]}, {1, 2, 4})

    def test_approved_and_sent_replies_are_treated_as_answered(self):
        out = _reviews_page(rows(), query(unanswered="true", page_size="50"))
        kept = {i["review_id"] for i in out["items"]}
        self.assertNotIn(3, kept)   # approved
        self.assertNotIn(5, kept)   # sent

    def test_without_the_flag_every_review_is_returned(self):
        out = _reviews_page(rows(), query(page_size="50"))
        self.assertEqual(out["total"], 5)

    def test_the_filter_is_scoped_to_one_salon(self):
        out = _reviews_page(rows(), query(unanswered="1", location_id=SALON_B, page_size="50"))
        self.assertEqual({i["review_id"] for i in out["items"]}, {4})

    def test_unanswered_results_are_newest_first_by_default(self):
        out = _reviews_page(rows(), query(unanswered="1", location_id=SALON_A, page_size="50"))
        self.assertEqual([i["review_id"] for i in out["items"]], [2, 1])
        self.assertEqual(out["sort"], "review_date")
        self.assertEqual(out["order"], "desc")

    def test_the_filter_combines_with_a_star_filter_and_pagination(self):
        seeded = rows() + [row(6, SALON_A, "no_draft", "2026-05-01T00:00:00Z", stars=2)]
        out = _reviews_page(seeded, query(unanswered="1", location_id=SALON_A, stars="2", page_size="1", page="1"))
        self.assertEqual(out["total"], 1)
        self.assertEqual([i["review_id"] for i in out["items"]], [6])
        self.assertEqual(out["pages"], 1)

if __name__ == "__main__":
    unittest.main()
