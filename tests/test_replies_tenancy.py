"""Increment 1 tests: multi-tenant scoping and the unanswered-review view (Issue #71)."""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from gbp.replies.models import Reply  # noqa: E402
from gbp.replies.tenancy import (  # noqa: E402
    location_ids, location_summary, newest_first, replies_for_location, reviews_for_location,
    select_reviews, unanswered_reviews,
)

SALON_A = "accounts/9001/locations/loc-101"
SALON_B = "accounts/9001/locations/loc-202"


def dataset():
    return {"dataset_version": 1, "locations": [
        {"location": SALON_A, "reviews": [
            {"reviewId": "a-1", "rating": 5, "comment": "Great", "createTime": "2026-01-01T00:00:00Z"},
            {"reviewId": "a-2", "rating": 2, "comment": "Late", "createTime": "2026-03-01T00:00:00Z"},
            {"reviewId": "a-3", "rating": 4, "comment": "Nice", "createTime": "2026-02-01T00:00:00Z"},
        ]},
        {"location": SALON_B, "reviews": [
            {"reviewId": "b-1", "rating": 5, "comment": "Perfect", "createTime": "2026-04-01T00:00:00Z"},
        ]},
        {"location": "accounts/9001/locations/loc-303", "reviews": []},
    ]}


def reply(review_id, location_id, state="approved"):
    return Reply(reply_id="reply-" + review_id, review_id=review_id, location_id=location_id,
                 text="thanks", state=state)


class ScopingTests(unittest.TestCase):
    def test_locations_are_listed_stably(self):
        self.assertEqual(location_ids(dataset()), sorted([SALON_A, SALON_B,
                                                         "accounts/9001/locations/loc-303"]))

    def test_a_location_never_sees_another_locations_reviews(self):
        a = reviews_for_location(dataset(), SALON_A)
        b = reviews_for_location(dataset(), SALON_B)
        self.assertEqual({r["reviewId"] for r in a}, {"a-1", "a-2", "a-3"})
        self.assertEqual({r["reviewId"] for r in b}, {"b-1"})
        self.assertFalse({r["reviewId"] for r in a} & {r["reviewId"] for r in b})

    def test_unknown_or_empty_location_returns_nothing(self):
        for bad in ("accounts/9999/locations/nope", "", None):
            self.assertEqual(reviews_for_location(dataset(), bad), [])

    def test_a_location_without_reviews_is_empty_not_everything(self):
        self.assertEqual(reviews_for_location(dataset(), "accounts/9001/locations/loc-303"), [])

    def test_replies_are_scoped_to_their_location(self):
        replies = [reply("a-1", SALON_A), reply("b-1", SALON_B)]
        self.assertEqual([r.review_id for r in replies_for_location(replies, SALON_B)], ["b-1"])
        self.assertEqual(replies_for_location(replies, "unknown"), [])


class UnansweredTests(unittest.TestCase):
    def test_reviews_without_a_reply_are_unanswered(self):
        pending = unanswered_reviews(dataset(), SALON_A)
        self.assertEqual({r["reviewId"] for r in pending}, {"a-1", "a-2", "a-3"})

    def test_an_approved_reply_marks_its_own_review_answered(self):
        pending = unanswered_reviews(dataset(), SALON_A, [reply("a-2", SALON_A)])
        self.assertEqual({r["reviewId"] for r in pending}, {"a-1", "a-3"})

    def test_a_reply_for_another_salon_does_not_mark_this_review_answered(self):
        pending = unanswered_reviews(dataset(), SALON_A, [reply("a-2", SALON_B)])
        self.assertIn("a-2", {r["reviewId"] for r in pending})

    def test_a_reply_edited_back_to_draft_needs_attention_again(self):
        pending = unanswered_reviews(dataset(), SALON_A, [reply("a-2", SALON_A, state="draft")])
        self.assertIn("a-2", {r["reviewId"] for r in pending})

    def test_identical_review_ids_in_two_salons_stay_distinct(self):
        data = {"locations": [
            {"location": SALON_A, "reviews": [{"reviewId": "x", "createTime": "2026-01-01T00:00:00Z"}]},
            {"location": SALON_B, "reviews": [{"reviewId": "x", "createTime": "2026-01-02T00:00:00Z"}]}]}
        self.assertEqual(len(unanswered_reviews(data, SALON_A, [reply("x", SALON_B)])), 1)
        self.assertEqual(len(unanswered_reviews(data, SALON_B, [reply("x", SALON_B)])), 0)


class OrderingAndScaleTests(unittest.TestCase):
    def test_newest_review_comes_first(self):
        ordered = [r["reviewId"] for r in newest_first(reviews_for_location(dataset(), SALON_A))]
        self.assertEqual(ordered, ["a-2", "a-3", "a-1"])

    def test_reviews_without_a_timestamp_sort_last_and_are_kept(self):
        ordered = newest_first([{"reviewId": "no-date"}, {"reviewId": "dated",
                                                          "createTime": "2026-01-01T00:00:00Z"}])
        self.assertEqual([r["reviewId"] for r in ordered], ["dated", "no-date"])

    def test_select_returns_unanswered_newest_first_with_a_limit(self):
        chosen = select_reviews(dataset(), SALON_A, replies=[reply("a-2", SALON_A)], limit=2)
        self.assertEqual([r["reviewId"] for r in chosen], ["a-3", "a-1"])

    def test_select_can_include_answered_reviews_when_asked(self):
        chosen = select_reviews(dataset(), SALON_A, unanswered_only=False,
                                replies=[reply("a-2", SALON_A)])
        self.assertEqual(len(chosen), 3)

    def test_summary_counts_per_salon(self):
        summary = {row["location"]: row for row in location_summary(dataset(), [reply("a-1", SALON_A)])}
        self.assertEqual(summary[SALON_A]["reviews"], 3)
        self.assertEqual(summary[SALON_A]["unanswered"], 2)
        self.assertEqual(summary[SALON_B]["unanswered"], 1)
        self.assertEqual(summary["accounts/9001/locations/loc-303"]["reviews"], 0)

    def test_large_dataset_is_scoped_quickly_and_correctly(self):
        big = {"locations": [
            {"location": "loc-%04d" % i,
             "reviews": [{"reviewId": "r-%d-%d" % (i, j), "createTime":
                          "2026-01-%02dT00:00:00Z" % (j % 28 + 1)} for j in range(20)]}
            for i in range(500)]}
        started = time.time()
        chosen = select_reviews(big, "loc-0499", limit=5)
        elapsed = time.time() - started
        self.assertEqual(len(chosen), 5)
        self.assertTrue(all(r["reviewId"].startswith("r-499-") for r in chosen))
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(location_summary(big)), 500)


if __name__ == "__main__":
    unittest.main()
