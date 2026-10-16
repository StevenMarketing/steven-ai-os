"""Tests for the offline fixtures, MockGBPClient pagination and backoff.

Run from the project root:

    cd /opt/data/gbp-integration
    PYTHONPATH=src python3 -m unittest discover -s tests -v

Standard library only; no network access.
"""

from __future__ import annotations

import unittest
from unittest import mock

from gbp import fixtures
from gbp.client import (
    AuthError,
    GBPError,
    MockGBPClient,
    RateLimitedError,
    with_backoff,
)

ACCOUNT = "accounts/1001"
LOCATION = "accounts/1001/locations/loc-001"

#: Patch target for the sleep used by the retry helper.
SLEEP_TARGET = "gbp.client.mock_client.time.sleep"


class FixturesTests(unittest.TestCase):
    def test_accounts_fixture(self):
        accounts = fixtures.list_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["name"], "accounts/1001")
        self.assertEqual(accounts[0]["accountName"], "Demo Nail Group")

    def test_locations_fixture_has_en_and_vi(self):
        locations = fixtures.list_locations()
        self.assertGreaterEqual(len(locations), 5)

        languages = {str(loc.get("language", "")).lower() for loc in locations}
        self.assertIn("en", languages)
        self.assertIn("vi", languages)

        for loc in locations:
            self.assertTrue(loc["name"].startswith(f"{ACCOUNT}/locations/"))
            self.assertTrue(loc["title"])
            self.assertTrue(loc["city"])
            self.assertTrue(loc["state"])

        cities = {loc["city"] for loc in locations}
        self.assertEqual(len(cities), len(locations), "cities must be distinct")

    def test_review_pages_fixture_shape(self):
        pages = fixtures.list_reviews_pages()
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(pages[0]["reviews"]), 9)
        self.assertTrue(pages[0]["nextPageToken"])
        self.assertNotIn("nextPageToken", pages[1])


class MockClientTestBase(unittest.TestCase):
    def make_client(self, **kwargs) -> MockGBPClient:
        return MockGBPClient(**kwargs)

    def review_ids(self, reviews):
        return {r["reviewId"] for r in reviews}


class PaginationTests(MockClientTestBase):
    def test_list_reviews_returns_reviews_from_both_pages(self):
        client = self.make_client()
        reviews = client.list_reviews(LOCATION)
        ids = self.review_ids(reviews)

        # r-1001..r-1003 (+ r-1007..r-1011) live on page 1, r-1004..r-1006 on page 2.
        page1_ids = {"r-1001", "r-1002", "r-1003", "r-1007", "r-1008", "r-1009", "r-1010", "r-1011"}
        page2_ids = {"r-1004", "r-1005", "r-1006"}
        self.assertTrue(page1_ids.issubset(ids))
        self.assertTrue(page2_ids.issubset(ids))
        self.assertEqual(len(reviews), 11, "11 unique reviews across both pages")

    def test_fetch_page_proves_next_page_token_walk(self):
        client = self.make_client()

        page1 = client.fetch_page(LOCATION)
        self.assertEqual(len(page1["reviews"]), 9)
        self.assertEqual(page1["nextPageToken"], "reviews-page-2")

        page2 = client.fetch_page(LOCATION, page1["nextPageToken"])
        self.assertEqual(len(page2["reviews"]), 5)
        self.assertNotIn("nextPageToken", page2)

        # Walk the pages manually and compare against list_reviews().
        collected, token, hops = [], None, 0
        while True:
            page = client.fetch_page(LOCATION, token)
            collected.extend(r["name"] for r in page["reviews"])
            hops += 1
            token = page.get("nextPageToken")
            if not token:
                break

        self.assertEqual(hops, 2, "walk must take exactly two hops")
        self.assertEqual(len(collected), 14, "page1 (9) + page2 (5) raw entries")

        deduped = client.list_reviews(LOCATION)
        self.assertEqual([r["name"] for r in deduped], list(dict.fromkeys(collected)))

        page_calls = [c for c in client.calls if c["method"] == "fetch_page"]
        tokens = [c["page_token"] for c in page_calls]
        self.assertIn(None, tokens)
        self.assertIn("reviews-page-2", tokens)

    def test_unknown_page_token_raises(self):
        client = self.make_client()
        with self.assertRaises(ValueError):
            client.fetch_page(LOCATION, "does-not-exist")


class DedupTests(MockClientTestBase):
    def test_duplicate_review_name_appears_once(self):
        client = self.make_client()
        reviews = client.list_reviews(LOCATION)

        names = [r["name"] for r in reviews]
        duplicated = f"{LOCATION}/reviews/r-1001"
        self.assertEqual(names.count(duplicated), 1, "duplicate must collapse to one")
        self.assertEqual(len(names), len(set(names)))

        # Newer updateTime wins (D2): page 2's edited version is kept.
        kept = next(r for r in reviews if r["name"] == duplicated)
        self.assertIn("(updated)", kept["comment"])
        self.assertEqual(kept["updateTime"], "2026-09-13T09:00:00Z")

    def test_duplicate_exists_in_raw_pages(self):
        pages = fixtures.list_reviews_pages()
        raw_names = [r["name"] for page in pages for r in page["reviews"]]
        self.assertEqual(len(raw_names), 14)
        self.assertEqual(raw_names.count(f"{LOCATION}/reviews/r-1001"), 2)


