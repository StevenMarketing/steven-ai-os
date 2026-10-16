"""Group 7 / T21 -- demo seeder idempotency + honest draft/status signals.

Standard library only, MOCK data only, NO network, NO Google, NO AI. The
seeder writes a throwaway SQLite file in a TemporaryDirectory -- no real DB is
ever touched. Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from gbp.api.server import create_server  # noqa: E402

import serve_ui  # noqa: E402  (scripts/serve_ui.py)


def _counts(repo):
    reviews = repo.conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]
    drafts = repo.conn.execute("SELECT COUNT(*) AS n FROM reply_drafts").fetchone()["n"]
    dupes = repo.conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT review_id FROM reply_drafts"
        " GROUP BY review_id HAVING COUNT(*) > 1)"
    ).fetchone()["n"]
    nulls = repo.conn.execute(
        "SELECT COUNT(*) AS n FROM reply_drafts WHERE draft_text IS NULL"
    ).fetchone()["n"]
    return int(reviews), int(drafts), int(dupes), int(nulls)


class DemoSeedIdempotencyTests(unittest.TestCase):
    """Seeding the SAME db file three times must not grow reply_drafts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "demo.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_seeds_are_constant(self):
        # Run 1 seeds; runs 2 and 3 must be no-ops (no new rows, no duplicates).
        service = serve_ui.build_service(self.db)
        r1 = _counts(service.repo)
        service.repo.close()

        service = serve_ui.build_service(self.db)
        r2 = _counts(service.repo)
        service.repo.close()

        service = serve_ui.build_service(self.db)
        r3 = _counts(service.repo)
        service.repo.close()

        # reviews stay 14 on every run
        self.assertEqual(r1[0], 14, "reviews should be 14")
        self.assertEqual((r2[0], r3[0]), (14, 14))
        # reply_drafts constant across runs (a review has at most one draft)
        self.assertEqual(r1[1], r2[1], "drafts grew between run 1 and 2")
        self.assertEqual(r2[1], r3[1], "drafts grew between run 2 and 3")
        # and never duplicated per review
        self.assertEqual(r1[2], 0, "a review ended up with >1 draft")
        self.assertEqual(r2[2], 0)
        self.assertEqual(r3[2], 0)

    def test_repo_writer_is_idempotent_per_review(self):
        service = serve_ui.build_service(self.db)
        repo = service.repo
        rid = repo.conn.execute(
            "SELECT id FROM reviews WHERE reply_text IS NULL LIMIT 1"
        ).fetchone()["id"]
        first = repo.insert_reply_draft(rid, draft_text="real text", status="needs_review")
        second = repo.insert_reply_draft(rid, draft_text="a second", status="draft")
        self.assertEqual(first, second, "a second insert must reuse the existing row")
        rows = repo.get_reply_drafts(rid)
        self.assertEqual(len(rows), 1)
        repo.close()

    def test_seeded_drafts_have_null_text_not_a_placeholder(self):
        service = serve_ui.build_service(self.db)
        repo = service.repo
        _, drafts, _, nulls = _counts(repo)
        # Every seeded draft is genuinely "not generated": draft_text is NULL,
        # and the old non-AI placeholder string is nowhere in the DB.
        self.assertEqual(nulls, drafts)
        rows = repo.conn.execute("SELECT draft_text FROM reply_drafts").fetchall()
        for row in rows:
            self.assertIsNone(row["draft_text"])
            self.assertNotIn("Draft reply pending review.", str(row["draft_text"]))
        repo.close()


class DemoApiHonestyTests(unittest.TestCase):
    """The seeded DB served through the API exposes honest signals."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "demo_api.db")
        cls.service = serve_ui.build_service(cls.db)
        cls.server = create_server(cls.service, port=0)
        cls.host, cls.port = cls.server.server_address[:2]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.service.repo.close()
        cls.tmp.cleanup()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_reviews_api_returns_null_draft_text(self):
        rows = self._get("/api/reviews?page_size=100")["items"]
        drafted = [r for r in rows if r.get("has_draft")]
        self.assertTrue(drafted, "expected at least one seeded draft")
        for r in drafted:
            self.assertIsNone(r["draft_text"])

    def test_review_detail_draft_text_is_null(self):
        rows = self._get("/api/reviews?page_size=100")["items"]
        with_draft = next(r for r in rows if r.get("has_draft") and r.get("draft_id"))
        detail = self._get(f"/api/reviews/{with_draft['review_id']}")
        self.assertTrue(detail.get("drafts"))
        for d in detail["drafts"]:
            self.assertIsNone(d["draft_text"])

    def test_dashboard_pill_signals_are_never_falsely_healthy(self):
        data = self._get("/api/dashboard")
        # No OAuth/sync is configured in this build -> NOT connected, and the
        # app is in mock/demo mode. It must never claim a healthy Google sync.
        self.assertIs(data["google_connected"], False)
        self.assertIs(data["mock_mode"], True)
        self.assertEqual(data["api_health"], "ok")

    def test_web_js_shows_reply_not_generated_and_three_pill_states(self):
        js_path = os.path.join(_ROOT, "web", "app.js")
        with open(js_path, "r", encoding="utf-8") as fh:
            js = fh.read()
        # No false "Sync healthy" text may exist at all.
        self.assertNotIn("Sync healthy", js)
        # The three distinct states are exposed as strings.
        self.assertIn("Google: not connected", js)
        self.assertIn("Google: connected", js)
        self.assertIn("API: ok", js)
        self.assertIn("API: error", js)
        self.assertIn("Demo data", js)
        # And the "not generated" draft state is rendered.
        self.assertIn("Reply not generated", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
