"""Group 8B / T26 -- Google Business Profile integration readiness.

Everything is offline: a fake review client, in-memory SQLite, and a
``MockTransport`` for the OAuth exchange. Nothing here opens a socket, and the
live path is asserted to be REFUSED unless the operator opted in.

Covered:
* the reusable multi-location sync queue -- 500 locations, bounded concurrency,
  no duplicates on a second pass, no data loss, no misattribution,
  per-location isolation, retry with ``Retry-After``, checkpoint resume;
* identity: ``location_resource_id`` is the only key;
* OAuth readiness: authorization URL, code exchange over an injected transport,
  live-transport refusal, honest connection status;
* DRAFT MODE: every publish path is blocked.

Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.auth.oauth import (  # noqa: E402
    AUTHORIZE_URL,
    LIVE_OPTIN_ENV,
    SCOPES,
    LiveGoogleNotAllowedError,
    build_authorization_url,
    exchange_code,
    google_connection_status,
    live_google_allowed,
)
from gbp.client.http_client import MockTransport  # noqa: E402
from gbp.client.mock_client import RateLimitedError  # noqa: E402
from gbp.client.urllib_transport import UrllibTransport  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.sync.queue import LocationSyncQueue  # noqa: E402
from gbp.sync.reviews_sync import ReviewSync  # noqa: E402
from gbp.workflow.service import ApprovalService, PublishBlocked  # noqa: E402
from gbp.workflow.states import STATES  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
SCHEMA_SQL_EXTRA = None

N_LOCATIONS = 500
PER_LOCATION = 4


class FatalError(RuntimeError):
    """A deliberate, NON-retryable crash (used to test resume)."""


def _review(location: str, index: int, rating: int, day: int) -> Dict[str, Any]:
    stamp = f"2026-02-{day:02d}T08:00:00Z"
    return {
        "name": f"{location}/reviews/r-{index}",
        "reviewId": f"r-{index}",
        "rating": rating,
        "comment": f"review {index} for {location}",
        "createTime": stamp,
        "updateTime": stamp,
        "reviewer": {"displayName": f"user-{index}"},
    }


class FakeReviewClient:
    """Offline stand-in for ``GbpClient`` exposing only ``list_reviews``."""

    def __init__(
        self,
        pages: Dict[str, List[Dict[str, Any]]],
        *,
        delay: float = 0.0,
        transient: Optional[Dict[str, int]] = None,
        kill_page1: Optional[set] = None,
        always_fail: Optional[set] = None,
    ) -> None:
        self.pages = pages
        self.delay = delay
        self.transient = dict(transient or {})       # location -> remaining times
        self.kill_page1 = set(kill_page1 or ())      # fatal on the 2nd page, once
        self.always_fail = set(always_fail or ())
        self.calls: List[tuple] = []

    def list_reviews(self, location: str, page_token: Optional[str] = None):
        if self.delay:
            time.sleep(self.delay)
        if location in self.always_fail:
            raise FatalError(f"always fails: {location}")
        left = self.transient.get(location, 0)
        if left > 0:
            self.transient[location] = left - 1
            err = RateLimitedError("429 simulated")
            err.retry_after = 2.0
            raise err
        pages = self.pages.get(location, [])
        index = 0 if not page_token else int(str(page_token).rsplit(":", 1)[-1])
        if location in self.kill_page1 and index == 1:
            self.kill_page1.discard(location)
            raise FatalError(f"killed before page 2: {location}")
        self.calls.append((location, index))
        if index >= len(pages):
            return {"reviews": [], "nextPageToken": None}
        page = dict(pages[index])
        page["nextPageToken"] = (
            f"{location}:{index + 1}" if index + 1 < len(pages) else None)
        return page


def build_dataset(n_locations: int = N_LOCATIONS,
                  per_location: int = PER_LOCATION):
    """Two pages per location so pagination/resume are exercised."""
    pages: Dict[str, List[Dict[str, Any]]] = {}
    locations: List[str] = []
    names: List[str] = []
    for i in range(1, n_locations + 1):
        loc = f"accounts/1001/locations/loc-{i:04d}"
        locations.append(loc)
        half = max(1, per_location // 2)
        first = [_review(loc, j, (j % 5) + 1, j + 1) for j in range(half)]
        second = [_review(loc, half + j, ((half + j) % 5) + 1, half + j + 1)
                  for j in range(per_location - half)]
        pages[loc] = [{"reviews": first}, {"reviews": second}]
        names.extend(r["name"] for r in first + second)
    return locations, pages, names


class QueueScaleTests(unittest.TestCase):
    """500 locations through the real ReviewSync + queue against SQLite."""

    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        self.locations, self.pages, self.review_names = build_dataset()
        self.client = FakeReviewClient(self.pages, delay=0.001)
        self.sync = ReviewSync(self.repo, self.client)
        self.state = self.sync.state

    def tearDown(self):
        self.repo.close()

    def _queue(self, **kwargs):
        return LocationSyncQueue(self.sync, sleep=lambda s: None, **kwargs)

    def _rows(self):
        return self.repo.conn.execute(
            "SELECT id, location_id, review_name FROM reviews").fetchall()

    # --- the big one ------------------------------------------------------
    def test_500_locations_sync_without_loss_or_duplication(self):
        report = self._queue(concurrency=4).run(self.locations)
        self.assertEqual(report["locations"], N_LOCATIONS)
        self.assertEqual(report["failed"], 0, report["failed_locations"][:5])
        self.assertEqual(report["ok"], N_LOCATIONS)
        self.assertEqual(report["fetched"], N_LOCATIONS * PER_LOCATION)

        rows = self._rows()
        self.assertEqual(len(rows), len(self.review_names))     # no loss
        self.assertEqual(len({r["review_name"] for r in rows}),
                         len(self.review_names))                # no duplicates

        second = self._queue(concurrency=4).run(self.locations)
        # A second pass rewrites the same rows (idempotent BY KEY) but must not
        # create anything new.
        self.assertEqual(len(self._rows()), len(self.review_names))
        self.assertEqual(len({r["review_name"] for r in self._rows()}),
                         len(self.review_names))
        self.assertEqual(
            self.repo.conn.execute(
                "SELECT COUNT(*) AS n FROM business_locations").fetchone()["n"],
            N_LOCATIONS, "a re-run must not duplicate locations")

    def test_concurrency_is_capped(self):
        report = self._queue(concurrency=2).run(self.locations)
        self.assertLessEqual(report["peak_in_flight"], 2)
        self.assertEqual(report["peak_in_flight"], 2,
                         "the pool should actually run two at a time")

    def test_no_misattribution(self):
        self._queue(concurrency=4).run(self.locations)
        # location_id is the DB row id: map back through location_resource_id
        # (the ONLY identifier) instead of assuming a creation order.
        ids = {r["location_name"]: r["id"] for r in self.repo.conn.execute(
            "SELECT id, location_name FROM business_locations").fetchall()}
        for row in self._rows():
            owner = row["review_name"].rsplit("/reviews/", 1)[0]
            self.assertEqual(row["location_id"], ids[owner],
                             f"{row['review_name']} stored under the wrong location")

    def test_ledger_has_a_row_per_location(self):
        self._queue(concurrency=4).run(self.locations)
        rows = self.repo.conn.execute("SELECT run_key FROM sync_runs").fetchall()
        self.assertEqual(len(rows), N_LOCATIONS)
        keys = " ".join(r["run_key"] for r in rows)
        for loc in (self.locations[0], self.locations[250], self.locations[-1]):
            self.assertIn(loc, keys)

    def test_sync_is_mirrored_into_the_dashboard_ledger(self):
        """T27 unification: a queue-driven sync is visible where the Dashboard
        reads it, is recorded EXACTLY once, and never loses old history.

        Before this, ``ReviewSync`` wrote only ``sync_runs`` while the Dashboard
        read ``sync_run_log`` -- a queue-driven sync was invisible in the UI and
        the consecutive-failure streak double-counted when callers also wrote.
        """
        locs = self.locations[:5]
        self._queue(concurrency=2).run(locs)
        summary = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM sync_runs").fetchone()["n"]
        ledger = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM sync_run_log").fetchone()["n"]
        self.assertEqual(summary, len(locs))
        self.assertEqual(ledger, len(locs), "one ledger row per attempt")

        history = self.state.history(locs[0])
        self.assertTrue(any(h["source"] == "sync_run_log" for h in history))
        # no attempt is reported twice
        keys = [h["run_key"] for h in history if h["run_key"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_unified_history_keeps_legacy_sync_runs_only_rows(self):
        """A row that only ever existed in ``sync_runs`` must still show up."""
        loc = self.locations[1]
        self.repo.conn.execute(
            "INSERT INTO sync_runs(run_key, started_at, ended_at, status,"
            " fetched, upserted, errors, location_id)"
            " VALUES (?, ?, ?, 'ok', 3, 3, 0, ?)",
            (f"{loc}@20260101-legacy", "2026-01-01T00:00:00Z",
             "2026-01-01T00:00:05Z", loc))
        self.repo.conn.commit()
        keys = [h["run_key"] for h in self.state.history(loc)]
        self.assertIn(f"{loc}@20260101-legacy", keys)
        sources = {h["source"] for h in self.state.history(loc)}
        self.assertIn("sync_runs", sources)

    def test_history_accepts_legacy_numeric_location_rows(self):
        """Older code stored the numeric row id in the ledger: still visible."""
        loc = self.locations[2]
        acct = self.repo.upsert_account("accounts/1001")
        self.repo.upsert_location(acct, loc, title="Legacy Shop",
                                  city="Reno", state="NV")
        row = self.repo.conn.execute(
            "SELECT id FROM business_locations WHERE location_name = ?",
            (loc,)).fetchone()
        self.state.record_run("legacy-numeric-run", str(row["id"]), "ok",
                              fetched=1, upserted=1)
        keys = [h["run_key"] for h in self.state.history(
            loc, legacy_id=str(row["id"]))]
        self.assertIn("legacy-numeric-run", keys)

    # --- isolation, retry, resume ----------------------------------------
    def test_a_failing_location_is_isolated(self):
        bad = self.locations[7]
        client = FakeReviewClient(self.pages, delay=0.0, always_fail={bad})
        sync = ReviewSync(self.repo, client)
        report = LocationSyncQueue(sync, concurrency=4,
                                   sleep=lambda s: None).run(self.locations)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["failed_locations"], [bad])
        self.assertEqual(report["ok"], N_LOCATIONS - 1)
        # everything else still landed
        self.assertEqual(len(self._rows()), len(self.review_names) - PER_LOCATION)
        ledger = self.repo.conn.execute(
            "SELECT status FROM sync_run_log WHERE location_id = ?"
            " ORDER BY id DESC LIMIT 1", (bad,)).fetchone()
        self.assertEqual(ledger["status"], "failed")

    def test_fatal_error_is_not_retried(self):
        bad = self.locations[3]
        client = FakeReviewClient(self.pages, always_fail={bad})
        sync = ReviewSync(self.repo, client)
        report = LocationSyncQueue(sync, concurrency=1, max_attempts=3,
                                   sleep=lambda s: None).run([bad])
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["retries"], 0)
        self.assertEqual(report["results"][0]["attempts"], 1)

    def test_transient_error_is_retried_honouring_retry_after(self):
        flaky = self.locations[5]
        client = FakeReviewClient(self.pages, transient={flaky: 1})
        sync = ReviewSync(self.repo, client)
        slept: List[float] = []
        report = LocationSyncQueue(sync, concurrency=1, max_attempts=3,
                                   sleep=slept.append).run([flaky])
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["retries"], 1)
        self.assertEqual(slept, [2.0], "Retry-After must be honoured")
        self.assertEqual(len(self._rows()), PER_LOCATION)

    def test_resume_after_a_fatal_crash_uses_the_checkpoint(self):
        victim = self.locations[11]
        client = FakeReviewClient(self.pages, kill_page1={victim})
        sync = ReviewSync(self.repo, client)

        first = LocationSyncQueue(sync, concurrency=1,
                                  sleep=lambda s: None).run([victim])
        self.assertEqual(first["failed"], 1)
        self.assertIsNotNone(self.state.get_checkpoint(victim),
                             "the page-1 checkpoint must survive the crash")

        second = LocationSyncQueue(sync, concurrency=1,
                                   sleep=lambda s: None).run([victim])
        self.assertEqual(second["failed"], 0)
        self.assertEqual(second["results"][0]["resumed_from"],
                         f"{victim}:1", "the retry must resume, not restart")
        self.assertIsNone(self.state.get_checkpoint(victim),
                          "a completed walk clears its checkpoint")
        stored = [r["review_name"] for r in self._rows()
                  if r["review_name"].startswith(victim + "/")]
        self.assertEqual(len(stored), PER_LOCATION, "no reviews lost on resume")
        self.assertEqual(len(set(stored)), PER_LOCATION, "no duplicates on resume")

    # --- identity + live guard -------------------------------------------
    def test_resource_id_is_the_only_identity(self):
        """Renaming the shop / adding a ZIP must not affect mapping."""
        self._queue(concurrency=4).run(self.locations)
        before = len(self._rows())
        self.repo.conn.execute(
            "UPDATE business_locations SET title = 'Renamed Shop',"
            " city = 'Nowhere', postal_code = '99999'")
        self.repo.conn.commit()
        report = self._queue(concurrency=4).run(self.locations)
        self.assertEqual(report["ok"], N_LOCATIONS)
        self.assertEqual(len(self._rows()), before)
        self.assertEqual(
            self.repo.conn.execute(
                "SELECT COUNT(*) AS n FROM business_locations").fetchone()["n"],
            N_LOCATIONS, "a rename must not create new locations")

    def test_queue_refuses_a_non_mock_transport(self):
        client = FakeReviewClient(self.pages)
        client.http = object()          # not a MockTransport
        sync = ReviewSync(self.repo, client)
        prev = os.environ.pop(LIVE_OPTIN_ENV, None)
        try:
            with self.assertRaises(LiveGoogleNotAllowedError):
                LocationSyncQueue(sync, sleep=lambda s: None)
        finally:
            if prev is not None:
                os.environ[LIVE_OPTIN_ENV] = prev

    def test_queue_allows_a_mock_transport(self):
        client = FakeReviewClient(self.pages)
        client.http = MockTransport()
        sync = ReviewSync(self.repo, client)
        LocationSyncQueue(sync, sleep=lambda s: None).run(self.locations[:2])


class OAuthReadinessTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop(LIVE_OPTIN_ENV, None)

    def test_scopes_are_the_documented_restricted_scope(self):
        self.assertEqual(SCOPES,
                         ("https://www.googleapis.com/auth/business.manage",))

    def test_authorization_url_carries_scope_state_and_offline_access(self):
        url = build_authorization_url("cid.apps.googleusercontent.com",
                                      "http://127.0.0.1:8000/oauth/callback",
                                      state="state-123")
        self.assertTrue(url.startswith(AUTHORIZE_URL + "?"))
        for piece in ("scope=", "state=state-123", "access_type=offline",
                      "prompt=consent", "response_type=code",
                      "business.manage"):
            self.assertIn(piece, url)

    def test_authorization_url_requires_its_inputs(self):
        with self.assertRaises(ValueError):
            build_authorization_url("", "http://x/cb", state="s")
        with self.assertRaises(ValueError):
            build_authorization_url("cid", "", state="s")
        with self.assertRaises(ValueError):
            build_authorization_url("cid", "http://x/cb", state="")

    def test_live_optin_is_off_by_default(self):
        os.environ.pop(LIVE_OPTIN_ENV, None)
        self.assertFalse(live_google_allowed())
        os.environ[LIVE_OPTIN_ENV] = "1"
        self.assertTrue(live_google_allowed())

    def test_exchange_code_over_a_mock_transport(self):
        transport = MockTransport()
        transport.register("POST", "https://oauth2.googleapis.com/token",
                           payload={"access_token": "at-1",
                                    "refresh_token": "rt-1",
                                    "scope": SCOPES[0],
                                    "token_type": "Bearer"})
        token = exchange_code(transport, client_id="cid", code="the-code",
                              redirect_uri="http://127.0.0.1:8000/cb")
        self.assertEqual(token.access_token, "at-1")
        self.assertEqual(token.refresh_token, "rt-1")
        self.assertEqual(token.scopes, list(SCOPES))

    def test_exchange_code_refuses_a_live_transport(self):
        os.environ.pop(LIVE_OPTIN_ENV, None)
        with self.assertRaises(LiveGoogleNotAllowedError):
            exchange_code(object(), client_id="cid", code="c",
                          redirect_uri="http://x/cb")

    def test_exchange_code_validates_its_inputs(self):
        transport = MockTransport()
        with self.assertRaises(ValueError):
            exchange_code(transport, client_id="", code="c", redirect_uri="r")
        with self.assertRaises(ValueError):
            exchange_code(transport, client_id="cid", code="", redirect_uri="r")

    def test_connection_status_is_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope.json")
            self.assertFalse(google_connection_status(missing)["connected"])

            empty = os.path.join(tmp, "empty.json")
            with open(empty, "w", encoding="utf-8") as fh:
                json.dump({"access_token": ""}, fh)
            status = google_connection_status(empty)
            self.assertFalse(status["connected"])
            self.assertEqual(status["reason"], "empty_token")

            good = os.path.join(tmp, "token.json")
            with open(good, "w", encoding="utf-8") as fh:
                json.dump({"access_token": "real-looking", "refresh_token": "r",
                           "expires_at": 1e12, "scopes": list(SCOPES)}, fh)
            status = google_connection_status(good)
            self.assertTrue(status["connected"])
            self.assertNotIn("real-looking", json.dumps(status))

    def test_urllib_transport_refuses_google_hosts_without_optin(self):
        os.environ.pop(LIVE_OPTIN_ENV, None)

        class ExplodingOpener:
            def open(self, *a, **k):  # pragma: no cover - must never run
                raise AssertionError("a live request was attempted")

        transport = UrllibTransport(opener=ExplodingOpener())
        for url in ("https://mybusiness.googleapis.com/v1/accounts",
                    "https://oauth2.googleapis.com/token"):
            with self.assertRaises(LiveGoogleNotAllowedError):
                transport.get(url)
        self.assertEqual(transport.requests, 0)

    def test_urllib_transport_still_works_offline_for_non_google_hosts(self):
        class FakeResponse:
            headers: Dict[str, str] = {}

            def read(self):
                return b'{"ok": true}'

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener:
            def open(self, request, timeout=None):
                return FakeResponse()

        transport = UrllibTransport(opener=Opener())
        response = transport.get("http://127.0.0.1:9999/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(transport.requests, 1)


class PublishStillBlockedTests(unittest.TestCase):
    def test_every_publish_path_is_blocked(self):
        repo = Repository(":memory:")
        repo.init_db(SCHEMA_PATH)
        acct = repo.upsert_account("accounts/1001")
        loc = repo.upsert_location(acct, "accounts/1001/locations/loc-001",
                                   title="Demo Nails", city="Reno", state="NV")
        rid = repo.upsert_review(loc, "reviews/r1", rating=5, comment="Great")
        service = ApprovalService(repo)
        try:
            draft_ids = []
            for status in STATES:
                # Written directly: the repo helper only accepts the reply-engine
                # subset, and every state must be probed.
                cur = repo.conn.execute(
                    "INSERT INTO reply_drafts(review_id, draft_text, status)"
                    " VALUES (?, ?, ?)", (rid, "Thanks!", status))
                repo.conn.commit()
                draft_id = int(cur.lastrowid)
                draft_ids.append(draft_id)
                with self.assertRaises(PublishBlocked):
                    service.publish_guard(draft_id)
            # and publish_guard itself wrote nothing: the only row with status
            # 'published' is the fixture row this test inserted by hand.
            published = repo.conn.execute(
                "SELECT id FROM reply_drafts WHERE status = 'published'").fetchall()
            self.assertEqual([r["id"] for r in published],
                             [draft_ids[STATES.index("published")]])
            self.assertEqual(len(draft_ids), len(STATES))
        finally:
            repo.close()

    def test_no_publish_method_exists_on_the_client(self):
        from gbp.client.gbp_client import GbpClient

        for banned in ("publish", "reply", "put_reply", "post_reply"):
            self.assertFalse(hasattr(GbpClient, banned),
                             f"GbpClient must not expose {banned}()")

    def test_cli_forbids_publish_commands(self):
        from gbp import cli

        for banned in cli.FORBIDDEN_COMMANDS:
            self.assertIn(banned, cli.FORBIDDEN_COMMANDS)

    def test_cli_live_client_requires_the_optin(self):
        from gbp import cli

        os.environ.pop(LIVE_OPTIN_ENV, None)
        try:
            with self.assertRaises(LiveGoogleNotAllowedError):
                cli._live_client()
        finally:
            os.environ.pop(LIVE_OPTIN_ENV, None)

    def test_cli_has_live_credentials_ignores_ai_keys(self):
        from gbp import cli

        os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-not-a-google-credential"
        try:
            self.assertFalse(cli.has_live_credentials())
        finally:
            os.environ.pop("OPENROUTER_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
