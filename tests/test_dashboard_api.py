"""Offline tests for Group 6A dashboard API additions.

Covers server-side pagination/search/filter on ``GET /api/queue``, the new
``GET /api/locations/status`` sync-status endpoint and the ``GET /api/usage``
AI-usage aggregation. Standard library only, MOCK data only -- nothing here
touches the network (the server binds 127.0.0.1 on an ephemeral port). Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import (  # noqa: E402
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    create_server,
)
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402
from gbp.sync.state import SyncState  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")


class DashboardApiTestCase(unittest.TestCase):
    """In-memory repo + a running loopback server in mock (auth=None) mode."""

    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc_a = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-001",
            title="Demo Nails A", city="Garden Grove", state="CA", language="en",
        )
        self.loc_b = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-002",
            title="Demo Nails B", city="Westminster", state="CA", language="vi",
        )
        # A brand profile so /api/locations/status can surface business_name and
        # so the free-text search can match a business name that differs from
        # the location title.
        self.repo.conn.execute(
            "INSERT INTO brand_profiles(location_id, business_name, city, language)"
            " VALUES (?, ?, ?, ?)",
            (self.loc_a, "Sunny Nails Spa", "Garden Grove", "en"),
        )
        self.repo.conn.commit()

        # Reviews (loc A: two unanswered + one answered; loc B: one unanswered).
        self.r1 = self.repo.upsert_review(
            location_id=self.loc_a, review_name="reviews/d1", rating=5,
            comment="Amazing gel manicure, loved it!")
        self.r2 = self.repo.upsert_review(
            location_id=self.loc_a, review_name="reviews/d2", rating=1,
            comment="Terrible service, never again")
        self.r3 = self.repo.upsert_review(
            location_id=self.loc_a, review_name="reviews/d3", rating=5,
            comment="Great work", reply_text="Thank you!")
        self.r4 = self.repo.upsert_review(
            location_id=self.loc_b, review_name="reviews/d4", rating=4,
            comment="Nice place")

        # Drafts across the workflow states.
        self.repo.insert_reply_draft(self.r1, draft_text="Thanks!", status="needs_review")
        self.repo.insert_reply_draft(self.r2, draft_text="Sorry!", status="draft")
        self.repo.insert_reply_draft(self.r3, draft_text="Welcome", status="approved")
        self.repo.insert_reply_draft(self.r4, draft_text="Merci", status="rejected")

        # Sync ledger: loc A failed 3x in a row (alert), loc B succeeded once.
        self.sync = SyncState(self.repo)
        for i in range(3):
            self.sync.record_run(f"run-a-{i}", str(self.loc_a), "failed",
                                 fetched=0, upserted=0, errors=2,
                                 started_at="2026-10-14T10:00:00",
                                 ended_at=f"2026-10-14T10:0{i}:00")
        self.sync.record_run("run-b-0", str(self.loc_b), "ok",
                            fetched=4, upserted=4, errors=0,
                            started_at="2026-10-15T09:00:00",
                            ended_at="2026-10-15T09:01:00")

        # AI usage rows across two days.
        self._add_usage("2026-10-14T08:00:00", "generate", 2, 100, 50, 0.01)
        self._add_usage("2026-10-14T09:00:00", "classify", 1, 10, 5, 0.005)
        self._add_usage("2026-10-15T08:00:00", "generate", 3, 300, 200, 0.02)

        self.service = ApprovalService(self.repo)
        self.server = create_server(self.service, port=0)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repo.close()

    def _add_usage(self, ts, endpoint, calls, itok, otok, cost):
        self.repo.conn.execute(
            "INSERT INTO api_usage(ts, endpoint, calls, input_tokens,"
            " output_tokens, est_cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, endpoint, calls, itok, otok, cost),
        )
        self.repo.conn.commit()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - unexpected
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # ------------------------------------------------------------------ #
    # pagination
    # ------------------------------------------------------------------ #
    def test_queue_pagination_math(self):
        status, page1 = self._get("/api/queue?page=1&page_size=2")
        self.assertEqual(status, 200)
        self.assertEqual(set(page1), {"items", "total", "page", "page_size", "pages"})
        self.assertEqual(page1["total"], 4)
        self.assertEqual(page1["pages"], 2)
        self.assertEqual(page1["page"], 1)
        self.assertEqual(page1["page_size"], 2)
        self.assertEqual(len(page1["items"]), 2)

        status, page2 = self._get("/api/queue?page=2&page_size=2")
        self.assertEqual(status, 200)
        self.assertEqual(page2["page"], 2)
        self.assertEqual(len(page2["items"]), 2)

        # No overlap and full coverage across the two pages.
        ids = [r["draft_id"] for r in page1["items"]] + \
              [r["draft_id"] for r in page2["items"]]
        self.assertEqual(sorted(ids), sorted(set(ids)))
        self.assertEqual(len(ids), 4)

        # A page past the end is simply empty.
        status, pageN = self._get("/api/queue?page=9&page_size=2")
        self.assertEqual(pageN["items"], [])

    def test_queue_defaults_and_page_size_cap(self):
        status, payload = self._get("/api/queue")
        self.assertEqual(status, 200)
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["page_size"], DEFAULT_PAGE_SIZE)

        status, payload = self._get("/api/queue?page_size=1000")
        self.assertEqual(payload["page_size"], MAX_PAGE_SIZE)

        status, payload = self._get("/api/queue?page=0&page_size=0")
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["page_size"], 1)

    # ------------------------------------------------------------------ #
    # search + filters
    # ------------------------------------------------------------------ #
    def test_queue_search_filters_by_comment(self):
        status, payload = self._get("/api/queue?q=manicure")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["draft_id"],
                         self._draft_id_for_review(self.r1))

    def test_queue_search_filters_by_business_name(self):
        # "Sunny Nails Spa" is the brand_profiles business_name for loc A.
        status, payload = self._get("/api/queue?q=Sunny")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 3)
        for item in payload["items"]:
            self.assertEqual(item["location_id"], self.loc_a)

    def test_queue_search_has_no_match(self):
        status, payload = self._get("/api/queue?q=zzzzz-nothing")
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["pages"], 0)

    def test_each_filter_works(self):
        # stars
        _s, by_stars = self._get("/api/queue?stars=5")
        self.assertEqual(by_stars["total"], 2)
        # status
        _s, by_status = self._get("/api/queue?status=needs_review")
        self.assertEqual(by_status["total"], 1)
        self.assertEqual(by_status["items"][0]["status"], "needs_review")
        # language
        _s, by_lang = self._get("/api/queue?language=vi")
        self.assertEqual(by_lang["total"], 1)
        self.assertEqual(by_lang["items"][0]["location_id"], self.loc_b)
        # location_id (resource name)
        _s, by_loc = self._get("/api/queue?location_id=accounts/1001/locations/loc-001")
        self.assertEqual(by_loc["total"], 3)
        # since
        _s, since_all = self._get("/api/queue?since=2000-01-01")
        self.assertEqual(since_all["total"], 4)
        _s, since_none = self._get("/api/queue?since=2999-01-01")
        self.assertEqual(since_none["total"], 0)

    def _draft_id_for_review(self, review_id):
        row = self.repo.conn.execute(
            "SELECT id FROM reply_drafts WHERE review_id = ? ORDER BY id", (review_id,)
        ).fetchone()
        return int(row["id"])

    # ------------------------------------------------------------------ #
    # /api/locations/status
    # ------------------------------------------------------------------ #
    def test_locations_status_shape_and_alert(self):
        status, rows = self._get("/api/locations/status")
        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 2)
        expected_keys = {
            "business_name", "city", "state", "language", "last_sync_at",
            "last_sync_status", "last_error", "consecutive_failures",
            "needs_alert", "unanswered_reviews", "drafts_pending_review",
            "drafts_approved",
        }
        by_id = {r["location_id"]: r for r in rows}
        for r in rows:
            self.assertTrue(expected_keys.issubset(set(r)))

        a = by_id[self.loc_a]
        self.assertEqual(a["business_name"], "Sunny Nails Spa")
        self.assertEqual(a["city"], "Garden Grove")
        self.assertEqual(a["state"], "CA")
        self.assertEqual(a["language"], "en")
        self.assertEqual(a["last_sync_status"], "failed")
        self.assertEqual(a["consecutive_failures"], 3)
        self.assertTrue(a["needs_alert"])
        self.assertIsNotNone(a["last_error"])
        self.assertEqual(a["unanswered_reviews"], 2)
        self.assertEqual(a["drafts_pending_review"], 2)
        self.assertEqual(a["drafts_approved"], 1)

        b = by_id[self.loc_b]
        self.assertEqual(b["last_sync_status"], "ok")
        self.assertEqual(b["consecutive_failures"], 0)
        self.assertFalse(b["needs_alert"])
        self.assertIsNone(b["last_error"])
        self.assertEqual(b["unanswered_reviews"], 1)

    # ------------------------------------------------------------------ #
    # /api/usage
    # ------------------------------------------------------------------ #
    def test_usage_aggregation(self):
        status, usage = self._get("/api/usage")
        self.assertEqual(status, 200)
        self.assertEqual(usage["total_calls"], 6)
        self.assertEqual(usage["input_tokens"], 410)
        self.assertEqual(usage["output_tokens"], 255)
        self.assertAlmostEqual(usage["estimated_cost_usd"], 0.035, places=6)

        by_day = {d["date"]: d for d in usage["by_day"]}
        self.assertEqual(set(by_day), {"2026-10-14", "2026-10-15"})
        self.assertEqual(by_day["2026-10-14"]["calls"], 3)
        self.assertEqual(by_day["2026-10-14"]["input_tokens"], 110)
        self.assertEqual(by_day["2026-10-14"]["output_tokens"], 55)
        self.assertAlmostEqual(by_day["2026-10-14"]["estimated_cost_usd"], 0.015, places=6)
        self.assertEqual(by_day["2026-10-15"]["calls"], 3)
        self.assertEqual(by_day["2026-10-15"]["input_tokens"], 300)
        # per-location is not derivable from the schema -> reported empty.
        self.assertEqual(usage["per_location"], [])

    def test_usage_empty_table_is_all_zero(self):
        self.repo.conn.execute("DELETE FROM api_usage")
        self.repo.conn.commit()
        _s, usage = self._get("/api/usage")
        self.assertEqual(usage["total_calls"], 0)
        self.assertEqual(usage["estimated_cost_usd"], 0)
        self.assertEqual(usage["by_day"], [])


if __name__ == "__main__":
    unittest.main()
