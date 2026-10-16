"""Offline tests for Group 7 / T19 -- Overview & Locations V2 API additions.

Everything is asserted against the SEEDED mock SQLite DB with a direct SQL
query, so the API can never drift from the data. Standard library only, MOCK
data only -- nothing here touches the network (the server binds 127.0.0.1 on an
ephemeral port). Run with:

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

from gbp.api.server import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, create_server  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.sync.state import SyncState  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")

LONG_COMMENT = "Worst pedicure ever, " + ("x" * 200)


class OverviewLocationsApiTestCase(unittest.TestCase):
    """In-memory repo + a running loopback server in mock (auth=None) mode."""

    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc1 = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-001", title="Demo Nails A",
            city="Garden Grove", state="CA", language="en")
        self.loc2 = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-002", title="Demo Nails B",
            city="Westminster", state="CA", language="vi")
        self.loc3 = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-003", title="Demo Nails C",
            city="Reno", state="NV", language="en")

        # Brand profiles: only loc-001 auto-publishes.
        self.repo.conn.executemany(
            "INSERT INTO brand_profiles(location_id, business_name, city,"
            " language, auto_publish_allowed) VALUES (?, ?, ?, ?, ?)",
            [(self.loc1, "Sunny Nails Spa", "Garden Grove", "en", 1),
             (self.loc2, "Bright Nails", "Westminster", "vi", 0),
             (self.loc3, "Lakeside Salon", "Reno", "en", 0)],
        )
        self.repo.conn.commit()

        # Reviews -- four carry a create_time, one does not.
        self.r1 = self.repo.upsert_review(
            self.loc1, "reviews/r1", rating=5, comment=LONG_COMMENT,
            create_time="2026-10-10T10:00:00")
        self.r2 = self.repo.upsert_review(
            self.loc1, "reviews/r2", rating=1, comment="Bad",
            create_time="2026-10-11T09:00:00")
        self.r3 = self.repo.upsert_review(
            self.loc1, "reviews/r3", rating=4, comment="Good",
            reply_text="Thank you!", create_time="2026-10-11T12:00:00")
        self.r4 = self.repo.upsert_review(
            self.loc2, "reviews/r4", rating=5, comment="Great",
            create_time="2026-10-12T08:00:00")
        self.r5 = self.repo.upsert_review(
            self.loc3, "reviews/r5", rating=3, comment="OK")

        # Drafts across the workflow states. ready_to_publish / published are
        # written directly because the repository helper only accepts the four
        # reply-engine statuses.
        self.repo.insert_reply_draft(self.r1, "Thanks!", status="needs_review")
        self.repo.insert_reply_draft(self.r2, "Sorry", status="draft")
        self.repo.insert_reply_draft(self.r3, "Welcome", status="approved")
        self.repo.conn.executemany(
            "INSERT INTO reply_drafts(review_id, draft_text, status) VALUES (?, ?, ?)",
            [(self.r4, "Merci", "ready_to_publish"),
             (self.r5, "Thanks", "published")],
        )
        self.repo.conn.commit()

        # Sync ledger: loc1 failed 3x in a row (alert), loc2 ok, loc3 none.
        sync = SyncState(self.repo)
        for i in range(3):
            sync.record_run(f"run-a-{i}", str(self.loc1), "failed", errors=2,
                            started_at="2026-10-14T10:00:00",
                            ended_at=f"2026-10-14T10:0{i}:00")
        sync.record_run("run-b-0", str(self.loc2), "ok", fetched=4, upserted=4,
                        started_at="2026-10-15T09:00:00",
                        ended_at="2026-10-15T09:01:00")

        # AI usage rows (only phase that may ever write api_usage).
        self._add_usage("2026-10-14T08:00:00", 2, 100, 50, 0.02)
        self._add_usage("2026-10-15T08:00:00", 1, 10, 5, 0.005)

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

    def _add_usage(self, ts, calls, itok, otok, cost):
        self.repo.conn.execute(
            "INSERT INTO api_usage(ts, endpoint, calls, input_tokens,"
            " output_tokens, est_cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, "generate", calls, itok, otok, cost))
        self.repo.conn.commit()

    def _sql(self, query, params=()):
        return self.repo.conn.execute(query, params).fetchone()[0]

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # ------------------------------------------------------------------ #
    # /api/dashboard -- KPIs vs direct SQL
    # ------------------------------------------------------------------ #
    def test_dashboard_keeps_legacy_keys(self):
        status, data = self._get("/api/dashboard")
        self.assertEqual(status, 200)
        for key in ("draft", "needs_review", "approved", "rejected",
                    "ready_to_publish", "published", "failed", "total"):
            self.assertIn(key, data)
        self.assertEqual(data["needs_review"], 1)

    def test_kpis_equal_direct_sql_counts(self):
        _s, data = self._get("/api/dashboard")
        kpi = data["kpis"]
        self.assertEqual(set(kpi), {
            "total_locations", "total_reviews", "unanswered_reviews",
            "needs_approval", "approved_replies", "published_replies",
            "sync_errors", "estimated_ai_cost_usd"})

        self.assertEqual(kpi["total_locations"], self._sql(
            "SELECT COUNT(*) FROM business_locations"))
        self.assertEqual(kpi["total_reviews"], self._sql(
            "SELECT COUNT(*) FROM reviews"))
        self.assertEqual(kpi["unanswered_reviews"], self._sql(
            "SELECT COUNT(*) FROM reviews WHERE reply_text IS NULL"
            " OR TRIM(reply_text) = ''"))
        self.assertEqual(kpi["needs_approval"], self._sql(
            "SELECT COUNT(*) FROM reply_drafts"
            " WHERE status IN ('draft', 'needs_review')"))
        self.assertEqual(kpi["approved_replies"], self._sql(
            "SELECT COUNT(*) FROM reply_drafts"
            " WHERE status IN ('approved', 'ready_to_publish')"))
        self.assertEqual(kpi["published_replies"], self._sql(
            "SELECT COUNT(*) FROM reply_drafts WHERE status = 'published'"))
        # loc1's most recent run failed -> exactly one location in error.
        self.assertEqual(kpi["sync_errors"], 1)

    def test_estimated_cost_comes_from_api_usage_only(self):
        _s, data = self._get("/api/dashboard")
        expected = self._sql("SELECT COALESCE(SUM(est_cost_usd), 0) FROM api_usage")
        self.assertAlmostEqual(data["kpis"]["estimated_ai_cost_usd"], expected, places=6)
        self.assertAlmostEqual(expected, 0.025, places=6)

    def test_estimated_cost_is_zero_when_api_usage_empty(self):
        self.repo.conn.execute("DELETE FROM api_usage")
        self.repo.conn.commit()
        _s, data = self._get("/api/dashboard")
        self.assertEqual(data["kpis"]["estimated_ai_cost_usd"], 0.0)

    def test_rating_distribution_matches_sql_and_sums_to_total(self):
        _s, data = self._get("/api/dashboard")
        dist = data["rating_distribution"]
        self.assertEqual([d["stars"] for d in dist], [1, 2, 3, 4, 5])
        by_star = {d["stars"]: d["count"] for d in dist}
        for star in (1, 2, 3, 4, 5):
            self.assertEqual(by_star[star], self._sql(
                "SELECT COUNT(*) FROM reviews WHERE rating = ?", (star,)))
        self.assertEqual(sum(by_star.values()), data["kpis"]["total_reviews"])

    def test_review_trend_matches_sql_or_is_explicitly_unavailable(self):
        _s, data = self._get("/api/dashboard")
        if data.get("review_trend_available"):
            expected = {
                row[0]: row[1] for row in self.repo.conn.execute(
                    "SELECT date(COALESCE(create_time, update_time)) AS d,"
                    " COUNT(*) FROM reviews"
                    " WHERE COALESCE(create_time, update_time) IS NOT NULL"
                    " GROUP BY d ORDER BY d").fetchall()
            }
            self.assertEqual({d["date"]: d["count"] for d in data["review_trend"]},
                             expected)
            self.assertEqual(data["review_trend"],
                             [{"date": d, "count": c} for d, c in sorted(expected.items())])
        else:
            # No usable timestamp -> empty series, never a fabricated one.
            self.assertEqual(data["review_trend"], [])

    def test_review_trend_unavailable_without_timestamps(self):
        self.repo.conn.execute("UPDATE reviews SET create_time = NULL, update_time = NULL")
        self.repo.conn.commit()
        _s, data = self._get("/api/dashboard")
        self.assertEqual(data["review_trend"], [])
        self.assertFalse(data["review_trend_available"])

    def test_attention_locations_worst_first_and_capped(self):
        _s, data = self._get("/api/dashboard")
        att = data["attention_locations"]
        self.assertLessEqual(len(att), 10)
        for row in att:
            self.assertTrue(row["needs_alert"] or row["unanswered_reviews"] > 0)
        # Every flagged location is present...
        self.assertEqual([r["location_id"] for r in att],
                         [self.loc1, self.loc2, self.loc3])
        # ...and the alerting one comes first.
        self.assertTrue(att[0]["needs_alert"])
        self.assertEqual(att[0]["location_id"], self.loc1)
        # Ordered by most unanswered among the non-alerting rows.
        rest = [r["unanswered_reviews"] for r in att[1:]]
        self.assertEqual(rest, sorted(rest, reverse=True))

    def test_recent_reviews_show_business_name_not_resource_id(self):
        _s, data = self._get("/api/dashboard")
        recent = data["recent_reviews"]
        self.assertLessEqual(len(recent), 10)
        for row in recent:
            self.assertNotIn("accounts/1001/locations", str(row["business_name"]))
            self.assertIn("business_name", row)
            self.assertIn("stars", row)
            self.assertIn("status", row)
            self.assertIn("comment", row)
            self.assertIn("date", row)
        # The long comment is excerpted, not dumped whole.
        long_rows = [r for r in recent if r["business_name"] == "Sunny Nails Spa"]
        self.assertTrue(long_rows)
        self.assertLessEqual(len(max((r["comment"] for r in long_rows), key=len)), 161)

    def test_dashboard_get_does_not_trigger_any_ai_call(self):
        before = self._sql("SELECT COUNT(*) FROM api_usage")
        self._get("/api/dashboard")
        self._get("/api/dashboard")
        self.assertEqual(self._sql("SELECT COUNT(*) FROM api_usage"), before)

    # ------------------------------------------------------------------ #
    # /api/locations -- legacy shape + paginated envelope
    # ------------------------------------------------------------------ #
    def test_bare_locations_call_keeps_legacy_list_shape(self):
        status, body = self._get("/api/locations")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 3)
        self.assertIn("brand_complete", body[0])

    def test_locations_pagination_math(self):
        for query in ("?page=1&page_size=2", "?page=1", "?q=", "?sort=business_name"):
            status, page1 = self._get(f"/api/locations{query}")
            self.assertEqual(status, 200)
        _s, page1 = self._get("/api/locations?page=1&page_size=2")
        self.assertEqual(set(page1) >= {"items", "total", "page", "page_size", "pages"},
                         True)
        self.assertEqual(page1["total"], 3)
        self.assertEqual(page1["pages"], 2)
        self.assertEqual(page1["page"], 1)
        self.assertEqual(page1["page_size"], 2)
        self.assertEqual(len(page1["items"]), 2)

        _s, page2 = self._get("/api/locations?page=2&page_size=2")
        self.assertEqual(len(page2["items"]), 1)
        ids = [r["id"] for r in page1["items"]] + [r["id"] for r in page2["items"]]
        self.assertEqual(sorted(ids), sorted({self.loc1, self.loc2, self.loc3}))

        _s, past = self._get("/api/locations?page=9&page_size=2")
        self.assertEqual(past["items"], [])

    def test_locations_defaults_and_page_size_cap(self):
        _s, payload = self._get("/api/locations?page=1")
        self.assertEqual(payload["page_size"], DEFAULT_PAGE_SIZE)
        _s, payload = self._get("/api/locations?page_size=1000")
        self.assertEqual(payload["page_size"], MAX_PAGE_SIZE)
        _s, payload = self._get("/api/locations?page=0&page_size=0")
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["page_size"], 1)

    def test_locations_item_shape(self):
        _s, payload = self._get("/api/locations?page=1&page_size=100")
        item = next(r for r in payload["items"] if r["id"] == self.loc1)
        for key in ("business_name", "city", "state", "language", "total_reviews",
                    "unanswered", "needs_approval", "sync_status", "last_sync",
                    "last_error", "needs_alert", "brand_profile_configured",
                    "auto_publish_policy"):
            self.assertIn(key, item)
        self.assertEqual(item["business_name"], "Sunny Nails Spa")
        self.assertNotIn("accounts/1001/locations", str(item["business_name"]))
        self.assertEqual(item["total_reviews"], 3)
        self.assertEqual(item["unanswered"], 2)
        self.assertEqual(item["needs_approval"], 2)
        self.assertEqual(item["sync_status"], "failed")
        self.assertTrue(item["needs_alert"])
        self.assertTrue(item["brand_profile_configured"])
        self.assertEqual(item["auto_publish_policy"], "auto")

    def test_locations_filters(self):
        _s, by_q = self._get("/api/locations?q=Sunny&page_size=100")
        self.assertEqual(by_q["total"], 1)
        self.assertEqual(by_q["items"][0]["id"], self.loc1)

        _s, by_state = self._get("/api/locations?state=CA&page_size=100")
        self.assertEqual(by_state["total"], 2)
        self.assertEqual(sorted(r["id"] for r in by_state["items"]),
                         sorted([self.loc1, self.loc2]))

        _s, low = self._get("/api/locations?state=ca&page_size=100")
        self.assertEqual(low["total"], 2)

        _s, by_sync = self._get("/api/locations?sync_status=failed&page_size=100")
        self.assertEqual(by_sync["total"], 1)
        self.assertEqual(by_sync["items"][0]["id"], self.loc1)

        _s, combined = self._get(
            "/api/locations?state=CA&sync_status=ok&page_size=100")
        self.assertEqual(combined["total"], 1)
        self.assertEqual(combined["items"][0]["id"], self.loc2)

        _s, none = self._get("/api/locations?q=zzzzz-nothing&page_size=100")
        self.assertEqual(none["total"], 0)
        self.assertEqual(none["items"], [])
        self.assertEqual(none["pages"], 0)

    def test_each_locations_sort_column(self):
        cases = [("business_name", "Sunny Nails Spa", "Lakeside Salon"),
                 ("city", "Garden Grove", "Westminster"),
                 ("state", "CA", "NV"),
                 ("total_reviews", "Demo Nails B", "Sunny Nails Spa"),
                 ("unanswered", "Bright Nails", "Sunny Nails Spa")]
        for column, _first_asc, _last_asc in cases:
            if column in ("total_reviews", "unanswered"):
                _s, asc = self._get(f"/api/locations?sort={column}&order=asc&page_size=100")
                _s, desc = self._get(f"/api/locations?sort={column}&order=desc&page_size=100")
                asc_vals = [r[column] for r in asc["items"]]
                self.assertEqual(asc_vals, sorted(asc_vals))
                self.assertEqual([r[column] for r in desc["items"]],
                                 sorted(asc_vals, reverse=True))
            else:
                _s, asc = self._get(f"/api/locations?sort={column}&order=asc&page_size=100")
                _s, desc = self._get(f"/api/locations?sort={column}&order=desc&page_size=100")
                asc_vals = [str(r[column]) for r in asc["items"]]
                self.assertEqual(asc_vals, sorted(asc_vals))
                self.assertEqual([str(r[column]) for r in desc["items"]],
                                 sorted(asc_vals, reverse=True))

        # last_sync: isolated to the single location that has a run.
        _s, by_sync = self._get("/api/locations?sort=last_sync&order=desc&page_size=100")
        self.assertEqual(by_sync["items"][0]["id"], self.loc2)

        # An unknown sort column falls back to business_name (never a 500).
        _s, bogus = self._get("/api/locations?sort=../../etc/passwd&page_size=100")
        self.assertEqual(bogus["sort"], "business_name")
        self.assertEqual(bogus["items"][0]["business_name"], "Bright Nails")

    # ------------------------------------------------------------------ #
    # /api/locations/<id>
    # ------------------------------------------------------------------ #
    def test_location_detail_ok_and_missing(self):
        status, detail = self._get(f"/api/locations/{self.loc1}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["id"], self.loc1)
        self.assertEqual(detail["business_name"], "Sunny Nails Spa")
        self.assertEqual(detail["total_reviews"], 3)
        self.assertEqual(detail["unanswered"], 2)
        self.assertIn("brand_profile", detail)
        self.assertTrue(detail["brand_profile"]["configured"])
        self.assertEqual(detail["brand_profile"]["business_name"], "Sunny Nails Spa")

        self.assertEqual(self._get("/api/locations/999999")[0], 404)
        self.assertEqual(self._get("/api/locations/not-an-id")[0], 404)

    def test_location_detail_does_not_trigger_any_ai_call(self):
        before = self._sql("SELECT COUNT(*) FROM api_usage")
        self._get(f"/api/locations/{self.loc1}")
        self.assertEqual(self._sql("SELECT COUNT(*) FROM api_usage"), before)


if __name__ == "__main__":
    unittest.main()
