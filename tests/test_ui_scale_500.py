"""Group 7 / T21 -- UI scale: page through 500 locations without loading all.

FAST (<5s), standard library only, NO network, NO Google, NO AI. Seeds 500
locations + ~2000 reviews into a TEMPORARY SQLite file (never a real DB), then
serves it over a loopback server and asserts the SERVER-SIDE pagination works:
page 2 of size 25 returns exactly 25 rows with total==500, a name search
narrows correctly, and a single page fetch stays under ~250ms.

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import create_server  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")

LOCATIONS = 500
REVIEWS_PER_LOCATION = 4  # on every 4th location -> 2000 reviews
PAGE_FETCH_BUDGET_S = 0.25


def _seed_scale_db(path: str) -> None:
    repo = Repository(path)
    repo.init_db(SCHEMA_PATH)
    acct = repo.upsert_account("accounts/scale")
    loc_rows = []
    review_rows = []
    for i in range(LOCATIONS):
        # title carries the human name the UI searches on
        loc_rows.append((
            acct, f"accounts/scale/locations/loc-{i:04d}",
            f"Salon {i:03d}", "Garden Grove", "CA", "vi" if i % 2 else "en",
        ))
    repo.conn.executemany(
        "INSERT INTO business_locations"
        " (account_id, location_name, title, city, state, language)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        loc_rows,
    )
    # location ids are 1..LOCATIONS in insertion order
    for i in range(LOCATIONS):
        loc_id = i + 1
        stamp = "2026-01-%02dT08:00:00Z" % ((i % 28) + 1)
        for j in range(REVIEWS_PER_LOCATION):
            review_rows.append((
                loc_id,
                f"accounts/scale/locations/loc-{i:04d}/reviews/r-{j}",
                f"r-{i}-{j}",
                (i + j) % 5 + 1,
                f"Comment {j} for salon {i}",
                f"user-{j}",
                stamp, stamp, "unanswered",
            ))
    repo.conn.executemany(
        "INSERT INTO reviews"
        " (location_id, review_name, review_id, rating, comment, reviewer_name,"
        "  create_time, update_time, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        review_rows,
    )
    repo.conn.commit()
    repo.close()


class UiScale500Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "scale.db")
        _seed_scale_db(cls.db)
        cls.repo = Repository(cls.db)
        cls.service = ApprovalService(cls.repo)
        cls.server = create_server(cls.service, port=0)
        cls.host, cls.port = cls.server.server_address[:2]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.repo.close()
        cls.tmp.cleanup()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_dataset_seeded(self):
        n = self.repo.conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]
        self.assertEqual(n, 2000)

    def test_page_two_returns_25_of_500(self):
        data = self._get("/api/locations?page=2&page_size=25")
        self.assertEqual(data["total"], 500)
        self.assertEqual(len(data["items"]), 25)
        self.assertEqual(data["page"], 2)
        self.assertEqual(data["page_size"], 25)
        self.assertEqual(data["pages"], 20)

    def test_name_search_narrows(self):
        data = self._get("/api/locations?q=Salon%2001&page_size=100")
        # "Salon 01x" -> exactly 10 locations (010..019)
        self.assertEqual(data["total"], 10)
        for item in data["items"]:
            self.assertIn("salon 01", str(item["business_name"]).lower())

    def test_page_fetch_under_budget(self):
        # Warm the connection/serializer once, then measure a real page fetch.
        self._get("/api/locations?page=1&page_size=25")
        start = time.perf_counter()
        data = self._get("/api/locations?page=2&page_size=25")
        elapsed = time.perf_counter() - start
        self.assertEqual(len(data["items"]), 25)
        self.assertLess(
            elapsed, PAGE_FETCH_BUDGET_S,
            f"page fetch took {elapsed*1000:.1f}ms (budget {PAGE_FETCH_BUDGET_S*1000:.0f}ms)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
