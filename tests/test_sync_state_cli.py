"""Offline tests for Group 5A: sync state, CLI, batch sync and retry hardening.

No network, no Google, standard library only. Run with::

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from gbp.cli import main as cli_main
from gbp.client.gbp_client import GbpClient
from gbp.client.http_client import (
    ApiError,
    ApiTimeout,
    AuthError,
    HttpClient,
    MockTransport,
    Response,
)
from gbp.repo.sqlite_repo import Repository
from gbp.sync.reviews_sync import ReviewSync
from gbp.sync.state import SyncState

BASE = "https://mybusiness.googleapis.com"
LOC1 = "accounts/1001/locations/loc-001"
LOC2 = "accounts/1001/locations/loc-002"


def _review(location: str, review_id: str, rating, update_time, reviewer: str = "X"):
    return {
        "name": f"{location}/reviews/{review_id}",
        "rating": rating,
        "comment": f"c-{review_id}",
        "createTime": "2026-01-01T00:00:00Z",
        "updateTime": update_time,
        "reviewer": {"displayName": reviewer},
    }


class RecordingSleep:
    def __init__(self) -> None:
        self.delays = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class FlakyTransport:
    """Returns queued responses/raises queued exceptions, in order."""

    def __init__(self, items):
        self.items = list(items)
        self.calls = 0

    def _next(self):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, url, params=None, headers=None, timeout=15.0):
        return self._next()

    def post(self, url, data=None, headers=None, timeout=15.0):
        return self._next()


class BaseSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repository(":memory:")
        self.repo.init_db()
        self.addCleanup(self.repo.close)
        self.transport = MockTransport()
        self.client = GbpClient(self.transport, base=BASE)
        self.sync = ReviewSync(self.repo, self.client)
        self.page_url = f"{BASE}/v4/{LOC1}/reviews"

    def _register_pages(self, r1_time="2026-01-02T00:00:00Z") -> None:
        self.transport.register(
            "GET",
            self.page_url,
            {"reviews": [_review(LOC1, "r3", 3, "2026-01-04T00:00:00Z", "Chi")]},
            params={"pageSize": 50, "pageToken": "tok2"},
        )
        self.transport.register(
            "GET",
            self.page_url,
            {
                "reviews": [
                    _review(LOC1, "r1", 5, r1_time, "An"),
                    _review(LOC1, "r2", 4, "2026-01-03T00:00:00Z", "Binh"),
                ],
                "nextPageToken": "tok2",
            },
        )

    def _count(self, sql, args=()):
        return int(self.repo.conn.execute(sql, args).fetchone()[0])


# --------------------------------------------------------------------------- #
# SyncState
# --------------------------------------------------------------------------- #
class SyncStateTests(BaseSyncTest):
    def test_watermark_round_trip(self) -> None:
        state = SyncState(self.repo)
        self.assertIsNone(state.get_watermark(LOC1))
        state.set_watermark(LOC1, "2026-01-04T00:00:00Z")
        self.assertEqual(state.get_watermark(LOC1), "2026-01-04T00:00:00Z")

    def test_checkpoint_set_get_clear(self) -> None:
        state = SyncState(self.repo)
        self.assertIsNone(state.get_checkpoint(LOC1))
        state.set_checkpoint(LOC1, "tok2")
        self.assertEqual(state.get_checkpoint(LOC1), "tok2")
        state.clear_checkpoint(LOC1)
        self.assertIsNone(state.get_checkpoint(LOC1))

    def test_record_run_and_consecutive_failures(self) -> None:
        state = SyncState(self.repo)
        state.record_run("k1", LOC1, "failed")
        state.record_run("k2", LOC1, "failed")
        self.assertEqual(state.consecutive_failures(LOC1), 2)
        self.assertFalse(state.should_alert(LOC1))
        state.record_run("k3", LOC1, "failed")
        self.assertEqual(state.consecutive_failures(LOC1), 3)
        self.assertTrue(state.should_alert(LOC1))

    def test_a_success_resets_the_streak(self) -> None:
        state = SyncState(self.repo)
        state.record_run("k1", LOC1, "failed")
        state.record_run("k2", LOC1, "failed")
        state.record_run("k3", LOC1, "ok")
        state.record_run("k4", LOC1, "failed")
        self.assertEqual(state.consecutive_failures(LOC1), 1)
        self.assertFalse(state.should_alert(LOC1))


# --------------------------------------------------------------------------- #
# watermark / incremental behaviour
# --------------------------------------------------------------------------- #
class WatermarkIncrementalTests(BaseSyncTest):
    def test_watermark_persists_and_unchanged_reviews_are_skipped(self) -> None:
        self._register_pages()
        first = self.sync.incremental_sync(LOC1)
        self.assertEqual(first["fetched"], 3)
        self.assertEqual(first["upserted"], 3)
        self.assertEqual(first["skipped"], 0)
        self.assertEqual(self.sync.state.get_watermark(LOC1), "2026-01-04T00:00:00Z")
        self.assertEqual(self._count("SELECT COUNT(*) FROM reviews"), 3)

        # A brand-new ReviewSync over the SAME repo must reuse the watermark.
        fresh = ReviewSync(self.repo, self.client)
        second = fresh.incremental_sync(LOC1)
        self.assertEqual(second["skipped"], 3)
        self.assertEqual(second["upserted"], 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reviews"), 3)

    def test_second_identical_sync_writes_nothing(self) -> None:
        self._register_pages()
        self.sync.incremental_sync(LOC1)
        before = self._count("SELECT COUNT(*) FROM reviews")
        again = self.sync.incremental_sync(LOC1)
        self.assertEqual(again["upserted"], 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reviews"), before)

    def test_newer_update_time_is_re_upserted(self) -> None:
        self._register_pages()
        self.sync.incremental_sync(LOC1)
        # r1 is edited: strictly newer updateTime.
        self.transport.routes.clear()
        self.transport.register(
            "GET",
            self.page_url,
            {"reviews": [_review(LOC1, "r1", 1, "2026-02-01T00:00:00Z", "An")]},
        )
        result = self.sync.incremental_sync(LOC1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reviews"), 3)  # no new row
        row = self.repo.conn.execute(
            "SELECT rating, update_time FROM reviews WHERE review_name = ?",
            (f"{LOC1}/reviews/r1",),
        ).fetchone()
        self.assertEqual(row["rating"], 1)
        self.assertEqual(row["update_time"], "2026-02-01T00:00:00Z")

    def test_checkpoint_resume_continues_from_stored_token(self) -> None:
        self.transport.register(
            "GET",
            self.page_url,
            {"reviews": [_review(LOC1, "r3", 3, "2026-01-05T00:00:00Z")]},
            params={"pageSize": 50, "pageToken": "tok2"},
        )
        self.sync.state.set_checkpoint(LOC1, "tok2")

        result = self.sync.incremental_sync(LOC1)
        self.assertEqual(result["resumed_from"], "tok2")
        self.assertEqual(result["fetched"], 1)  # only the resumed page
        call = self.transport.calls[0]
        self.assertEqual(call["params"].get("pageToken"), "tok2")
        self.assertIsNone(self.sync.state.get_checkpoint(LOC1))  # cleared after the walk)


# --------------------------------------------------------------------------- #
# sync_all: resilience + batch
# --------------------------------------------------------------------------- #
class SyncAllTests(BaseSyncTest):
    def test_failing_location_is_recorded_and_does_not_abort(self) -> None:
        # LOG1 route registered; LOC2 has no route -> 404 -> failure.
        self._register_pages()
        results = self.sync.sync_all([LOC1, LOC2])
        self.assertEqual(results[LOC1]["status"], "ok")
        self.assertEqual(results[LOC2]["status"], "failed")
        self.assertEqual(self.sync.state.consecutive_failures(LOC2), 1)

    def test_should_alert_after_three_consecutive_failures(self) -> None:
        self._register_pages()
        for _ in range(3):
            self.sync.sync_all([LOC1, LOC2])
        self.assertEqual(self.sync.state.consecutive_failures(LOC2), 3)
        self.assertTrue(self.sync.state.should_alert(LOC2))
        self.assertFalse(self.sync.state.should_alert(LOC1))

    def test_batch_mode_calls_batch_and_upserts_once(self) -> None:
        url = f"{BASE}/v4/accounts/1001/locations:batchGetReviews"
        payload = {
            "reviews": [
                _review(LOC1, "b1", 5, "2026-01-02T00:00:00Z"),
                _review(LOC2, "b2", 4, "2026-01-03T00:00:00Z"),
            ]
        }
        self.transport.register("POST", url, payload)

        results = self.sync.sync_all([LOC1, LOC2], batch=True)
        self.assertEqual(results[LOC1]["status"], "ok")
        self.assertEqual(results[LOC2]["status"], "ok")
        # the batch endpoint was called exactly once
        batch_calls = [c for c in self.transport.calls if c["method"] == "POST"]
        self.assertEqual(len(batch_calls), 1)
        self.assertEqual(batch_calls[0]["url"], url)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reviews"), 2)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reviews WHERE review_name LIKE ?", (f"{LOC1}/%",)), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reviews WHERE review_name LIKE ?", (f"{LOC2}/%",)), 1)

        # Idempotent: a second batch run never duplicates a row.
        self.sync.sync_all([LOC1, LOC2], batch=True)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reviews"), 2)


# --------------------------------------------------------------------------- #
# HttpClient hardening
# --------------------------------------------------------------------------- #
class HttpClientHardeningTests(unittest.TestCase):
    URL = "https://api.example.com/thing"

    def test_retries_429_then_succeeds(self) -> None:
        transport = MockTransport()
        transport.register("GET", self.URL, {"ok": True})
        transport.fail_429(self.URL, times=1)
        sleep = RecordingSleep()
        client = HttpClient(transport, max_attempts=3, base_delay=0.01, sleep=sleep, jitter=lambda: 1.0)
        self.assertEqual(client.request("GET", self.URL), {"ok": True})
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(sleep.delays), 1)

    def test_retries_5xx_then_succeeds(self) -> None:
        transport = FlakyTransport(
            [Response(503, {"error": {"message": "boom"}}), Response(200, {"ok": True})]
        )
        sleep = RecordingSleep()
        client = HttpClient(transport, max_attempts=3, base_delay=0.01, sleep=sleep, jitter=lambda: 0.0)
        self.assertEqual(client.request("GET", self.URL), {"ok": True})
        self.assertEqual(transport.calls, 2)
        self.assertEqual(len(sleep.delays), 1)

    def test_does_not_retry_401(self) -> None:
        transport = MockTransport()
        transport.register("GET", self.URL, {"ok": True})
        transport.always_401(self.URL)
        sleep = RecordingSleep()
        client = HttpClient(transport, max_attempts=3, sleep=sleep)
        with self.assertRaises(AuthError):
            client.request("GET", self.URL)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleep.delays, [])

    def test_timeout_raises_api_timeout_after_retries(self) -> None:
        transport = FlakyTransport([TimeoutError("slow")] * 3)
        sleep = RecordingSleep()
        client = HttpClient(transport, max_attempts=3, base_delay=0.01, sleep=sleep, jitter=lambda: 0.0)
        with self.assertRaises(ApiTimeout):
            client.request("GET", self.URL)
        self.assertEqual(transport.calls, 3)
        self.assertEqual(len(sleep.delays), 2)

    def test_timeout_then_success_retries(self) -> None:
        transport = FlakyTransport([TimeoutError("slow"), Response(200, {"ok": True})])
        sleep = RecordingSleep()
        client = HttpClient(transport, max_attempts=3, base_delay=0.01, sleep=sleep, jitter=lambda: 0.0)
        self.assertEqual(client.request("GET", self.URL), {"ok": True})
        self.assertEqual(transport.calls, 2)

    def test_timeout_default_and_forwarded(self) -> None:
        transport = MockTransport()
        transport.register("GET", self.URL, {"ok": True})
        client = HttpClient(transport, sleep=RecordingSleep(), timeout=9.5)
        client.request("GET", self.URL)
        self.assertEqual(transport.calls[0]["timeout"], 9.5)


class ModelClientRetryTests(unittest.TestCase):
    def test_openrouter_retries_429_then_succeeds(self) -> None:
        from gbp.reply.model_client import ApiError as ModelApiError, OpenRouterModelClient

        calls = {"n": 0}

        def transport(payload, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ModelApiError("HTTP 429", code=429)
            return {"choices": [{"message": {"content": json.dumps({"reply": "hi"})}}]}

        client = OpenRouterModelClient(api_key="k", transport=transport, sleep=lambda s: None)
        self.assertEqual(client.complete("s", "u"), json.dumps({"reply": "hi"}))
        self.assertEqual(calls["n"], 2)

    def test_openrouter_never_retries_401(self) -> None:
        from gbp.reply.model_client import ApiError as ModelApiError, ModelError, OpenRouterModelClient

        calls = {"n": 0}

        def transport(payload, timeout):
            calls["n"] += 1
            raise ModelApiError("HTTP 401", code=401)

        client = OpenRouterModelClient(api_key="k", transport=transport, sleep=lambda s: None)
        with self.assertRaises(ModelError):
            client.complete("s", "u")
        self.assertEqual(calls["n"], 1)

    def test_openrouter_bounded_retries_on_5xx(self) -> None:
        from gbp.reply.model_client import ApiError as ModelApiError, ModelError, OpenRouterModelClient

        calls = {"n": 0}

        def transport(payload, timeout):
            calls["n"] += 1
            raise ModelApiError("HTTP 500", code=500)

        client = OpenRouterModelClient(api_key="k", transport=transport, sleep=lambda s: None, max_attempts=3)
        with self.assertRaises(ModelError):
            client.complete("s", "u")
        self.assertEqual(calls["n"], 3)  # 1 + 2 retries


# --------------------------------------------------------------------------- #
# CLI (mock mode only)
# --------------------------------------------------------------------------- #
class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "cli.db")

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_main(argv)
        return code, out.getvalue()

    def test_list_locations_prints_n_locations(self) -> None:
        code, out = self._run(["list-locations"])
        self.assertEqual(code, 0)
        self.assertIn("6 locations", out)
        self.assertIn("loc-001", out)

    def test_sync_reviews_writes_rows_and_run_records(self) -> None:
        code, out = self._run(["sync-reviews", "--limit", "2", "--db", self.db])
        self.assertEqual(code, 0)
        repo = Repository(self.db)
        self.addCleanup(repo.close)
        reviews = repo.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        runs = repo.conn.execute("SELECT COUNT(*) FROM sync_run_log").fetchone()[0]
        self.assertGreater(reviews, 0)
        self.assertGreater(runs, 0)

    def test_draft_reply_uses_mock_model_without_network(self) -> None:
        code, out = self._run(["draft-reply", "--db", self.db])
        self.assertEqual(code, 0)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["model"], "MockModelClient")
        self.assertTrue(payload["draft"])

    def test_live_refuses_without_credentials(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        env["GBP_TOKEN_PATH"] = os.path.join(self.tmp.name, "missing-token.json")
        with mock.patch.dict(os.environ, env, clear=True):
            code, _ = self._run(["list-locations", "--live"])
        self.assertEqual(code, 2)

    def test_publish_command_is_hard_refused(self) -> None:
        code, _ = self._run(["publish", "--location", LOC1])
        self.assertEqual(code, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