class ReviewContentTests(MockClientTestBase):
    def setUp(self):
        self.reviews = self.make_client().list_reviews(LOCATION)

    def by_id(self, review_id):
        return next(r for r in self.reviews if r["reviewId"] == review_id)

    def test_rating_only_review_present_with_empty_comment(self):
        review = self.by_id("r-1002")
        self.assertEqual(review["starRating"], "FIVE")
        self.assertFalse(review["comment"], "rating-only review has no comment")
        self.assertIsNone(review["comment"])

    def test_five_star_review_with_text_comment(self):
        review = self.by_id("r-1001")
        self.assertEqual(review["starRating"], "FIVE")
        self.assertTrue(review["comment"].strip())

    def test_four_star_review_present(self):
        self.assertEqual(self.by_id("r-1003")["starRating"], "FOUR")

    def test_one_star_negative_review_present(self):
        review = self.by_id("r-1004")
        self.assertEqual(review["starRating"], "ONE")
        self.assertIn("Terrible", review["comment"])

    def test_review_mentions_service_and_staff(self):
        joined = " ".join(r["comment"] or "" for r in self.reviews)
        self.assertIn("gel manicure", joined.lower())
        self.assertTrue(
            "Kim" in joined or "Anna" in joined, "a staff member must be named"
        )

    def test_vietnamese_review_present(self):
        review = self.by_id("r-1005")
        comment = review["comment"]
        self.assertTrue(any(ch in comment for ch in "ấệơđ"), f"expected Vietnamese text: {comment!r}")
        self.assertIn("nhân viên", comment)

    def test_every_review_has_required_fields(self):
        for review in self.reviews:
            self.assertTrue(review["name"].startswith(f"{LOCATION}/reviews/"))
            self.assertTrue(review["reviewId"])
            self.assertIn(
                review["starRating"], {"ONE", "TWO", "THREE", "FOUR", "FIVE"}
            )
            self.assertTrue(review["createTime"])
            self.assertTrue(review["updateTime"])
            self.assertTrue(review["reviewer"]["displayName"])


class LocationClientTests(MockClientTestBase):
    def test_list_locations_returns_five_plus_with_en_and_vi(self):
        locations = self.make_client().list_locations(ACCOUNT)
        self.assertGreaterEqual(len(locations), 5)

        languages = {str(loc.get("language", "")).lower() for loc in locations}
        self.assertIn("en", languages)
        self.assertIn("vi", languages)
        for loc in locations:
            self.assertTrue(loc["name"].startswith(f"{ACCOUNT}/locations/"))

    def test_list_locations_accepts_account_dict_and_default(self):
        client = self.make_client()
        by_dict = client.list_locations({"name": ACCOUNT})
        by_default = client.list_locations()
        self.assertEqual(len(by_dict), 6)
        self.assertEqual(len(by_default), 6)


class ErrorHandlingTests(MockClientTestBase):
    def test_rate_limited_error_raised_for_429(self):
        client = self.make_client(error="error_429")
        with self.assertRaises(RateLimitedError) as ctx:
            client.list_locations(ACCOUNT)
        self.assertEqual(ctx.exception.code, 429)
        self.assertIn("Quota exceeded", ctx.exception.message)
        self.assertEqual(ctx.exception.payload["error"]["status"], "RESOURCE_EXHAUSTED")
        self.assertIsInstance(ctx.exception, GBPError)

    def test_auth_error_raised_for_401(self):
        client = self.make_client(error="error_401")
        with self.assertRaises(AuthError) as ctx:
            client.list_locations(ACCOUNT)
        self.assertEqual(ctx.exception.code, 401)
        self.assertIn("invalid authentication credentials", ctx.exception.message)
        self.assertEqual(ctx.exception.payload["error"]["status"], "UNAUTHENTICATED")

    def test_rate_limited_error_raised_from_pagination_walk(self):
        client = self.make_client(error="error_429")
        with self.assertRaises(RateLimitedError):
            client.list_reviews(LOCATION)

    def test_error_on_limits_which_method_fails(self):
        client = self.make_client(error="error_429", error_on={"fetch_page"})
        self.assertEqual(len(client.list_locations(ACCOUNT)), 6)
        with self.assertRaises(RateLimitedError):
            client.fetch_page(LOCATION)

    def test_with_backoff_succeeds_after_transient_failure(self):
        client = self.make_client(error="error_429", fail_times=1)

        with mock.patch(SLEEP_TARGET) as sleep:
            locations = with_backoff(
                lambda: client.list_locations(ACCOUNT), attempts=3, base_delay=0.5
            )

        self.assertEqual(len(locations), 6)
        self.assertEqual(client._failures, 1, "one injected failure then success")
        self.assertEqual(sleep.call_count, 1, "slept once between attempts")
        sleep.assert_called_once_with(0.5)

    def test_with_backoff_retries_then_raises_when_error_persists(self):
        client = self.make_client(error="error_429")

        with mock.patch(SLEEP_TARGET) as sleep:
            with self.assertRaises(RateLimitedError):
                with_backoff(lambda: client.fetch_page(LOCATION), attempts=3)

        self.assertEqual(sleep.call_count, 2, "sleeps between attempt 1-2 and 2-3")
        self.assertEqual(client._failures, 3, "all three attempts were made")

    def test_with_backoff_does_not_retry_auth_error(self):
        client = self.make_client(error="error_401")

        with mock.patch(SLEEP_TARGET) as sleep:
            with self.assertRaises(AuthError):
                with_backoff(lambda: client.list_locations(ACCOUNT), attempts=3)

        self.assertEqual(sleep.call_count, 0, "AuthError must not be retried")
        self.assertEqual(client._failures, 1, "exactly one attempt was made")

    def test_with_backoff_zero_delay_recovers_without_waiting(self):
        client = self.make_client(error="error_429", fail_times=2)
        result = with_backoff(lambda: client.fetch_page(LOCATION), attempts=3)
        self.assertEqual(len(result["reviews"]), 9)
        self.assertEqual(client._failures, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
