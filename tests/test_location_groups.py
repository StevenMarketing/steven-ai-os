"""Issue #112 -- group-aware locations: projection, API fields, filter compose.

A GBP *business group* is an account of type ``LOCATION_GROUP``. These tests are
fully offline (stdlib + mock SQLite only, loopback server at most):

* the projection rule (a group keeps its name; a primary/personal account is
  reported as ``Ungrouped``);
* the demo fixture carries a SECOND group so grouping is visible without live
  data;
* ``GET /api/locations`` and ``GET /api/locations/status`` carry
  ``group_id`` / ``group_name`` / ``group_type`` and the per-group counts;
* the group filter narrows the list exactly and COMPOSES with the search and
  sync-status filters, with the counts staying visible;
* connection rows expose the same group fields;
* a source-level check that the Locations UI sends and honours the filter.

Run from the project root::

    PYTHONPATH=src python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import unittest
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp import fixtures  # noqa: E402
from gbp.api.server import create_server  # noqa: E402
from gbp.groups import UNGROUPED, group_fields, group_summary, is_group_account  # noqa: E402
from gbp.oauth.flow import connection_rows  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.sync.live_sync import map_account, map_location  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
APP_JS = os.path.join(_ROOT, "web", "app.js")
INDEX_HTML = os.path.join(_ROOT, "web", "index.html")

GROUP_ACCOUNT = "accounts/2002"
PRIMARY_ACCOUNT = "accounts/1001"


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


# ===================================================================== #
# projection
# ===================================================================== #
class GroupProjectionTests(unittest.TestCase):
    def test_location_group_is_a_group(self):
        self.assertTrue(is_group_account("LOCATION_GROUP"))
        self.assertTrue(is_group_account(" location_group "))
        self.assertFalse(is_group_account("PERSONAL"))
        self.assertFalse(is_group_account(None))

    def test_group_account_keeps_its_name(self):
        fields = group_fields(GROUP_ACCOUNT, "ADZDigital Clients", "LOCATION_GROUP")
        self.assertEqual(fields, {"group_id": GROUP_ACCOUNT,
                                  "group_name": "ADZDigital Clients",
                                  "group_type": "LOCATION_GROUP"})

    def test_primary_account_is_ungrouped(self):
        fields = group_fields(PRIMARY_ACCOUNT, "Demo Nail Group", "PERSONAL")
        self.assertEqual(fields["group_name"], UNGROUPED)
        self.assertEqual(fields["group_id"], PRIMARY_ACCOUNT)
        self.assertEqual(fields["group_type"], "PERSONAL")

    def test_unknown_type_is_ungrouped_and_named_group_without_name(self):
        self.assertEqual(group_fields("accounts/9", "No Name", None)["group_name"],
                         UNGROUPED)
        # A LOCATION_GROUP with a blank display name still gets a label.
        self.assertEqual(
            group_fields("accounts/9", "", "LOCATION_GROUP")["group_name"], UNGROUPED)

    def test_map_account_carries_group_fields(self):
        row = map_account({"name": GROUP_ACCOUNT, "accountName": "ADZDigital Clients",
                           "type": "LOCATION_GROUP"}, tenant="t1")
        self.assertEqual(row["group_id"], GROUP_ACCOUNT)
        self.assertEqual(row["group_name"], "ADZDigital Clients")
        self.assertEqual(row["group_type"], "LOCATION_GROUP")

    def test_map_location_uses_the_account_group(self):
        loc = {"name": GROUP_ACCOUNT + "/locations/loc-101", "title": "ADZ Boston",
               "storefrontAddress": {"postalCode": "02108", "locality": "Boston"}}
        row = map_location(loc, tenant="t1", account=GROUP_ACCOUNT,
                           account_name="ADZDigital Clients",
                           account_type="LOCATION_GROUP")
        self.assertEqual(row["group_name"], "ADZDigital Clients")
        self.assertEqual(row["group_id"], GROUP_ACCOUNT)
        # The pre-existing location fields are untouched.
        self.assertEqual(row["business_name"], "ADZ Boston")
        self.assertEqual(row["zip"], "02108")

    def test_map_location_without_account_type_is_ungrouped(self):
        row = map_location({"name": "accounts/9/locations/x"}, tenant="t1",
                           account="accounts/9")
        self.assertEqual(row["group_name"], UNGROUPED)

    def test_group_summary_counts_and_orders_ungrouped_last(self):
        rows = [
            {"group_id": GROUP_ACCOUNT, "group_name": "ADZDigital Clients",
             "group_type": "LOCATION_GROUP"},
            {"group_id": GROUP_ACCOUNT, "group_name": "ADZDigital Clients",
             "group_type": "LOCATION_GROUP"},
            {"group_id": PRIMARY_ACCOUNT, "group_name": UNGROUPED,
             "group_type": "PERSONAL"},
        ]
        summary = group_summary(rows)
        self.assertEqual([g["group_name"] for g in summary],
                         ["ADZDigital Clients", UNGROUPED])
        self.assertEqual([g["count"] for g in summary], [2, 1])

    def test_group_summary_counts_ungrouped_when_fields_missing(self):
        # A row with no group fields is never dropped -- it is Ungrouped.
        summary = group_summary([{"id": 1}, {"id": 2}])
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["group_name"], UNGROUPED)
        self.assertEqual(summary[0]["count"], 2)


# ===================================================================== #
# demo fixture
# ===================================================================== #
class GroupFixtureTests(unittest.TestCase):
    def test_fixture_defines_a_second_group_with_locations(self):
        groups = fixtures.load_location_groups()
        self.assertTrue(groups, "expected at least one extra business group")
        names = {g["name"] for g in groups}
        self.assertIn(GROUP_ACCOUNT, names)
        for group in groups:
            self.assertEqual(group["type"], "LOCATION_GROUP")
            self.assertTrue(group["accountName"])
            self.assertTrue(group["locations"])
            for loc in group["locations"]:
                self.assertTrue(loc["name"].startswith(group["name"] + "/locations/"))

    def test_primary_fixture_stays_a_single_ungrouped_account(self):
        # The extra group lives in its own fixture, so the primary account
        # fixtures -- and the tests that assert their shape -- are unchanged.
        accounts = fixtures.list_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["name"], PRIMARY_ACCOUNT)
        self.assertEqual(accounts[0]["type"], "PERSONAL")

    def test_load_location_groups_tolerates_a_missing_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(fixtures.load_location_groups(tmp), [])


# ===================================================================== #
# repository migration
# ===================================================================== #
class AccountMetadataTests(unittest.TestCase):
    def test_metadata_round_trips_and_is_additive(self):
        repo = Repository(":memory:")
        repo.init_db(SCHEMA_PATH)
        acct = repo.upsert_account(PRIMARY_ACCOUNT, account_name="Demo Nail Group",
                                   account_type="PERSONAL")
        # A later call without metadata must not erase it.
        repo.upsert_account(PRIMARY_ACCOUNT)
        meta = repo.account_metadata()
        self.assertEqual(meta[acct]["account_name"], "Demo Nail Group")
        self.assertEqual(meta[acct]["account_type"], "PERSONAL")
        repo.close()

    def test_account_metadata_on_a_pre_112_database(self):
        """A DB without the two columns reports them as None, never crashing."""
        repo = Repository(":memory:")
        # The pre-Issue-#112 table shape: no account_name / account_type.
        repo.conn.execute(
            "CREATE TABLE google_accounts ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT NOT NULL UNIQUE,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        repo.conn.execute("INSERT INTO google_accounts(name) VALUES (?)",
                          (PRIMARY_ACCOUNT,))
        repo.conn.commit()
        meta = repo.account_metadata()
        self.assertEqual(len(meta), 1)
        row = next(iter(meta.values()))
        self.assertIsNone(row["account_name"])
        self.assertIsNone(row["account_type"])
        repo.close()

    def test_init_db_upgrades_a_pre_112_database_in_place(self):
        repo = Repository(":memory:")
        repo.conn.execute(
            "CREATE TABLE google_accounts ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT NOT NULL UNIQUE,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        repo.conn.commit()
        repo.init_db(SCHEMA_PATH)
        cols = {r[1] for r in repo.conn.execute(
            "PRAGMA table_info(google_accounts)")}
        self.assertIn("account_name", cols)
        self.assertIn("account_type", cols)
        repo.close()


# ===================================================================== #
# API
# ===================================================================== #
class GroupApiTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        primary = self.repo.upsert_account(PRIMARY_ACCOUNT,
                                           account_name="Demo Nail Group",
                                           account_type="PERSONAL")
        group = self.repo.upsert_account(GROUP_ACCOUNT,
                                         account_name="ADZDigital Clients",
                                         account_type="LOCATION_GROUP")
        self.loc_u1 = self.repo.upsert_location(
            primary, PRIMARY_ACCOUNT + "/locations/loc-001", title="Demo Nails A",
            city="Garden Grove", state="CA", language="en")
        self.loc_u2 = self.repo.upsert_location(
            primary, PRIMARY_ACCOUNT + "/locations/loc-002", title="Demo Nails B",
            city="San Jose", state="CA", language="en")
        self.loc_g1 = self.repo.upsert_location(
            group, GROUP_ACCOUNT + "/locations/loc-101", title="ADZ Nails Boston",
            city="Boston", state="MA", language="en")
        self.loc_g2 = self.repo.upsert_location(
            group, GROUP_ACCOUNT + "/locations/loc-102", title="ADZ Nails Austin",
            city="Austin", state="TX", language="vi")

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

    def test_envelope_reports_two_groups_with_counts(self):
        _, env = self._get("/api/locations?page=1&page_size=100")
        by_name = {g["group_name"]: g for g in env["groups"]}
        self.assertEqual(set(by_name), {"ADZDigital Clients", UNGROUPED})
        self.assertEqual(by_name["ADZDigital Clients"]["count"], 2)
        self.assertEqual(by_name[UNGROUPED]["count"], 2)
        self.assertEqual(by_name["ADZDigital Clients"]["group_id"], GROUP_ACCOUNT)
        self.assertEqual(by_name["ADZDigital Clients"]["group_type"], "LOCATION_GROUP")

    def test_every_item_carries_the_group_fields(self):
        _, env = self._get("/api/locations?page=1&page_size=100")
        for item in env["items"]:
            for key in ("group_id", "group_name", "group_type"):
                self.assertIn(key, item)
        groups = {i["id"]: i["group_name"] for i in env["items"]}
        self.assertEqual(groups[self.loc_g1], "ADZDigital Clients")
        self.assertEqual(groups[self.loc_u1], UNGROUPED)

    def test_group_filter_narrows_exactly(self):
        _, env = self._get(f"/api/locations?page=1&page_size=100&group={GROUP_ACCOUNT}")
        self.assertEqual(env["total"], 2)
        self.assertEqual(sorted(i["id"] for i in env["items"]),
                         sorted([self.loc_g1, self.loc_g2]))
        # Counts stay visible for every group even while one is selected.
        self.assertEqual({g["group_name"] for g in env["groups"]},
                         {"ADZDigital Clients", UNGROUPED})

    def test_group_filter_composes_with_search(self):
        _, env = self._get(
            f"/api/locations?page=1&page_size=100&group={GROUP_ACCOUNT}&q=austin")
        self.assertEqual(env["total"], 1)
        self.assertEqual(env["items"][0]["id"], self.loc_g2)
        # The search narrows the group counts too (Austin only exists in ADZ).
        counts = {g["group_name"]: g["count"] for g in env["groups"]}
        self.assertEqual(counts, {"ADZDigital Clients": 1})

    def test_group_filter_composes_with_state(self):
        _, env = self._get(
            f"/api/locations?page=1&page_size=100&group={GROUP_ACCOUNT}&state=MA")
        self.assertEqual(env["total"], 1)
        self.assertEqual(env["items"][0]["id"], self.loc_g1)

    def test_ungrouped_filter_selects_only_primary_locations(self):
        _, env = self._get(f"/api/locations?page=1&page_size=100&group={PRIMARY_ACCOUNT}")
        self.assertEqual(env["total"], 2)
        self.assertEqual(sorted(i["id"] for i in env["items"]),
                         sorted([self.loc_u1, self.loc_u2]))

    def test_status_rows_carry_group_fields(self):
        _, rows = self._get("/api/locations/status")
        by_id = {r["location_id"]: r for r in rows}
        self.assertEqual(by_id[self.loc_g1]["group_name"], "ADZDigital Clients")
        self.assertEqual(by_id[self.loc_u1]["group_name"], UNGROUPED)
        self.assertEqual(by_id[self.loc_g1]["group_type"], "LOCATION_GROUP")

    def test_dashboard_exposes_the_group_summary(self):
        _, dash = self._get("/api/dashboard")
        by_name = {g["group_name"]: g["count"] for g in dash["location_groups"]}
        self.assertEqual(by_name, {"ADZDigital Clients": 2, UNGROUPED: 2})

    def test_bare_locations_call_keeps_the_legacy_list_shape(self):
        _, rows = self._get("/api/locations")
        self.assertIsInstance(rows, list)
        self.assertTrue(all("group_id" in r for r in rows))


# ===================================================================== #
# connection rows reuse the same group fields
# ===================================================================== #
class ConnectionGroupTests(unittest.TestCase):
    def test_connection_rows_expose_group_fields(self):
        businesses = [{
            "location_id": 1,
            "location_name": GROUP_ACCOUNT + "/locations/loc-101",
            "business_name": "ADZ Nails Boston",
            "group_id": GROUP_ACCOUNT,
            "group_name": "ADZDigital Clients",
            "group_type": "LOCATION_GROUP",
        }]

        class _Vault:
            def connection_state(self, tenant, now=None):
                return "not_connected"

            def meta(self, tenant):
                return None

        rows = connection_rows(businesses, _Vault(), now=1000.0)
        self.assertEqual(rows[0]["group_id"], GROUP_ACCOUNT)
        self.assertEqual(rows[0]["group_name"], "ADZDigital Clients")
        self.assertEqual(rows[0]["group_type"], "LOCATION_GROUP")


# ===================================================================== #
# UI wiring (source-level, no browser)
# ===================================================================== #
class GroupUiSourceTests(unittest.TestCase):
    def setUp(self):
        self.js = _read(APP_JS)
        self.html = _read(INDEX_HTML)

    def test_index_has_a_group_filter_control(self):
        self.assertIn('id="loc-group"', self.html)
        self.assertIn('name="group"', self.html)
        self.assertIn("All groups", self.html)

    def test_js_sends_the_group_filter_and_keeps_counts_visible(self):
        self.assertIn('q.set("group", locState.group)', self.js)
        self.assertIn("function populateGroupFilter(groups)", self.js)
        self.assertIn("function renderGroupsSummary(groups)", self.js)
        # counts are rendered into the option labels / summary, not hidden
        self.assertIn('t("locations.allGroups")', self.js)
        self.assertIn("groupLabel", self.js)
        self.assertIn('cls: "group-heading"', self.js)
        self.assertIn('id="loc-groups-summary"', self.html)

    def test_group_filter_applies_immediately_and_resets(self):
        self.assertIn('$("loc-group").addEventListener("change"', self.js)
        # the search handler reads the current group selection too
        self.assertIn('if ($("loc-group")) locState.group = $("loc-group").value || "";',
                      self.js)
        # reset clears the group filter
        self.assertIn('locState.group = "";', self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
