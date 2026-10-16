"""Group 8B / T27 -- scalability & reliability at 500 locations.

Everything is offline and disposable: a temporary SQLite file, a fake review
client, and the real API server on loopback. No Google, no network.

Covered:
* 500 locations / 2000 reviews: pagination, search, filter, sort correctness;
* endpoint latency percentiles with deliberately generous bounds (this is a
  regression guard, not a benchmark -- run ``scripts/bench_scale.py`` for the
  real numbers that go into ``docs/SCALE-REPORT.md``);
* per-location RBAC at scale (a scoped user sees and touches only their shops);
* interrupted sync: resume with no lost review and no duplicate;
* the unified sync history + the additive ``sync_runs.location_id`` migration.
"""

from __future__ import annotations

import http.client
import json
import os
import statistics
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from gbp.api.server import create_server  # noqa: E402
from gbp.authz import AuthService, UserStore  # noqa: E402
from gbp.authz.models import hash_password  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.sync.queue import LocationSyncQueue  # noqa: E402
from gbp.sync.reviews_sync import ReviewSync  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

import bench_scale  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
TEST_ENV = {"GBP_SESSION_SECRET": "t27-scale-secret-not-used-elsewhere"}
ADMIN_PW = "ScaleAdmin-2026-ok!"
REVIEWER_PW = "ScaleReviewer-2026!"

N_LOCATIONS = 500
PER_LOCATION = 4
#: Latency guard. Generous on purpose: the point is to catch an accidental
#: N+1 regression (which pushed these endpoints to ~70 ms per request at 500
#: locations), not to fail on a busy CI box.
P95_BUDGET_MS = 500.0


class FatalError(RuntimeError):
    """Non-retryable crash (used to simulate an interrupted sync)."""


class InterruptibleClient(bench_scale._PageClient):
    """``_PageClient`` that can fail a location's second page once."""

    def __init__(self, pages, kill_page1=None):
        super().__init__(pages)
        self.kill_page1 = set(kill_page1 or ())

    def list_reviews(self, location: str, page_token=None):
        index = 0 if not page_token else int(str(page_token).rsplit(":", 1)[-1])
        if location in self.kill_page1 and index == 1:
            self.kill_page1.discard(location)
            raise FatalError(f"interrupted: {location}")
        return super().list_reviews(location, page_token=page_token)


class ScaleBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "scale.db")
        self.repo, self.locations, self.pages = bench_scale.seed(
            self.db_path, N_LOCATIONS, PER_LOCATION)
        self.service = ApprovalService(self.repo)
        self.servers: List[Any] = []

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.repo.close()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _start(self, auth=None):
        server = create_server(self.service, port=0, auth=auth,
                               rate_limit=10000, login_limit=10000)
        host, port = server.server_address[:2]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return port

    def _get(self, port, path, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        headers = {"Cookie": cookie} if cookie else {}
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else None)
        finally:
            conn.close()

    def _measure(self, port, path, iterations=8) -> Dict[str, float]:
        timings: List[float] = []
        for _ in range(iterations):
            start = time.perf_counter()
            status, _body = self._get(port, path)
            self.assertEqual(status, 200, path)
            timings.append((time.perf_counter() - start) * 1000.0)
        ordered = sorted(timings)
        idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return {"p50": statistics.median(timings), "p95": ordered[idx],
                "max": max(timings)}


