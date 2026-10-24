"""Issue #125 -- demo seeding must stop when live mode is configured.

``scripts/serve_ui.py::build_service`` used to run ``_seed(repo)`` on every
startup, which resurrected the bundled demo fixtures into a real (live) DB on
each restart. These tests pin the fix:

* live mode + a FRESH temp DB  -> ZERO fixture rows (nothing is seeded);
* live mode + a DB that already holds live rows -> row counts unchanged;
* non-live mode -> seeding behaves exactly as today (fixtures created);
* live mode never mints a NEW demo admin user either.

Standard library only, MOCK/offline only, NO network, NO Google. Every test
uses a throwaway SQLite file in a TemporaryDirectory; no real DB is touched.
Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from gbp.repo import Repository  # noqa: E402

import serve_ui  # noqa: E402  (scripts/serve_ui.py)

#: The way production sets live mode: the master switch AND the full OAuth
#: client (the same predicate the rest of the app reads via ``live_enabled``).
LIVE_ENV = {
    "GBP_LIVE": "1",
    "GBP_OAUTH_CLIENT_ID": "cid.apps.googleusercontent.com",
    "GBP_OAUTH_CLIENT_SECRET": "shh-not-real",
    "GBP_OAUTH_REDIRECT_URI": "http://127.0.0.1/oauth/callback",
}


def _table_counts(repo: Repository) -> dict:
    """Row count per demo-seeded table (the fixtures land in all of these)."""
    tables = ("google_accounts", "business_locations", "brand_profiles",
              "reviews", "reply_drafts")
    out = {}
    for table in tables:
        out[table] = int(
            repo.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        )
    return out


class LiveModeSkipsSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "live.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_fresh_db_seeds_zero_fixtures(self):
        with mock.patch.dict(os.environ, LIVE_ENV, clear=False):
            service = serve_ui.build_service(self.db)
        counts = _table_counts(service.repo)
        service.repo.close()
        # On a fresh DB nothing at all is seeded in live mode.
        self.assertEqual(
            counts,
            {t: 0 for t in counts},
            "live mode must not seed any fixture rows",
        )

    def test_live_existing_db_is_unchanged(self):
        # Build a DB that already holds *live* rows (no fixtures involved).
        repo = Repository(self.db)
        repo.init_db(serve_ui.SCHEMA_PATH)
        acct = repo.upsert_account("accounts/live-1", account_name="Live Co",
                                   account_type="PERSONAL")
        loc = repo.upsert_location(acct, "locations/live-1", title="Live Nails",
                                   city="San Jose", state="CA", language="en")
        repo.upsert_review(
            location_id=loc, review_name="accounts/live-1/locations/live-1/reviews/r1",
            review_id="r1", rating=5, comment="Great!", reviewer_name="A. Client",
            create_time="2026-10-23T10:00:00Z", status="unanswered",
        )
        repo.conn.commit()
        before = _table_counts(repo)
        repo.close()

        with mock.patch.dict(os.environ, LIVE_ENV, clear=False):
            service = serve_ui.build_service(self.db)
        after = _table_counts(service.repo)
        service.repo.close()

        self.assertEqual(after, before, "live startup must not change row counts")
        self.assertEqual(after["business_locations"], 1)
        self.assertEqual(after["reviews"], 1)

    def test_non_live_mode_still_seeds(self):
        # No GBP_LIVE in the environment -> byte-for-byte today's behaviour.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GBP_LIVE", None)
            service = serve_ui.build_service(self.db)
        counts = _table_counts(service.repo)
        service.repo.close()
        self.assertEqual(counts["reviews"], 14, "non-live seeding is unchanged")
        self.assertGreater(counts["business_locations"], 0)
        self.assertGreater(counts["google_accounts"], 0)


class LiveModeSkipsDemoAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "auth.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _user_count(self, service):
        return int(service.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    def test_live_mode_creates_no_demo_admin(self):
        with mock.patch.dict(os.environ, LIVE_ENV, clear=False):
            service = serve_ui.build_service(self.db)
            serve_ui.build_auth(service.repo, password="Str0ng-Passw0rd!",
                                allow_ephemeral=True)
            self.assertEqual(self._user_count(service), 0,
                             "live mode must not create a new demo user")
            service.repo.close()

    def test_non_live_mode_creates_demo_admin(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GBP_LIVE", None)
            service = serve_ui.build_service(self.db)
            serve_ui.build_auth(service.repo, password="Str0ng-Passw0rd!",
                                allow_ephemeral=True)
            self.assertEqual(self._user_count(service), 1)
            service.repo.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
