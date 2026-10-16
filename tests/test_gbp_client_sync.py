"""Tests for the GBP client, the QPM limiter and the review sync.

Fully offline: every HTTP call is served by ``MockTransport``; no network, no
Google, standard library only.
"""

from __future__ import annotations

import unittest

from gbp.client.gbp_client import GbpClient
from gbp.client.http_client import MockTransport
from gbp.client.quota import QuotaLimiter
from gbp.repo.sqlite_repo import Repository
from gbp.sync.reviews_sync import ReviewSync

BASE = "https://mybusiness.googleapis.com"


class FakeClock:
    """Injectable monotonic clock for the quota limiter."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


def _review(location: str, review_id: str, rating, update_time, reviewer: str = "X"):
    return {
        "name": f"{location}/reviews/{review_id}",
        "rating": rating,
        "comment": f"c-{review_id}",
        "createTime": "2026-01-01T00:00:00Z",
        "updateTime": update_time,
        "reviewer": {"displayName": reviewer},
    }


# --------------------------------------------------------------------------- #
# GbpClient endpoints
# --------------------------------------------------------------------------- #
class GbpClientEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = MockTransport()
        self.client = GbpClient(self.transport, base=BASE)

    def test_list_accounts(self) -> None:
        self.transport.register("GET", f"{BASE}/v1/accounts", {"accounts": [{"name": "accounts/1001"}]})
        result = self.client.list_accounts()
        self.assertEqual(result["accounts"][0]["name"], "accounts/1001")
        call = self.transport.calls[-1]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], f"{BASE}/v1/accounts")
        self.assertEqual(call["params"], {})

    def test_list_locations_walks_pages_with_page_size_100(self) -> None:
        url = f"{BASE}/v1/accounts/1001/locations"
        # Register the param-specific page FIRST: first matching route wins.
        self.transport.register(
            "GET",
            url,
            {"locations": [{"name": "accounts/1001/locations/556"}]},
            params={"pageSize": 100, "pageToken": "tok-loc"},
        )
        self.transport.register(
            "GET",
            url,
            {
                "locations": [
                    {"name": "accounts/1001/locations/555"},
                    {"name": "accounts/1001/locations/556"},
                ],
                "nextPageToken": "tok-loc",
            },
            params={"pageSize": 100},
        )

        locations = self.client.list_locations("accounts/1001")
        self.assertEqual(len(locations), 3)
        self.assertEqual(self.transport.calls[0]["params"], {"pageSize": 100})
        self.assertEqual(
            self.transport.calls[1]["params"], {"pageSize": 100, "pageToken": "tok-loc"}
        )
        self.assertEqual(len(self.transport.calls), 2)

    def test_list_locations_accepts_bare_account_id(self) -> None:
        url = f"{BASE}/v1/accounts/1001/locations"
        self.transport.register("GET", url, {"locations": []}, params={"pageSize": 100})
        self.client.list_locations("1001")
        self.assertEqual(self.transport.calls[-1]["url"], url)

    def test_list_reviews_uses_page_size_50(self) -> None:
        url = f"{BASE}/v4/accounts/1001/locations/555/reviews"
        self.transport.register("GET", url, {"reviews": [{"name": "r1"}]})
        page = self.client.list_reviews("accounts/1001/locations/555")
        self.assertEqual(page["reviews"][0]["name"], "r1")
        call = self.transport.calls[-1]
        self.assertEqual(call["url"], url)
        self.assertEqual(call["params"], {"pageSize": 50})

    def test_list_reviews_with_page_token(self) -> None:
        url = f"{BASE}/v4/accounts/1001/locations/555/reviews"
        self.transport.register(
            "GET", url, {"reviews": []}, params={"pageSize": 50, "pageToken": "tok2"}
        )
        self.transport.register("GET", url, {"reviews": [{"name": "r1"}], "nextPageToken": "tok2"})
        self.client.list_reviews("accounts/1001/locations/555", page_token="tok2")
        self.assertEqual(
            self.transport.calls[-1]["params"], {"pageSize": 50, "pageToken": "tok2"}
        )

    def test_get_review(self) -> None:
        name = "accounts/1001/locations/555/reviews/r1"
        self.transport.register("GET", f"{BASE}/v4/{name}", {"name": name, "rating": 5})
        review = self.client.get_review(name)
        self.assertEqual(review["rating"], 5)
        call = self.transport.calls[-1]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], f"{BASE}/v4/{name}")

    def test_update_reply_uses_put_and_reply_suffix(self) -> None:
        name = "accounts/1001/locations/555/reviews/r1"
        self.transport.register("PUT", f"{BASE}/v4/{name}/reply", {"comment": "Thanks"})
        self.client.update_reply(name, "Thanks")
        call = self.transport.calls[-1]
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(call["url"], f"{BASE}/v4/{name}/reply")
        self.assertEqual(call["data"], {"comment": "Thanks"})

    def test_batch_get_reviews(self) -> None:
        locations = ["accounts/1001/locations/555", "accounts/1001/locations/556"]
        url = f"{BASE}/v4/accounts/1001/locations:batchGetReviews"
        self.transport.register("POST", url, {"reviews": []})
        self.client.batch_get_reviews("accounts/1001", locations)
        call = self.transport.calls[-1]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], url)
        self.assertEqual(call["data"], {"locationNames": locations})

    def test_quota_limiter_is_used_by_every_call(self) -> None:
        clock = FakeClock()
        quota = QuotaLimiter(qpm=5, clock=clock, sleep=lambda s: clock.advance(s))
        client = GbpClient(self.transport, quota=quota, base=BASE)
        self.transport.register("GET", f"{BASE}/v1/accounts", {"accounts": []})
        client.list_accounts()
        self.assertEqual(quota.calls, 1)


# --------------------------------------------------------------------------- #
# QuotaLimiter
# --------------------------------------------------------------------------- #
class QuotaLimiterTests(unittest.TestCase):
    def test_allows_up_to_qpm_then_waits(self) -> None:
        clock = FakeClock()
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            clock.advance(seconds)

        limiter = QuotaLimiter(qpm=2, clock=clock, sleep=sleep)
        limiter.acquire()
        limiter.acquire()
        self.assertEqual(limiter.calls, 2)
        self.assertTrue(limiter.would_wait())

        limiter.acquire()  # must block for ~60s before the window frees a slot
        self.assertEqual(limiter.calls, 3)
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 60.0)
        self.assertFalse(limiter.would_wait())

    def test_would_wait_is_false_when_empty(self) -> None:
        limiter = QuotaLimiter(qpm=1, clock=FakeClock(), sleep=lambda s: None)
        self.assertFalse(limiter.would_wait())


# --------------------------------------------------------------------------- #
# ReviewSync
# --------------------------------------------------------------------------- #
class ReviewSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repository(":memory:")
        self.repo.init_db()
        self.addCleanup(self.repo.close)
        self.transport = MockTransport()
        self.client = GbpClient(self.transport, base=BASE)
        self.sync = ReviewSync(self.repo, self.client)

        self.location = "accounts/1001/locations/555"
        self.page_url = f"{BASE}/v4/{self.location}/reviews"
        self._register_pages()

    def _register_pages(self) -> None:
        # Param-specific page FIRST (first matching route wins).
        self.transport.register(
            "GET",
            self.page_url,
            {"reviews": [_review(self.location, "r3", 3, "2026-01-04T00:00:00Z", "Chi")]},
            params={"pageSize": 50, "pageToken": "tok2"},
        )
        self.transport.register(
            "GET",
            self.page_url,
            {
                "reviews": [
                    _review(self.location, "r1", 5, "2026-01-02T00:00:00Z", "An"),
                    _review(self.location, "r2", 4, "2026-01-03T00:00:00Z", "Binh"),
                ],
                "nextPageToken": "tok2",
            },
        )

    def _count(self, sql: str, args=()) -> int:
        return int(self.repo.conn.execute(sql, args).fetchone()[0])

    def test_sync_location_walks_pages_and_upserts(self) -> None:
        result = self.sync.sync_location(self.location)

        self.assertEqual(result["fetched"], 3)
        self.assertEqual(result["upserted"], 3)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self._count("SELECT COUNT(*) FROM reviews"), 3)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM sync_runs WHERE run_key = ?", (result["run_key"],)), 1
        )

        row = self.repo.conn.execute(
            "SELECT * FROM sync_runs WHERE run_key = ?", (result["run_key"],)
        ).fetchone()
        self.assertEqual(row["fetched"], 3)
        self.assertEqual(row["upserted"], 3)
        self.assertEqual(row["errors"], 0)
        self.assertEqual(row["status"], "ok")
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["ended_at"])

        stored = self.repo.conn.execute(
            "SELECT rating, comment, update_time FROM reviews WHERE review_name = ?",
            (f"{self.location}/reviews/r3",),
        ).fetchone()
        self.assertEqual(stored["rating"], 3)
        self.assertEqual(stored["comment"], "c-r3")
        self.assertEqual(stored["update_time"], "2026-01-04T00:00:00Z")

    def test_sync_twice_is_idempotent(self) -> None:
        self.sync.sync_location(self.location)
        self.assertEqual(self._count("SELECT COUNT(*) FROM sync_runs"), 1)

        self.sync.sync_location(self.location)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reviews"), 3)  # no duplicates
        self.assertEqual(self._count("SELECT COUNT(*) FROM sync_runs"), 1)  # run_key collapses

    def test_older_snapshot_never_overwrites_newer(self) -> None:
        self.sync.sync_location(self.location)
        # A stale page 1 carrying an old r1 must not clobber the stored snapshot.
        self.transport.routes.clear()
        self.transport.register(
            "GET",
            self.page_url,
            {"reviews": [_review(self.location, "r1", 1, "2020-01-01T00:00:00Z", "Old")]},
        )
        self.sync.sync_location(self.location)
        row = self.repo.conn.execute(
            "SELECT rating, update_time FROM reviews WHERE review_name = ?",
            (f"{self.location}/reviews/r1",),
        ).fetchone()
        self.assertEqual(row["rating"], 5)
        self.assertEqual(row["update_time"], "2026-01-02T00:00:00Z")

    def test_empty_page_is_tolerated(self) -> None:
        other = "accounts/1001/locations/556"
        self.transport.register("GET", f"{BASE}/v4/{other}/reviews", {"reviews": []})
        result = self.sync.sync_location(other)
        self.assertEqual(result["fetched"], 0)
        self.assertEqual(result["upserted"], 0)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM sync_runs WHERE run_key = ?", (result["run_key"],)), 1
        )

    def test_incremental_sync_skips_unchanged_reviews(self) -> None:
        self.sync.sync_location(self.location)
        result = self.sync.incremental_sync(
            self.location, since_update_time="2026-01-03T00:00:00Z"
        )
        self.assertEqual(result["fetched"], 3)
        self.assertEqual(result["skipped"], 2)  # r1 (01-02) and r2 (01-03) are not newer
        self.assertEqual(result["upserted"], 1)  # only r3 (01-04)
        self.assertEqual(result["errors"], 0)

    def test_incremental_sync_picks_up_a_newer_review(self) -> None:
        self.sync.sync_location(self.location)
        # r1 is edited: newer updateTime, new rating.
        self.transport.routes.clear()
        self.transport.register(
            "GET",
            self.page_url,
            {"reviews": [_review(self.location, "r1", 1, "2026-02-01T00:00:00Z", "An")]},
        )
        result = self.sync.incremental_sync(
            self.location, since_update_time="2026-01-31T00:00:00Z"
        )
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reviews"), 3)  # no new rows
        row = self.repo.conn.execute(
            "SELECT rating, update_time FROM reviews WHERE review_name = ?",
            (f"{self.location}/reviews/r1",),
        ).fetchone()
        self.assertEqual(row["rating"], 1)
        self.assertEqual(row["update_time"], "2026-02-01T00:00:00Z")

    def test_malformed_review_counts_error_and_does_not_abort(self) -> None:
        other = "accounts/1001/locations/557"
        self.transport.register(
            "GET",
            f"{BASE}/v4/{other}/reviews",
            {
                "reviews": [
                    _review(other, "r1", 5, "2026-01-02T00:00:00Z"),
                    {"rating": "five"},  # malformed: no "name"
                    _review(other, "r2", 2, "2026-01-02T00:00:00Z"),
                ]
            },
        )
        result = self.sync.sync_location(other)
        self.assertEqual(result["fetched"], 3)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["upserted"], 2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM reviews WHERE review_name LIKE ?", (f"{other}/%",)), 2
        )
        row = self.repo.conn.execute("SELECT status, errors FROM sync_runs WHERE run_key = ?", (result["run_key"],)).fetchone()
        self.assertEqual(row["errors"], 1)
        self.assertEqual(row["status"], "partial")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