class ApiScaleTests(ScaleBase):
    def test_pagination_search_filter_sort_at_500_locations(self):
        port = self._start()
        status, page = self._get(port, "/api/locations?page=2&page_size=25")
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], N_LOCATIONS)
        self.assertEqual(page["pages"], N_LOCATIONS // 25)
        self.assertEqual(len(page["items"]), 25)

        _s, found = self._get(port, "/api/locations?q=Bench%20Nails%20250")
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["items"][0]["business_name"], "Bench Nails 250")

        _s, by_state = self._get(port, "/api/locations?state=CA&page_size=100")
        expected_ca = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM business_locations WHERE state = 'CA'"
        ).fetchone()["n"]
        self.assertEqual(by_state["total"], expected_ca)
        self.assertTrue(all(i["state"] == "CA" for i in by_state["items"]))

        _s, sorted_rows = self._get(
            port, "/api/locations?sort=business_name&order=desc&page_size=10")
        names = [i["business_name"] for i in sorted_rows["items"]]
        self.assertEqual(names, sorted(names, reverse=True))

    def test_zip_search_still_works_at_scale(self):
        port = self._start()
        _s, found = self._get(port, f"/api/locations?q={90000 + (250 % 999):05d}")
        self.assertGreaterEqual(found["total"], 1)
        self.assertTrue(all(i["postal_code"] for i in found["items"]))

    def test_endpoint_latency_budget(self):
        port = self._start()
        measured = {}
        for path in ("/api/locations?page=1&page_size=25",
                     "/api/locations?q=Bench%20Nails%20250",
                     "/api/dashboard",
                     "/api/reviews?page=1&page_size=25"):
            stats = self._measure(port, path)
            measured[path] = stats
            self.assertLess(stats["p95"], P95_BUDGET_MS,
                            f"{path} p95={stats['p95']:.1f}ms")
        # Guard against the N+1 pattern that used to make the location
        # endpoints ~70 ms (a per-location query loop). A page of locations may
        # be slower than a tiny review page, but only by a bounded factor.
        self.assertLess(measured["/api/locations?page=1&page_size=25"]["p95"],
                        100.0)

    def test_dashboard_at_scale(self):
        port = self._start()
        _s, data = self._get(port, "/api/dashboard")
        self.assertEqual(data["kpis"]["total_locations"], N_LOCATIONS)
        self.assertEqual(data["kpis"]["total_reviews"], 0)
        self.assertEqual(data["recent_reviews"], [])

        sync = ReviewSync(self.repo, bench_scale._PageClient(self.pages))
        LocationSyncQueue(sync, concurrency=4, sleep=lambda _s: None).run(
            self.locations)
        _s, data = self._get(port, "/api/dashboard")
        self.assertEqual(data["kpis"]["total_reviews"],
                         N_LOCATIONS * PER_LOCATION)
        self.assertEqual(len(data["recent_reviews"]), 10)
        self.assertEqual(data["kpis"]["unanswered_reviews"],
                         N_LOCATIONS * PER_LOCATION)


class RbacScaleTests(ScaleBase):
    """A scoped user must not see or touch other locations (500-location data)."""

    def setUp(self):
        super().setUp()
        store = UserStore(self.repo.conn)
        store.create_user("admin", hash_password(ADMIN_PW), ["admin"], [])
        self.allowed = [str(i) for i in (3, 150, 400)]
        store.create_user("reviewer", hash_password(REVIEWER_PW), ["reviewer"],
                          self.allowed)
        self.auth = AuthService(self.repo.conn, env=TEST_ENV, users=[])
        self.port = self._start(auth=self.auth)
        self.cookie = self._login("reviewer", REVIEWER_PW)

    def _login(self, username, password):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps({"username": username, "password": password}).encode()
        try:
            conn.request("POST", "/api/login", body=payload,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            if resp.status != 200:
                self.fail(f"login failed: {resp.status}")
            return resp.getheader("Set-Cookie", "").split(";")[0]
        finally:
            conn.close()

    def test_scoped_user_sees_only_their_locations(self):
        _s, page = self._get(self.port, "/api/locations?page_size=50", self.cookie)
        self.assertEqual(page["total"], len(self.allowed))
        self.assertEqual(sorted(str(i["id"]) for i in page["items"]),
                         sorted(self.allowed))

    def test_scoped_user_cannot_read_a_foreign_location(self):
        status, _b = self._get(self.port, "/api/locations/5", self.cookie)
        self.assertEqual(status, 403)
        status, _b = self._get(self.port, "/api/locations/3", self.cookie)
        self.assertEqual(status, 200)

    def test_dashboard_counts_are_scoped(self):
        _s, data = self._get(self.port, "/api/dashboard", self.cookie)
        self.assertEqual(data["kpis"]["total_locations"], len(self.allowed))

    def test_foreign_location_ledger_is_forbidden(self):
        status, _b = self._get(self.port, "/api/locations/5/sync-runs", self.cookie)
        self.assertEqual(status, 403)


class ReliabilityTests(ScaleBase):
    def test_interrupted_sync_resumes_without_loss_or_duplication(self):
        victims = self.locations[10:16]
        client = InterruptibleClient(self.pages, kill_page1=set(victims))
        sync = ReviewSync(self.repo, client)

        first = LocationSyncQueue(sync, concurrency=4,
                                  sleep=lambda _s: None).run(victims)
        self.assertEqual(first["failed"], len(victims))
        # Page 1 was already streamed into the database before the crash (that
        # is the T26/T27 fix: the checkpoint only advances once the page it
        # describes is stored), so exactly the page-1 reviews exist.
        page1_rows = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]
        self.assertEqual(page1_rows, len(victims) * (PER_LOCATION // 2))

        second = LocationSyncQueue(sync, concurrency=4,
                                   sleep=lambda _s: None).run(victims)
        self.assertEqual(second["failed"], 0)
        resumed = [r for r in second["results"] if r.get("resumed_from")]
        self.assertEqual(len(resumed), len(victims),
                         "every victim resumed from its checkpoint")

        rows = self.repo.conn.execute(
            "SELECT review_name FROM reviews").fetchall()
        names = [r["review_name"] for r in rows]
        self.assertEqual(len(names), len(victims) * PER_LOCATION)
        self.assertEqual(len(set(names)), len(names), "no duplicates after resume")
        left = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM sync_state WHERE checkpoint IS NOT NULL"
        ).fetchone()["n"]
        self.assertEqual(left, 0, "no stale checkpoints")

    def test_full_500_sync_is_idempotent_and_complete(self):
        sync = ReviewSync(self.repo, bench_scale._PageClient(self.pages))
        queue = LocationSyncQueue(sync, concurrency=4, sleep=lambda _s: None)
        report = queue.run(self.locations)
        self.assertEqual((report["ok"], report["failed"]), (N_LOCATIONS, 0))
        self.assertEqual(report["fetched"], N_LOCATIONS * PER_LOCATION)

        before = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]
        queue.run(self.locations)
        after = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]
        self.assertEqual(before, after, "a re-run must not duplicate reviews")
        self.assertEqual(before, N_LOCATIONS * PER_LOCATION)

    def test_queue_sync_is_visible_to_the_dashboard(self):
        """T27: queue-driven syncs now reach the ledger the Dashboard reads."""
        targets = self.locations[:4]
        sync = ReviewSync(self.repo, bench_scale._PageClient(self.pages))
        LocationSyncQueue(sync, concurrency=2, sleep=lambda _s: None).run(targets)

        port = self._start()
        _s, status_rows = self._get(port, "/api/locations/status")
        rows = status_rows if isinstance(status_rows, list) else status_rows["items"]
        seen = {r["location_id"]: r for r in rows}
        for i, loc in enumerate(targets, start=1):
            row = self.repo.conn.execute(
                "SELECT id FROM business_locations WHERE location_name = ?",
                (loc,)).fetchone()
            self.assertEqual(seen[row["id"]]["last_sync_status"], "ok")
            self.assertIsNotNone(seen[row["id"]]["last_sync_at"])
        # untouched locations stay honest
        self.assertIsNone(seen[500]["last_sync_status"])


