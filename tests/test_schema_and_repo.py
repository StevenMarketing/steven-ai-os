"""Tests for the SQLite schema and the repository layer.

Standard library only. Uses an in-memory SQLite database.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest

# Make ``gbp`` importable from src/ without an install step.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.repo import Repository  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")

EXPECTED_TABLES = {
    "google_accounts",
    "business_locations",
    "brand_profiles",
    "reviews",
    "reply_drafts",
    "approval_records",
    "sync_runs",
    "audit_logs",
    "api_usage",
}


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)

    def tearDown(self):
        self.repo.close()

    def test_schema_creates_all_tables(self):
        rows = self.repo.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        names = {r["name"] for r in rows}
        missing = EXPECTED_TABLES - names
        self.assertEqual(missing, set(), f"missing tables: {missing}")

    def test_expected_indexes_exist(self):
        rows = self.repo.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
        names = {r["name"] for r in rows}
        for idx in (
            "idx_reviews_location_id",
            "idx_reviews_status",
            "idx_reviews_update_time",
        ):
            self.assertIn(idx, names)

    def test_unique_constraint_on_review_name_enforced(self):
        loc_id = self._seed_location()
        self.repo.conn.execute(
            "INSERT INTO reviews (location_id, review_name) VALUES (?, ?)",
            (loc_id, "reviews/abc"),
        )
        self.repo.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.conn.execute(
                "INSERT INTO reviews (location_id, review_name) VALUES (?, ?)",
                (loc_id, "reviews/abc"),
            )

    # helper
    def _seed_location(self) -> int:
        acct = self.repo.upsert_account("accounts/1")
        return self.repo.upsert_location(acct, "locations/100", title="Salon")


class RepoUpsertTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1")
        self.loc_id = self.repo.upsert_location(acct, "locations/100", title="Salon")

    def tearDown(self):
        self.repo.close()

    def test_upsert_review_twice_keeps_one_row_and_refreshes_last_seen(self):
        rid1 = self.repo.upsert_review(
            self.loc_id, "reviews/abc", rating=5, comment="Great!",
            status="new",
        )
        first = self.repo.conn.execute(
            "SELECT * FROM reviews WHERE review_name = ?", ("reviews/abc",)
        ).fetchone()
        # Force a distinguishable first_seen/last_seen gap to prove the refresh.
        self.repo.conn.execute(
            "UPDATE reviews SET last_seen_at = '2000-01-01 00:00:00' "
            "WHERE review_name = ?",
            ("reviews/abc",),
        )
        self.repo.conn.commit()

        rid2 = self.repo.upsert_review(
            self.loc_id, "reviews/abc", rating=5, comment="Great!", status="new",
        )
        self.assertEqual(rid1, rid2)

        rows = self.repo.conn.execute(
            "SELECT * FROM reviews WHERE review_name = ?", ("reviews/abc",)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertNotEqual(row["last_seen_at"], "2000-01-01 00:00:00")
        self.assertEqual(row["first_seen_at"], first["first_seen_at"])

    def test_upsert_review_updates_changed_comment(self):
        self.repo.upsert_review(
            self.loc_id, "reviews/abc", comment="old comment", status="new",
        )
        self.repo.upsert_review(
            self.loc_id, "reviews/abc", comment="new comment", status="new",
        )
        rows = self.repo.conn.execute(
            "SELECT * FROM reviews WHERE review_name = ?", ("reviews/abc",)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["comment"], "new comment")

    def test_count_unanswered_counts_null_reply(self):
        self.repo.upsert_review(self.loc_id, "reviews/1", comment="a")
        self.repo.upsert_review(self.loc_id, "reviews/2", comment="b")
        self.repo.upsert_review(
            self.loc_id, "reviews/3", comment="c", reply_text="thanks",
        )
        self.assertEqual(self.repo.count_unanswered(), 2)

    def test_get_reviews_filters_by_status(self):
        self.repo.upsert_review(self.loc_id, "reviews/1", status="new")
        self.repo.upsert_review(self.loc_id, "reviews/2", status="replied")
        self.repo.upsert_review(self.loc_id, "reviews/3", status="new")
        all_reviews = self.repo.get_reviews()
        self.assertEqual(len(all_reviews), 3)
        new_only = self.repo.get_reviews(status="new")
        self.assertEqual(len(new_only), 2)
        self.assertTrue(all(r["status"] == "new" for r in new_only))


if __name__ == "__main__":
    unittest.main(verbosity=2)