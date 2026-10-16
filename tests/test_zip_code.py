"""Regression tests for ZIP / postal code support (display + storage + search).

Rules pinned here:
  * the ZIP lives in the location model/API as its own field, stored as TEXT so
    leading zeros survive (``07001`` must not become ``7001``);
  * a missing ZIP is ``None`` -> the UI renders an em dash. It is NEVER invented;
  * the bundled demo fixtures carry no ZIP, so every seeded demo location stays
    NULL (no fabricated codes);
  * name and ZIP are NOT identifiers -- ``location_resource_id`` stays the one
    unique id.

All data here is synthetic and lives in an in-memory / temporary SQLite DB.
No network, no Google, no publish. Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from gbp.api.server import create_server  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
APP_JS = os.path.join(_ROOT, "web", "app.js")
INDEX_HTML = os.path.join(_ROOT, "web", "index.html")

# Synthetic test ZIPs (test fixtures only -- never seeded into the demo).
ZIP_A = "92841"
ZIP_LEADING_ZERO = "07001"


class ZipCodeTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc_a = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-001", title="Demo Nails A",
            city="Garden Grove", state="CA", language="en", postal_code=ZIP_A)
        self.loc_b = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-002", title="Demo Nails B",
            city="Westminster", state="CA", language="en",
            postal_code=ZIP_LEADING_ZERO)
        self.loc_c = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-003", title="Demo Nails C",
            city="Reno", state="NV", language="en")   # no ZIP known
        self.repo.conn.executemany(
            "INSERT INTO brand_profiles(location_id, business_name, city,"
            " language, auto_publish_allowed) VALUES (?, ?, ?, ?, ?)",
            [(self.loc_a, "Sunny Nails Spa", "Garden Grove", "en", 1),
             (self.loc_b, "Bright Nails", "Westminster", "en", 0),
             (self.loc_c, "Lakeside Salon", "Reno", "en", 0)],
        )
        self.r1 = self.repo.upsert_review(
            self.loc_a, "reviews/r1", rating=5, comment="Lovely",
            create_time="2026-10-10T10:00:00")
        self.r2 = self.repo.upsert_review(
            self.loc_b, "reviews/r2", rating=2, comment="Not great",
            create_time="2026-10-11T09:00:00")
        self.repo.insert_reply_draft(self.r2, None, status="needs_review")
        self.repo.conn.commit()

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

    # ---------------------------------------------------------------- helpers
    def _get(self, path):
        with urllib.request.urlopen(
                f"http://{self.host}:{self.port}{path}", timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _sql(self, query, args=()):
        return self.repo.conn.execute(query, args).fetchone()[0]

    # ---------------------------------------------------------------- storage
    def test_schema_column_is_text(self):
        cols = {row[1]: row[2] for row in
                self.repo.conn.execute("PRAGMA table_info(business_locations)")}
        self.assertIn("postal_code", cols)
        self.assertEqual(cols["postal_code"].upper(), "TEXT")

    def test_zip_stored_as_string_and_round_trips(self):
        row = self.repo.conn.execute(
            "SELECT postal_code FROM business_locations WHERE id = ?",
            (self.loc_a,)).fetchone()
        self.assertEqual(row["postal_code"], ZIP_A)
        self.assertIsInstance(row["postal_code"], str)

    def test_leading_zero_survives(self):
        row = self.repo.conn.execute(
            "SELECT postal_code FROM business_locations WHERE id = ?",
            (self.loc_b,)).fetchone()
        self.assertEqual(row["postal_code"], ZIP_LEADING_ZERO)   # not "7001"

    def test_numeric_zip_is_coerced_to_string(self):
        acct = self.repo.conn.execute(
            "SELECT account_id FROM business_locations WHERE id = ?",
            (self.loc_c,)).fetchone()["account_id"]
        lid = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-004", title="Numeric",
            postal_code=7001)
        row = self.repo.conn.execute(
            "SELECT postal_code FROM business_locations WHERE id = ?",
            (lid,)).fetchone()
        self.assertEqual(row["postal_code"], "7001")
        self.assertIsInstance(row["postal_code"], str)

    def test_upsert_without_zip_does_not_erase_a_known_one(self):
        acct = self.repo.conn.execute(
            "SELECT account_id FROM business_locations WHERE id = ?",
            (self.loc_a,)).fetchone()["account_id"]
        self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-001", title="Demo Nails A",
            city="Garden Grove", state="CA")          # ZIP omitted
        row = self.repo.conn.execute(
            "SELECT postal_code FROM business_locations WHERE id = ?",
            (self.loc_a,)).fetchone()
        self.assertEqual(row["postal_code"], ZIP_A)

    def test_legacy_database_gets_the_column_additively(self):
        """An existing DB created before the ZIP change must keep its rows."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "legacy.db")
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE business_locations ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER,"
                " location_name TEXT NOT NULL UNIQUE, title TEXT, city TEXT,"
                " state TEXT, language TEXT,"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')));"
                "INSERT INTO business_locations(account_id, location_name, title,"
                " city, state, language) VALUES (1, 'accounts/1001/locations/old',"
                " 'Old Shop', 'Reno', 'NV', 'en');"
            )
            conn.commit()
            conn.close()

            repo = Repository(db)
            repo.init_db(SCHEMA_PATH)
            rows = repo.conn.execute(
                "SELECT title, postal_code FROM business_locations").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], "Old Shop")     # data preserved
            self.assertIsNone(rows[0]["postal_code"])          # unknown, not faked
            repo.close()

    # ---------------------------------------------------------------- demo seed
    def test_bundled_demo_fixtures_carry_no_zip(self):
        with io.open(os.path.join(_ROOT, "fixtures", "locations.json"),
                     encoding="utf-8") as fh:
            data = json.load(fh)
        for loc in data.get("locations", []):
            self.assertNotIn("postalCode", loc)
            self.assertNotIn("postal_code", loc)

    def test_seeded_demo_locations_have_no_invented_zip(self):
        import serve_ui
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "demo.db")
            serve_ui.build_service(db)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT postal_code FROM business_locations").fetchall()
            self.assertTrue(rows)
            for row in rows:
                self.assertIsNone(row["postal_code"],
                                  "demo seed must not fabricate ZIP codes")
            conn.close()

    # ---------------------------------------------------------------- api
    def test_locations_api_exposes_postal_code(self):
        _, data = self._get("/api/locations?page=1&page_size=50")
        by_id = {item["id"]: item for item in data["items"]}
        self.assertEqual(by_id[self.loc_a]["postal_code"], ZIP_A)
        self.assertEqual(by_id[self.loc_b]["postal_code"], ZIP_LEADING_ZERO)
        self.assertIsNone(by_id[self.loc_c]["postal_code"])

    def test_locations_search_by_zip(self):
        _, data = self._get(f"/api/locations?q={ZIP_A}")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["postal_code"], ZIP_A)

    def test_locations_search_still_matches_business_name(self):
        _, data = self._get("/api/locations?q=lakeside")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["postal_code"], None)

    def test_locations_sort_by_postal_code(self):
        _, data = self._get("/api/locations?sort=postal_code&order=desc")
        zips = [i["postal_code"] for i in data["items"] if i["postal_code"]]
        self.assertEqual(zips, sorted(zips, reverse=True))

    def test_location_detail_has_postal_code(self):
        _, data = self._get(f"/api/locations/{self.loc_a}")
        self.assertEqual(data["postal_code"], ZIP_A)

    def test_locations_status_has_postal_code(self):
        _, data = self._get("/api/locations/status")
        rows = data if isinstance(data, list) else data.get("items", [])
        got = {r["location_id"]: r["postal_code"] for r in rows}
        self.assertEqual(got[self.loc_a], ZIP_A)
        self.assertIsNone(got[self.loc_c])

    def test_reviews_api_items_have_postal_code(self):
        _, data = self._get("/api/reviews?page=1&page_size=50")
        self.assertEqual(data["total"], 2)
        for item in data["items"]:
            self.assertIn("postal_code", item)
            # name / ZIP are not ids -- the resource id stays the unique one
            self.assertIn("location_resource_id", item)
        zips = sorted(i["postal_code"] for i in data["items"])
        self.assertEqual(zips, sorted([ZIP_A, ZIP_LEADING_ZERO]))

    def test_reviews_search_by_zip(self):
        _, data = self._get(f"/api/reviews?q={ZIP_LEADING_ZERO}")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["postal_code"], ZIP_LEADING_ZERO)

    def test_review_detail_has_postal_code(self):
        _, data = self._get(f"/api/reviews/{self.r1}")
        self.assertEqual(data["postal_code"], ZIP_A)

    def test_dashboard_recent_and_attention_have_postal_code(self):
        _, data = self._get("/api/dashboard")
        for item in data["recent_reviews"]:
            self.assertIn("postal_code", item)
        for item in data["attention_locations"]:
            self.assertIn("postal_code", item)
        # loc_a has an unanswered review -> it must appear, with its ZIP
        ids = {i["location_id"]: i for i in data["attention_locations"]}
        self.assertEqual(ids[self.loc_a]["postal_code"], ZIP_A)

    # ---------------------------------------------------------------- ui
    def test_ui_renders_zip_label_and_dash_fallback(self):
        with io.open(APP_JS, encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn('zipCode: "ZIP Code"', js)
        self.assertIn("function zipLabel(", js)
        self.assertIn("\\u2014", js)              # em dash fallback in source
        self.assertNotIn('zipLabel(value) === null', js)
        # rendered in every area that shows a shop name
        self.assertIn('cls: "zip"', js)           # inbox rows
        self.assertIn('t("locations.zipCode")', js)

    def test_ui_zip_places(self):
        with io.open(APP_JS, encoding="utf-8") as fh:
            js = fh.read()
        # Overview attention + recent reviews + sync table, Locations table,
        # inbox detail -> each carries the ZIP label.
        self.assertGreaterEqual(js.count('t("locations.zipCode")'), 5)
        # Locations search advertises ZIP
        with io.open(INDEX_HTML, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("ZIP", html)


if __name__ == "__main__":
    unittest.main()