class MigrationTests(unittest.TestCase):
    """The ``sync_runs.location_id`` migration must be additive and lossless."""

    def test_legacy_sync_runs_table_gains_the_column_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "legacy.db")
            repo = Repository(db)
            repo.init_db(SCHEMA_PATH)
            acct = repo.upsert_account("accounts/1001")
            repo.upsert_location(acct, "accounts/1001/locations/loc-001",
                                 title="Legacy Shop", city="Reno", state="NV")
            # simulate the OLD shape: no location_id column, one legacy row
            repo.conn.execute("DROP TABLE sync_runs")
            repo.conn.execute(
                "CREATE TABLE sync_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " run_key TEXT NOT NULL UNIQUE, started_at TEXT, ended_at TEXT,"
                " status TEXT, fetched INTEGER DEFAULT 0, upserted INTEGER"
                " DEFAULT 0, errors INTEGER DEFAULT 0)")
            repo.conn.execute(
                "INSERT INTO sync_runs(run_key, started_at, ended_at, status,"
                " fetched, upserted, errors) VALUES"
                " ('accounts/1001/locations/loc-001@20260101', '2026-01-01T00:00:00Z',"
                "  '2026-01-01T00:00:01Z', 'ok', 4, 4, 0)")
            repo.conn.commit()
            repo.close()

            repo = Repository(db)
            repo.init_db(SCHEMA_PATH)          # the migration runs here
            cols = {r[1] for r in repo.conn.execute("PRAGMA table_info(sync_runs)")}
            self.assertIn("location_id", cols)

            row = repo.conn.execute(
                "SELECT run_key, status, location_id FROM sync_runs").fetchone()
            self.assertEqual(row["status"], "ok")           # row preserved
            self.assertEqual(row["location_id"],
                             "accounts/1001/locations/loc-001")  # back-filled
            repo.close()

    def test_unified_history_survives_a_second_migration_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "idem.db")
            repo = Repository(db)
            repo.init_db(SCHEMA_PATH)
            acct = repo.upsert_account("accounts/1001")
            loc = "accounts/1001/locations/loc-001"
            repo.upsert_location(acct, loc, title="Shop", city="Reno", state="NV")
            repo.conn.execute(
                "INSERT INTO sync_runs(run_key, status, started_at) VALUES"
                " ('legacy-run', 'ok', '2026-01-01T00:00:00Z')")
            repo.conn.commit()
            before = repo.conn.execute(
                "SELECT COUNT(*) AS n FROM sync_runs").fetchone()["n"]
            repo.init_db(SCHEMA_PATH)          # idempotent: nothing is dropped
            after = repo.conn.execute(
                "SELECT COUNT(*) AS n FROM sync_runs").fetchone()["n"]
            self.assertEqual(before, after)
            repo.close()


if __name__ == "__main__":
    unittest.main()
