"""Fixture-coverage tests for the review gaps G1-G18 (and defects D1-D3).

Every gap listed in ``docs/FIXTURE-REVIEW.md`` must be exercisable from the
local fixtures; each test below asserts the *data shape* that closes one gap so
a regression (deleting the case from the fixture) fails the suite.

Run from the project root:

    cd /opt/data/gbp-integration
    PYTHONPATH=src python3 -m unittest discover -s tests -v

Standard library only; no network access.
"""

from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp import fixtures  # noqa: E402
from gbp.client import MockGBPClient  # noqa: E402
from gbp.repo import Repository  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")

LOC1 = "accounts/1001/locations/loc-001"
LOC2 = "accounts/1001/locations/loc-002"
LOC5 = "accounts/1001/locations/loc-005"

STAR_TO_INT = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}

SENSITIVE_CATEGORIES = {
    "health_safety",
    "discrimination",
    "threat_abuse",
    "legal_financial",
    "pii",
    "spam_competitor",
}


class GapCoverageTest(unittest.TestCase):
    """One assertion-group per reported gap G1..G18."""

    def setUp(self):
        self.client = MockGBPClient()
        self.loc1_reviews = self.client.list_reviews(LOC1)

    def by_id(self, review_id, reviews=None):
        pool = reviews if reviews is not None else self.loc1_reviews
        hits = [r for r in pool if r.get("reviewId") == review_id]
        self.assertTrue(hits, f"review {review_id} missing")
        return hits[0]

    # -- star rating gaps (section 1) --------------------------------------

    def test_g01_three_star_with_comment_present(self):
        review = self.by_id("r-1007")
        self.assertEqual(review["starRating"], "THREE")
        self.assertTrue((review["comment"] or "").strip())

    def test_g02_two_star_with_comment_present(self):
        review = self.by_id("r-1008")
        self.assertEqual(review["starRating"], "TWO")
        self.assertTrue((review["comment"] or "").strip())

    def test_g03_rating_only_low_star_has_null_comment(self):
        review = self.by_id("r-1009")
        self.assertEqual(review["starRating"], "THREE")
        self.assertIsNone(review["comment"])

    # -- sensitive-category gaps (section 1.1): G4-G9 ----------------------

    def test_g04_to_g09_all_six_sensitive_categories_present(self):
        reviews = fixtures.list_sensitive_reviews()
        self.assertEqual(len(reviews), 6)

        categories = {r.get("sensitiveCategory") for r in reviews}
        self.assertEqual(categories, SENSITIVE_CATEGORIES)

        for review in reviews:
            self.assertTrue(review["name"].startswith(f"{LOC1}/reviews/"))
            self.assertTrue(review["reviewId"])
            self.assertTrue((review["comment"] or "").strip())
            self.assertTrue(review["reviewer"]["displayName"])

    def test_g04_health_safety_wording(self):
        review = self.by_id_of_category("health_safety")
        self.assertIn("infection", review["comment"].lower())

    def test_g05_discrimination_wording(self):
        review = self.by_id_of_category("discrimination")
        self.assertIn("racist", review["comment"].lower())

    def test_g06_threat_abuse_wording(self):
        review = self.by_id_of_category("threat_abuse")
        self.assertIn("destroy your shop", review["comment"].lower())

    def test_g07_legal_financial_wording(self):
        review = self.by_id_of_category("legal_financial")
        self.assertIn("sue you", review["comment"].lower())

    def test_g08_pii_wording(self):
        review = self.by_id_of_category("pii")
        self.assertIn("555-123-4567", review["comment"])

    def test_g09_spam_competitor_wording(self):
        review = self.by_id_of_category("spam_competitor")
        self.assertIn("cheap-nails-competitor.example", review["comment"])

    def by_id_of_category(self, category):
        return next(r for r in fixtures.list_sensitive_reviews()
                    if r.get("sensitiveCategory") == category)

    # -- local SEO / shop isolation (section 2, 6): G10-G14 ----------------

    def test_g10_loc002_has_its_own_reviews(self):
        reviews = self.client.list_reviews_for(LOC2, ["reviews_loc002_page1.json"])
        self.assertGreaterEqual(len(reviews), 1)
        self.assertTrue(all(r["name"].startswith(f"{LOC2}/") for r in reviews))

    def test_g10_shop_isolation_no_cross_leak(self):
        loc1 = self.client.list_reviews_for(LOC1, fixtures.review_page_files(LOC1))
        loc2 = self.client.list_reviews_for(LOC2, ["reviews_loc002_page1.json"])

        self.assertTrue(all(r["name"].startswith(f"{LOC1}/") for r in loc1))
        self.assertTrue(all(r["name"].startswith(f"{LOC2}/") for r in loc2))

        # A location with no review fixture returns nothing, not another shop's.
        self.assertEqual(self.client.list_reviews_for(LOC2, ["reviews_empty.json"]), [])
        # Default walk of another location yields no leakage either.
        self.assertEqual(self.client.list_reviews(LOC2), [])

    def test_g11_loc005_vietnamese_review_present(self):
        reviews = self.client.list_reviews_for(LOC5, ["reviews_loc005_vi_page1.json"])
        self.assertEqual(len(reviews), 1)
        comment = reviews[0]["comment"]
        self.assertTrue(reviews[0]["name"].startswith(f"{LOC5}/"))
        self.assertIn("nhân viên", comment)
        self.assertTrue(any(ch in comment for ch in "ấệơđ"), comment)

    def test_g12_g13_g14_brand_profiles_load_with_expected_fields(self):
        profiles = fixtures.load_brand_profiles()
        self.assertEqual(len(profiles), 2)

        by_loc = {p["location_id"]: p for p in profiles}
        self.assertEqual(set(by_loc), {LOC1, LOC5})

        for profile in profiles:
            for field in (
                "business_name", "city", "services_json", "language",
                "tone", "seo_keywords_json", "rules_json", "auto_publish_allowed",
            ):
                self.assertIn(field, profile, f"missing brand-profile field {field}")

        # G13: per-shop auto-publish + language.
        self.assertEqual(by_loc[LOC1]["language"], "en")
        self.assertEqual(by_loc[LOC1]["auto_publish_allowed"], 0)
        self.assertEqual(by_loc[LOC5]["language"], "vi")
        self.assertEqual(by_loc[LOC5]["auto_publish_allowed"], 1)

        # G14: do-not-mention list, naming staff that appear in the reviews.
        self.assertIn("Kim", by_loc[LOC1]["rules_json"]["do_not_mention"])
        # G12: keyword source for the Local SEO checks.
        self.assertTrue(by_loc[LOC1]["seo_keywords_json"])

    # -- state / de-dup gaps (section 5, 3): G15-G18 -----------------------

    def test_g15_review_with_existing_reply_present(self):
        review = self.by_id("r-1003")
        self.assertIn("reviewReply", review)
        self.assertTrue(review["reviewReply"]["comment"])
        self.assertTrue(review["reviewReply"]["updateTime"])

    def test_g16_downgrade_resolves_to_lower_rating_newer_time(self):
        review = self.by_id("r-1011")
        self.assertEqual(review["starRating"], "TWO")
        self.assertEqual(review["updateTime"], "2026-09-20T10:00:00Z")
        self.assertIn("(downgraded", review["comment"])

    def test_g17_empty_page_returns_empty_list_and_does_not_raise(self):
        self.assertEqual(fixtures.list_reviews_empty()["reviews"], [])
        self.assertNotIn("nextPageToken", fixtures.list_reviews_empty())
        # Serving the empty page set raises nothing and returns [].
        self.assertEqual(self.client.list_reviews_for(LOC1, ["reviews_empty.json"]), [])

    def test_g18_same_page_duplicate_de_duplicates_to_one_row_in_repo(self):
        page1 = self.client.list_reviews_for(LOC1, ["reviews_page1.json"])
        r1010 = [r for r in page1 if r["reviewId"] == "r-1010"]
        self.assertEqual(len(r1010), 1, "same-page duplicate collapses to one")
        self.assertIn("(same-page updated)", r1010[0]["comment"])

        repo = Repository(":memory:")
        try:
            repo.init_db(SCHEMA_PATH)
            acct = repo.upsert_account("accounts/1001")
            loc_id = repo.upsert_location(acct, LOC1, title="Demo Nails")
            for review in page1:
                repo.upsert_review(
                    loc_id,
                    review["name"],
                    review_id=review["reviewId"],
                    rating=STAR_TO_INT[review["starRating"]],
                    comment=review["comment"],
                )
            rows = repo.conn.execute(
                "SELECT * FROM reviews WHERE review_name = ?", (r1010[0]["name"],)
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["comment"], r1010[0]["comment"])
        finally:
            repo.close()

    # -- defect checks D1 / D2 / D3 ----------------------------------------

    def test_d1_totalsize_matches_deduplicated_count(self):
        unique = self.client.list_reviews(LOC1)
        self.assertEqual(len(unique), 11)
        for page in fixtures.list_reviews_pages():
            self.assertEqual(page["totalSize"], len(unique))
            self.assertEqual(page["totalSize"], 11)

    def test_d2_same_name_resolves_to_newer_updatetime(self):
        review = self.by_id("r-1001")
        self.assertIn("(updated)", review["comment"])
        self.assertEqual(review["updateTime"], "2026-09-13T09:00:00Z")

    def test_d3_average_rating_consistent_with_deduplicated_set(self):
        unique = self.client.list_reviews(LOC1)
        expected = round(sum(STAR_TO_INT[r["starRating"]] for r in unique) / len(unique), 1)
        for page in fixtures.list_reviews_pages():
            self.assertAlmostEqual(page["averageRating"], expected, places=1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
