"""Dashboard V2 UX-pass tests: Sync Status labels, relative Last Sync, the slim
Locations table, the per-shop detail panel and the read-only sync ledger.

Nothing here fabricates data: the sync ledger comes from ``sync_run_log`` and an
empty ledger must be reported as empty. Standard library only, mock data only,
no network beyond the loopback test server.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import unittest
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import create_server  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.sync.state import SyncState  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
APP_JS = os.path.join(_ROOT, "web", "app.js")
STYLES = os.path.join(_ROOT, "web", "styles.css")

PRIORITY_COLUMNS = ["businessName", "cityState", "zipCode", "unanswered",
                    "syncStatus", "lastSync"]
REMOVED_COLUMNS = ["locations.reviews", "locations.needsApproval",
                   "locations.brandProfile", "locations.autoPublish",
                   "locations.actions"]


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def _js_function_body(source, signature):
    """Return the body of a top-level ``function name(...) { ... }`` block."""
    start = source.index(signature)
    depth = 0
    i = source.index("{", start)
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[i:j + 1]
    raise AssertionError(f"unterminated function: {signature}")


class LocationUxApiTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-001", title="Demo Nails A",
            city="Garden Grove", state="CA", language="en", postal_code="92841")
        self.empty_loc = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-002", title="Demo Nails B",
            city="Reno", state="NV", language="en")
        sync = SyncState(self.repo)
        sync.record_run("run-old", str(self.loc), "failed", errors=2,
                        started_at="2026-10-14T10:00:00",
                        ended_at="2026-10-14T10:01:00")
        sync.record_run("run-new", str(self.loc), "ok", fetched=4, upserted=4,
                        started_at="2026-10-15T09:00:00",
                        ended_at="2026-10-15T09:01:00")
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

    def _get(self, path):
        with urllib.request.urlopen(
                f"http://{self.host}:{self.port}{path}", timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_sync_runs_newest_first(self):
        _, data = self._get(f"/api/locations/{self.loc}/sync-runs")
        self.assertEqual(data["count"], 2)
        self.assertEqual([r["run_key"] for r in data["runs"]], ["run-new", "run-old"])
        self.assertEqual(data["runs"][0]["status"], "ok")
        self.assertEqual(data["runs"][0]["fetched"], 4)

    def test_sync_runs_respects_limit(self):
        _, data = self._get(f"/api/locations/{self.loc}/sync-runs?limit=1")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["runs"][0]["run_key"], "run-new")

    def test_sync_runs_empty_ledger_is_empty_not_fabricated(self):
        _, data = self._get(f"/api/locations/{self.empty_loc}/sync-runs")
        self.assertEqual(data["runs"], [])
        self.assertEqual(data["count"], 0)

    def test_sync_runs_unknown_location_404(self):
        try:
            self._get("/api/locations/9999/sync-runs")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)
            return
        self.fail("expected 404")

    def test_location_detail_still_exposes_every_field(self):
        """Trimming UI columns must not remove API data."""
        _, loc = self._get(f"/api/locations/{self.loc}")
        for key in ("total_reviews", "unanswered", "needs_approval",
                    "brand_profile_configured", "auto_publish_policy",
                    "sync_status", "last_sync", "postal_code"):
            self.assertIn(key, loc)


class LocationUxSourceTests(unittest.TestCase):
    """Source-level checks for the display rules (no browser needed)."""

    def setUp(self):
        self.js = _read(APP_JS)

    def test_locations_table_has_only_priority_columns_in_order(self):
        body = _js_function_body(self.js, "function locationsTable(rows)")
        labels = re.findall(r't\("locations\.(\w+)"\)', body)
        head = labels[:len(PRIORITY_COLUMNS)]
        self.assertEqual(head, PRIORITY_COLUMNS)
        for gone in REMOVED_COLUMNS:
            self.assertNotIn(gone, body, f"{gone} should live in the detail panel")

    def test_business_name_opens_the_detail_panel(self):
        body = _js_function_body(self.js, "function locationsTable(rows)")
        self.assertIn('cls: "link"', body)
        self.assertIn("openLocation(loc.id)", body)

    def test_detail_panel_keeps_the_hidden_data_and_adds_zip(self):
        body = _js_function_body(self.js, "function openLocation(id)")
        self.assertIn('t("locations.zipCode"), loc.postal_code', body)
        for kept in ("Reviews", "Unanswered", "Needs approval",
                     "Brand profile", "Auto-publish policy"):
            self.assertIn(kept, body)

    def test_sync_status_is_mapped_never_assumed(self):
        self.assertIn("SYNC_STATUS_LABELS", self.js)
        self.assertIn('ok: "sync.synced"', self.js)
        self.assertIn('partial: "sync.partial"', self.js)
        self.assertIn('failed: "sync.failed"', self.js)
        self.assertIn('running: "sync.syncing"', self.js)
        self.assertIn('return t("sync.notTracked");', self.js)
        # unknown values are echoed, not upgraded to "Synced"
        self.assertIn("return key ? t(key) : raw;", self.js)

    def test_last_sync_is_relative_with_full_time_available(self):
        body = _js_function_body(self.js, "function relTime(value, absolute)")
        for piece in ('"m ago"', '"h ago"', '"d ago"', "just now"):
            self.assertIn(piece, body)
        self.assertIn("isNaN(ms)) return raw", body)      # never invents a time
        self.assertIn('title: fullTime(loc.last_sync)', self.js)   # hover = full
        self.assertIn('relTime(loc.last_sync, true)', self.js)     # detail = full

    def test_never_synced_states(self):
        self.assertIn('never: "Never"', self.js)
        self.assertIn('notTracked: "Not tracked"', self.js)

    def test_sync_history_has_an_empty_state(self):
        self.assertIn('noSyncRuns: "No sync runs recorded."', self.js)
        self.assertIn('renderEmpty(histWrap, t("locations.noSyncRuns"))', self.js)
        self.assertIn("/sync-runs?limit=20", self.js)

    def test_zip_still_rendered_with_dash_fallback(self):
        self.assertIn("function zipLabel(", self.js)
        self.assertIn('return v ? v : "\\u2014";', self.js)

    def test_css_keeps_desktop_columns_on_one_line(self):
        css = _read(STYLES)
        self.assertIn("table.grid.locations-table th", css)
        self.assertIn("white-space: nowrap", css)
        self.assertIn("button.link", css)


if __name__ == "__main__":
    unittest.main()
