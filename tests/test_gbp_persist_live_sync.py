"""Issue #117 -- persist the live sync (accounts/locations) into the dashboard.

Covers the increment that turns a successful connect into REAL dashboard data:

* :func:`gbp.sync.persist.persist_bundle` -- idempotent, tenant-scoped upserts of
  accounts (``google_accounts``) and locations (``business_locations``) keyed on
  the Google resource name, with the ``gbp.groups`` projection intact;
* a location Google no longer returns is MARKED (``sync_status = 'removed'``);
* ``POST /api/google/sync`` -- session-scoped, live-gated, read-only, returns the
  persisted counts and records the outcome;
* the OAuth callback PERSISTS the bundle it used to discard;
* every attempt records ``sync_status`` / ``last_sync_at`` (or a safe reason) on
  the tenant's vault meta;
* the header chip is derived from the real vault state in live mode.

Fully OFFLINE: the token endpoint and the GBP API are ``MockTransport`` objects
(no socket is opened beyond the loopback test server), no credential is real and
``GBP_LIVE`` is only ever set inside a patched environment.

Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests
"""

from __future__ import annotations

import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import _google_connected, _locations_status, create_server  # noqa: E402
from gbp.auth.token_manager import Token  # noqa: E402
from gbp.authz import AuthService, UserStore, hash_password  # noqa: E402
from gbp.client.http_client import MockTransport  # noqa: E402
from gbp.groups import group_fields  # noqa: E402
from gbp.oauth.config import LIVE_ENV  # noqa: E402
from gbp.oauth.flow import connection_rows  # noqa: E402
from gbp.oauth.vault import STATE_CONNECTED, TOKEN_URL, TokenVault  # noqa: E402
from gbp.oauth.wiring import GoogleConnectWiring  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.sync.persist import persist_bundle, record_outcome, safe_reason  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
TEST_SECRET = "gbp-persist-live-sync-secret-not-used-elsewhere"
TEST_ENV = {"GBP_SESSION_SECRET": TEST_SECRET}
VAULT_KEY = "unit-test-vault-key-material-0123456789abcdef"
CANONICAL_REDIRECT = "https://gbp.adzdigital.com/api/google/callback"

LOC_A = "accounts/1001/locations/loc-001"
LOC_B = "accounts/1001/locations/loc-002"
LIVE_A = "accounts/1001/locations/777001"
LIVE_B = "accounts/2002/locations/888002"

PASSWORD = "P3rsistSecret!"
ACCEPT = "application/json"


def full_env(vault_dir: str, **over: str) -> dict:
    env = {
        LIVE_ENV: "1",
        "GBP_OAUTH_CLIENT_ID": "client-id-probe",
        "GBP_OAUTH_CLIENT_SECRET": "client-secret-probe",
        "GBP_OAUTH_REDIRECT_URI": CANONICAL_REDIRECT,
        "GBP_TOKEN_ENCRYPTION_KEY": VAULT_KEY,
        "GBP_TOKEN_VAULT_DIR": vault_dir,
    }
    env.update(over)
    return env


def token_transport() -> MockTransport:
    transport = MockTransport()
    transport.register("POST", TOKEN_URL, {
        "access_token": "access-token-probe",
        "refresh_token": "refresh-token-probe",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/business.manage",
        "token_type": "Bearer",
    }, status=200)
    return transport


def sync_transport(accounts=None, locations=None) -> MockTransport:
    """A fake GBP API: one LOCATION_GROUP account with two locations + one
    personal account with one location, so the group projection is exercised."""
    accounts = accounts if accounts is not None else [
        {"name": "accounts/1001", "accountName": "Acme Group",
         "type": "LOCATION_GROUP"},
        {"name": "accounts/2002", "accountName": "Personal",
         "type": "PERSONAL"},
    ]
    locations = locations if locations is not None else {
        "accounts/1001": [
            {"name": LIVE_A, "title": "Live Nails A",
             "storefrontAddress": {"postalCode": "94103", "locality": "Garden Grove",
                                   "administrativeArea": "CA"}},
        ],
        "accounts/2002": [
            {"name": LIVE_B, "title": "Live Nails B",
             "storefrontAddress": {"postalCode": "07001", "locality": "Avenel",
                                   "administrativeArea": "NJ"}},
        ],
    }
    transport = MockTransport()
    transport.register(
        "GET", "https://mybusiness.googleapis.com/v1/accounts",
        {"accounts": accounts})
    for account, rows in locations.items():
        transport.register(
            "GET",
            "https://mybusiness.googleapis.com/v1/%s/locations" % account,
            {"locations": rows})
    return transport


def bundle(accounts=None, locations=None, status="ok", last_sync=None) -> dict:
    """A sync bundle in the exact shape ``LiveLocationSync.sync()`` returns."""
    stamp = last_sync or "2026-10-24T01:00:00Z"
    accounts = accounts if accounts is not None else [
        {"tenant_id": LOC_A, "account_id": "accounts/1001",
         "account_name": "Acme Group", "type": "LOCATION_GROUP",
         **group_fields("accounts/1001", "Acme Group", "LOCATION_GROUP")},
        {"tenant_id": LOC_A, "account_id": "accounts/2002",
         "account_name": "Personal", "type": "PERSONAL",
         **group_fields("accounts/2002", "Personal", "PERSONAL")},
    ]
    locations = locations if locations is not None else [
        {"tenant_id": LOC_A, "location_id": LIVE_A, "account_id": "accounts/1001",
         "business_name": "Live Nails A", "zip": "94103", "city": "Garden Grove",
         "region": "CA", "sync_status": "ok", "last_sync": stamp,
         **group_fields("accounts/1001", "Acme Group", "LOCATION_GROUP")},
        {"tenant_id": LOC_A, "location_id": LIVE_B, "account_id": "accounts/2002",
         "business_name": "Live Nails B", "zip": "07001", "city": "Avenel",
         "region": "NJ", "sync_status": "ok", "last_sync": stamp,
         **group_fields("accounts/2002", "Personal", "PERSONAL")},
    ]
    return {"tenant_id": LOC_A, "accounts": accounts, "locations": locations,
            "sync_status": status, "last_sync": stamp}


def make_repo() -> Repository:
    repo = Repository(":memory:")
    repo.init_db(SCHEMA_PATH)
    return repo


def rows_for(repo: Repository, location_name: str):
    return repo.conn.execute(
        "SELECT * FROM business_locations WHERE location_name = ?",
        (location_name,)).fetchone()


# ===================================================================== #
# persistence (unit)
# ===================================================================== #
class PersistBundleTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        acct = self.repo.upsert_account("accounts/1001")
        self.repo.upsert_location(acct, LOC_A, title="Demo Nails A",
                                  city="Garden Grove", state="CA")

    def tearDown(self):
        self.repo.close()

    def test_persists_accounts_and_locations_and_reports_counts(self):
        counts = persist_bundle(self.repo, LOC_A, bundle())
        self.assertEqual(counts["accounts"], 2)
        self.assertEqual(counts["locations"], 2)
        self.assertEqual(counts["removed"], 0)
        self.assertEqual(counts["conflicts"], 0)
        self.assertEqual(counts["sync_status"], "ok")

        row = rows_for(self.repo, LIVE_A)
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Live Nails A")
        self.assertEqual(row["postal_code"], "94103")
        self.assertEqual(row["city"], "Garden Grove")
        self.assertEqual(row["state"], "CA")
        self.assertEqual(row["tenant_id"], LOC_A)
        self.assertEqual(row["source"], "live")
        self.assertEqual(row["sync_status"], "ok")
        self.assertEqual(row["last_sync_at"], "2026-10-24T01:00:00Z")
        # The account resource name is preserved as the dashboard key.
        acct = self.repo.conn.execute(
            "SELECT name, account_name, account_type, tenant_id"
            " FROM google_accounts WHERE id = ?", (row["account_id"],)).fetchone()
        self.assertEqual(acct["name"], "accounts/1001")
        self.assertEqual(acct["account_name"], "Acme Group")
        self.assertEqual(acct["account_type"], "LOCATION_GROUP")
        self.assertEqual(acct["tenant_id"], LOC_A)

    def test_resync_is_idempotent_and_keeps_row_ids(self):
        first = persist_bundle(self.repo, LOC_A, bundle())
        ids_before = {name: rows_for(self.repo, name)["id"]
                      for name in (LIVE_A, LIVE_B)}
        total_before = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM business_locations").fetchone()["n"]

        second = persist_bundle(self.repo, LOC_A, bundle(
            last_sync="2026-10-24T02:00:00Z"))

        self.assertEqual(first["locations"], second["locations"])
        self.assertEqual(
            self.repo.conn.execute(
                "SELECT COUNT(*) AS n FROM business_locations").fetchone()["n"],
            total_before)
        for name, row_id in ids_before.items():
            self.assertEqual(rows_for(self.repo, name)["id"], row_id)
        self.assertEqual(rows_for(self.repo, LIVE_A)["last_sync_at"],
                         "2026-10-24T02:00:00Z")

    def test_group_projection_survives_the_persist(self):
        persist_bundle(self.repo, LOC_A, bundle())
        meta = self.repo.account_metadata()
        group_id_by_account = {
            v["name"]: k for k, v in meta.items()}
        self.assertEqual(
            group_fields("accounts/1001", meta[group_id_by_account["accounts/1001"]]
                         ["account_name"],
                         meta[group_id_by_account["accounts/1001"]]["account_type"]),
            {"group_id": "accounts/1001", "group_name": "Acme Group",
             "group_type": "LOCATION_GROUP"})
        # A primary/personal account is reported as Ungrouped, never invented.
        self.assertEqual(
            group_fields("accounts/2002", meta[group_id_by_account["accounts/2002"]]
                         ["account_name"],
                         meta[group_id_by_account["accounts/2002"]]["account_type"]),
            {"group_id": "accounts/2002", "group_name": "Ungrouped",
             "group_type": "PERSONAL"})

    def test_a_location_no_longer_on_google_is_marked_not_deleted(self):
        persist_bundle(self.repo, LOC_A, bundle())
        shrunk = bundle(locations=[r for r in bundle()["locations"]
                                   if r["location_id"] == LIVE_A])
        counts = persist_bundle(self.repo, LOC_A, shrunk,
                                now_iso="2026-10-24T03:00:00Z")
        self.assertEqual(counts["locations"], 1)
        self.assertEqual(counts["removed"], 1)
        gone = rows_for(self.repo, LIVE_B)
        self.assertIsNotNone(gone)                     # never deleted
        self.assertEqual(gone["sync_status"], "removed")
        self.assertEqual(gone["last_sync_at"], "2026-10-24T03:00:00Z")
        self.assertEqual(rows_for(self.repo, LIVE_A)["sync_status"], "ok")

    def test_another_tenants_rows_are_never_overwritten(self):
        persist_bundle(self.repo, LOC_A, bundle())
        foreign = bundle(
            accounts=[{"account_id": "accounts/1001", "account_name": "Stolen",
                       "type": "LOCATION_GROUP"}],
            locations=[{"location_id": LIVE_A, "account_id": "accounts/1001",
                        "business_name": "Stolen Nails", "zip": "00000"}])
        counts = persist_bundle(self.repo, LOC_B, foreign)
        self.assertEqual(counts["locations"], 0)
        self.assertEqual(counts["conflicts"], 2)  # account + location refused
        row = rows_for(self.repo, LIVE_A)
        self.assertEqual(row["tenant_id"], LOC_A)          # owner unchanged
        self.assertEqual(row["title"], "Live Nails A")     # data unchanged
        acct = self.repo.conn.execute(
            "SELECT account_name FROM google_accounts WHERE name = 'accounts/1001'"
        ).fetchone()
        self.assertEqual(acct["account_name"], "Acme Group")

    def test_a_fixture_row_is_claimed_by_the_tenant_that_syncs_it(self):
        row = rows_for(self.repo, LOC_A)
        self.assertIsNone(row["tenant_id"])  # demo seed: untagged
        persist_bundle(self.repo, LOC_A, bundle(
            accounts=[{"account_id": "accounts/1001", "account_name": "Acme Group",
                       "type": "LOCATION_GROUP"}],
            locations=[{"location_id": LOC_A, "account_id": "accounts/1001",
                        "business_name": "Demo Nails A", "zip": "94103"}]))
        row = rows_for(self.repo, LOC_A)
        self.assertEqual(row["tenant_id"], LOC_A)
        self.assertEqual(row["source"], "live")
        # The same Google resource is ONE row: no duplicate was created.
        self.assertEqual(self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM business_locations WHERE location_name = ?",
            (LOC_A,)).fetchone()["n"], 1)

    def test_reseeding_fixtures_never_strips_live_provenance(self):
        persist_bundle(self.repo, LOC_A, bundle())
        acct_id = rows_for(self.repo, LIVE_A)["account_id"]
        # A later demo seed omits tenant/source/status (fixture call style).
        self.repo.upsert_location(acct_id, LIVE_A, title="Live Nails A")
        row = rows_for(self.repo, LIVE_A)
        self.assertEqual(row["tenant_id"], LOC_A)
        self.assertEqual(row["source"], "live")
        self.assertEqual(row["sync_status"], "ok")


# ===================================================================== #
# recording the outcome on the vault meta
# ===================================================================== #
class RecordOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gbp-persist-outcome-")
        self.vault = TokenVault(self.tmp, VAULT_KEY)
        self.vault.save(LOC_A, Token(access_token="a", refresh_token="r",
                                     expires_at=1e12, scopes=["scope/a"]))

    def test_success_writes_status_and_time_and_clears_a_previous_error(self):
        self.vault.mark_error(LOC_A, "RuntimeError")
        meta = record_outcome(self.vault, LOC_A, bundle())
        self.assertEqual(meta["sync_status"], "ok")
        self.assertEqual(meta["last_sync_at"], "2026-10-24T01:00:00Z")
        self.assertIsNone(meta["last_error"])
        self.assertEqual(self.vault.connection_state(LOC_A), STATE_CONNECTED)

    def test_failure_records_a_safe_reason_only(self):
        meta = record_outcome(self.vault, LOC_A, {
            "sync_status": "error",
            "reason": "RuntimeError",
            "last_sync": "2026-10-24T04:00:00Z"})
        self.assertEqual(meta["sync_status"], "error")
        self.assertEqual(meta["last_sync_at"], "2026-10-24T04:00:00Z")
        self.assertEqual(meta["last_error"], "RuntimeError")
        # The token itself never leaks into the meta projection.
        self.assertNotIn("access_token", json.dumps(meta))
        self.assertNotIn('"refresh_token":', json.dumps(meta))
        self.assertIn("has_refresh_token", meta)

    def test_a_raw_provider_body_is_never_recorded(self):
        meta = record_outcome(self.vault, LOC_A, {
            "sync_status": "error",
            "reason": "HTTP 403 denied\n{\"error\": \"PERMISSION_DENIED\","
                      " \"token\": \"access-token-probe\"}"})
        self.assertEqual(meta["last_error"], "unknown_error")
        self.assertNotIn("access-token-probe", json.dumps(meta))
        self.assertNotIn("PERMISSION_DENIED", json.dumps(meta))

    def test_token_expired_keeps_the_dead_token_reason(self):
        meta = record_outcome(self.vault, LOC_A, {"sync_status": "token_expired"})
        self.assertEqual(meta["sync_status"], "token_expired")
        self.assertEqual(meta["last_error"], "token_expired")


class SafeReasonTests(unittest.TestCase):
    def test_only_class_names_and_error_codes_pass_through(self):
        self.assertEqual(safe_reason(RuntimeError("boom")), "RuntimeError")
        self.assertEqual(safe_reason("PERMISSION_DENIED"), "PERMISSION_DENIED")
        self.assertEqual(safe_reason({"error": "invalid_grant"}), "invalid_grant")
        self.assertEqual(safe_reason("GET https://x/y failed with token abc"),
                         "unknown_error")
        self.assertEqual(safe_reason(None), "unknown_error")
        self.assertEqual(safe_reason({"error": "x" * 300}), "unknown_error")


# ===================================================================== #
# the chip: derived from real vault state
# ===================================================================== #
class HeaderChipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gbp-chip-")
        self.repo = make_repo()
        acct = self.repo.upsert_account("accounts/1001")
        self.repo.upsert_location(acct, LOC_A, title="Demo Nails A")

    def tearDown(self):
        self.repo.close()

    def test_fixture_mode_is_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_google_connected(self.repo))
        with mock.patch.dict(os.environ, {"GBP_GOOGLE_CONNECTED": "1"},
                             clear=True):
            self.assertTrue(_google_connected(self.repo))

    def test_live_mode_reports_connected_only_with_a_stored_token(self):
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            self.assertFalse(_google_connected(self.repo))
            TokenVault(self.tmp, VAULT_KEY).save(LOC_A, Token(
                access_token="a", refresh_token="r", expires_at=1e12,
                scopes=["scope/a"]))
            self.assertTrue(_google_connected(self.repo))
            TokenVault(self.tmp, VAULT_KEY).delete(LOC_A)
            self.assertFalse(_google_connected(self.repo))

    def test_live_mode_off_ignores_a_stored_token(self):
        TokenVault(self.tmp, VAULT_KEY).save(LOC_A, Token(
            access_token="a", refresh_token="r", expires_at=1e12,
            scopes=["scope/a"]))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_google_connected(self.repo))

    def test_non_token_sync_error_does_not_mark_the_connection_dead(self):
        """Issue #121 regression: refresh token + last_error='ApiError' -> live.

        A sync that failed with a NON-token error (an ``ApiError`` recorded via
        :func:`gbp.sync.persist.record_outcome`) must NOT mark the connection
        dead: the stored refresh token is still usable, so BOTH the header chip
        (:func:`_google_connected`) and the connection row report connected.
        """
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            vault = TokenVault(self.tmp, VAULT_KEY)
            vault.save(LOC_A, Token(
                access_token="a", refresh_token="refresh-token-probe",
                expires_at=1e12, scopes=["scope/a"]))
            record_outcome(vault, LOC_A, {
                "tenant_id": LOC_A, "sync_status": "error", "reason": "ApiError",
            }, now_iso="2026-10-24T03:00:00Z")
            # Sanity: the non-token reason really was recorded.
            self.assertEqual(vault.meta(LOC_A)["last_error"], "ApiError")

            # Both signals must agree -- and both must say "connected".
            self.assertEqual(vault.connection_state(LOC_A), STATE_CONNECTED)
            self.assertTrue(_google_connected(self.repo))

            rows = connection_rows(_locations_status(self.repo), vault)
            row = next(r for r in rows if r["location_name"] == LOC_A)
            self.assertEqual(row["connection_state"], STATE_CONNECTED)

    def test_chip_and_row_agree_when_the_tenant_is_the_resource_name(self):
        """Issue #121 divergence: the chip must resolve the tenant like the row.

        The Google Connection row resolves a row's tenant as ``location_name``
        OR ``tenant_id`` OR ``location_id``; the old chip looked at
        ``location_name`` ONLY. A live location whose ``location_name`` is blank
        but whose ``tenant_id`` is the Google resource name therefore read
        "Connected" on the row and "not connected" on the chip. The chip now
        reads the SAME projection, so the two can never disagree again.
        """
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            # A live location tagged with the resource name as its tenant, but
            # with a blank location_name (a real shape for a partially-mapped
            # row) -- the only handle on its vault record is tenant_id.
            self.repo.conn.execute(
                "INSERT INTO business_locations"
                " (account_id, location_name, title, tenant_id, source)"
                " VALUES ((SELECT id FROM google_accounts LIMIT 1),"
                " '', 'Blank Name Salon', ?, 'live')", (LOC_A,))
            self.repo.conn.commit()
            vault = TokenVault(self.tmp, VAULT_KEY)
            vault.save(LOC_A, Token(
                access_token="a", refresh_token="r", expires_at=1e12,
                scopes=["scope/a"]))
            vault.mark_error(LOC_A, "ApiError")

            rows = connection_rows(_locations_status(self.repo), vault)
            row = next(r for r in rows if r.get("tenant_id") == LOC_A)
            self.assertEqual(row["connection_state"], STATE_CONNECTED)
            # The chip must agree with the row.
            self.assertTrue(_google_connected(self.repo))


# ===================================================================== #
# HTTP: callback persists, "Sync now" persists, outcome is visible
# ===================================================================== #
class LiveSyncRouteBase(unittest.TestCase):
    """A live-capable dashboard with injected offline OAuth/GBP transports."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gbp-persist-http-")
        self.repo = make_repo()
        acct = self.repo.upsert_account("accounts/1001")
        self.loc_a = self.repo.upsert_location(
            acct, LOC_A, title="Demo Nails A", city="Garden Grove", state="CA")
        self.loc_b = self.repo.upsert_location(
            acct, LOC_B, title="Demo Nails B", city="Westminster", state="CA")
        self.a_ids = str(self.loc_a)
        self.b_ids = str(self.loc_b)

        seed = UserStore(self.repo.conn)
        seed.create_user("admin", hash_password(PASSWORD), ["admin"], [])
        seed.create_user("owner1", hash_password(PASSWORD), ["owner"], [self.a_ids])

        self.service = ApprovalService(self.repo)
        self.auth = AuthService(self.repo.conn, env=TEST_ENV, ttl=3600)
        self.token_transport = token_transport()
        self.api_transport = sync_transport()
        self.wiring = GoogleConnectWiring(
            token_transport=self.token_transport, api_transport=self.api_transport)
        self.server = create_server(
            self.service, port=0, auth=self.auth,
            rate_limit=100000, login_limit=100000, google_oauth=self.wiring)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repo.close()

    # -- helpers --------------------------------------------------------- #
    def _req(self, method, path, body=None, cookie=None, csrf=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(payload))
        if cookie:
            hdrs["Cookie"] = cookie
        if csrf:
            hdrs["X-CSRF-Token"] = csrf
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else None
            return resp.status, dict(resp.getheaders()), parsed
        finally:
            conn.close()

    def _login(self, username):
        status, headers, body = self._req(
            "POST", "/api/login", {"username": username, "password": PASSWORD})
        set_cookie = headers.get("Set-Cookie", "")
        return (status,
                set_cookie.split(";")[0] if set_cookie else None,
                (body or {}).get("csrf_token") if isinstance(body, dict) else None)

    def _state_from_location(self, location):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        return (query.get("state") or [None])[0]

    def _vault(self):
        return TokenVault(self.tmp, VAULT_KEY)

    def _connect(self, username="owner1"):
        """Run the whole connect flow; returns (cookie, csrf)."""
        _s, cookie, csrf = self._login(username)
        _st, headers, _b = self._req(
            "GET", f"/api/google/connect?location_id={self.a_ids}", cookie=cookie)
        state = self._state_from_location(headers.get("Location", ""))
        status, _h, _b = self._req(
            "GET", "/api/google/callback?code=code-probe&state=" + str(state))
        assert status == 302, status
        return cookie, csrf


class CallbackPersistsTests(LiveSyncRouteBase):
    def test_a_successful_connect_persists_the_locations_into_the_dashboard(self):
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            self._connect()
            # Read the Locations view as an ADMIN: a scoped owner only ever sees
            # the locations granted to it (unchanged tenancy rule), so freshly
            # synced rows stay admin-visible until they are granted.
            _s, admin_cookie, _csrf = self._login("admin")

            # The Locations view now shows the LIVE businesses...
            status, _h, rows = self._req("GET", "/api/locations", cookie=admin_cookie)
            self.assertEqual(status, 200)
            by_name = {r["location_name"]: r for r in rows}
            self.assertIn(LIVE_A, by_name)
            self.assertEqual(by_name[LIVE_A]["business_name"], "Live Nails A")
            self.assertEqual(by_name[LIVE_A]["postal_code"], "94103")

            # ...with the group projection Inc 1b renders.
            status, _h, status_rows = self._req(
                "GET", "/api/locations/status", cookie=admin_cookie)
            self.assertEqual(status, 200)
            live = {r["location_name"]: r for r in status_rows}
            self.assertEqual(live[LIVE_A]["group_name"], "Acme Group")
            self.assertEqual(live[LIVE_A]["group_type"], "LOCATION_GROUP")
            self.assertEqual(live[LIVE_B]["group_name"], "Ungrouped")
            self.assertEqual(live[LIVE_A]["source"], "live")
            self.assertEqual(live[LIVE_A]["last_sync_status"], "ok")

            # The demo fixtures stay visible for rows with no live data.
            self.assertEqual(live[LOC_B]["source"], "fixture")

        # The sync outcome is recorded on the tenant's vault meta.
        meta = self._vault().meta(LOC_A)
        self.assertEqual(meta["sync_status"], "ok")
        self.assertIsNotNone(meta["last_sync_at"])

    def test_the_chip_and_the_connection_row_reflect_the_new_state(self):
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            _s, cookie, _csrf = self._login("owner1")
            status, _h, payload = self._req("GET", "/api/dashboard", cookie=cookie)
            self.assertEqual(status, 200)
            self.assertFalse(payload["google_connected"])  # nothing stored yet

            self._connect()

            status, _h, payload = self._req("GET", "/api/dashboard", cookie=cookie)
            self.assertTrue(payload["google_connected"])   # chip fixed

            status, _h, conns = self._req(
                "GET", "/api/google/connections", cookie=cookie)
            row = next(r for r in conns["connections"]
                       if r["location_name"] == LOC_A)
            self.assertEqual(row["connection_state"], STATE_CONNECTED)
            self.assertEqual(row["google_sync_status"], "ok")
            self.assertIn("last_sync_at", row)
            self.assertNotIn("access-token-probe", json.dumps(conns))


class SyncNowRouteTests(LiveSyncRouteBase):
    def test_sync_now_persists_and_returns_the_counts(self):
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            _s, cookie, csrf = self._login("owner1")
            self._vault().save(LOC_A, Token(
                access_token="a", refresh_token="r", expires_at=1e12,
                scopes=["scope/a"]))
            status, _h, body = self._req(
                "POST", "/api/google/sync", {"location_id": self.a_ids},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["accounts"], 2)
            self.assertEqual(body["locations"], 2)
            self.assertEqual(body["removed"], 0)
            self.assertEqual(body["sync_status"], "ok")
            self.assertNotIn("access-token-probe", json.dumps(body))

            # The counts match what was really persisted for this tenant.
            persisted = self.repo.live_location_names(LOC_A)
            self.assertEqual(sorted(persisted), sorted([LIVE_A, LIVE_B]))

            # A second run is idempotent: same counts, no new rows.
            before = self.repo.conn.execute(
                "SELECT COUNT(*) AS n FROM business_locations").fetchone()["n"]
            status, _h, again = self._req(
                "POST", "/api/google/sync", {"location_id": self.a_ids},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 200)
            self.assertEqual(again["locations"], 2)
            after = self.repo.conn.execute(
                "SELECT COUNT(*) AS n FROM business_locations").fetchone()["n"]
            self.assertEqual(before, after)

            # The vault meta was bumped by the attempt.
            meta = self._vault().meta(LOC_A)
            self.assertEqual(meta["sync_status"], "ok")
            self.assertIsNotNone(meta["last_sync_at"])

    def test_sync_now_is_session_scoped_and_csrf_guarded(self):
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            _s, cookie, csrf = self._login("owner1")
            # No session -> 401 (never a tenant derived from the body).
            status, _h, body = self._req(
                "POST", "/api/google/sync", {"location_id": self.a_ids})
            self.assertEqual(status, 401)
            # Session without CSRF -> refused.
            status, _h, body = self._req(
                "POST", "/api/google/sync", {"location_id": self.a_ids},
                cookie=cookie)
            self.assertEqual(status, 403)
            self.assertEqual(body.get("error"), "csrf_failed")
            # A foreign location is forbidden.
            status, _h, body = self._req(
                "POST", "/api/google/sync", {"location_id": self.b_ids},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 403)
            self.assertEqual(body.get("error"), "forbidden")
            # Unknown body keys are rejected.
            status, _h, body = self._req(
                "POST", "/api/google/sync",
                {"location_id": self.a_ids, "tenant": "accounts/9999/locations/x"},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 400)
            self.assertEqual(self.api_transport.calls, [])

    def test_sync_now_refuses_honestly_in_fixture_mode(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            _s, cookie, csrf = self._login("owner1")
            status, _h, body = self._req(
                "POST", "/api/google/sync", {"location_id": self.a_ids},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 409)
            self.assertEqual(body.get("error"), "live_not_configured")
            self.assertEqual(self.api_transport.calls, [])


class FailedSyncIsRecordedTests(LiveSyncRouteBase):
    class Boom:
        """A sync client whose read fails like a provider error would."""

        def sync(self):
            raise RuntimeError(
                "GET https://mybusiness.googleapis.com/v1/accounts failed: "
                "access-token-probe must never be recorded")

    def test_a_failed_sync_leaves_a_safe_reason_and_a_visible_state(self):
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            self.wiring._sync_factory = lambda tenant, vault: self.Boom()
            _s, cookie, csrf = self._login("owner1")
            self._vault().save(LOC_A, Token(
                access_token="a", refresh_token="r", expires_at=1e12,
                scopes=["scope/a"]))
            status, _h, body = self._req(
                "POST", "/api/google/sync", {"location_id": self.a_ids},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 200)
            self.assertEqual(body["sync_status"], "error")
            self.assertEqual(body["reason"], "RuntimeError")  # class only
            self.assertEqual(body["locations"], 0)

            raw = json.dumps(body)
            self.assertNotIn("access-token-probe", raw)
            self.assertNotIn("mybusiness.googleapis.com", raw)

            # Recorded on the vault meta: no silent no-op any more.
            meta = self._vault().meta(LOC_A)
            self.assertEqual(meta["sync_status"], "error")
            self.assertEqual(meta["last_error"], "RuntimeError")
            self.assertIsNotNone(meta["last_sync_at"])
            # A failed sync never invalidates the token the tenant granted.
            self.assertEqual(self._vault().connection_state(LOC_A), STATE_CONNECTED)

            # ...and it is visible on the Google Connection row the UI reads.
            status, _h, conns = self._req(
                "GET", "/api/google/connections", cookie=cookie)
            row = next(r for r in conns["connections"]
                       if r["location_name"] == LOC_A)
            self.assertEqual(row["google_sync_status"], "error")
            self.assertEqual(row["google_last_error"], "RuntimeError")
            self.assertNotIn("access-token-probe", json.dumps(conns))


# ===================================================================== #
# additive migration on a database that predates Issue #117
# ===================================================================== #
class LegacyDatabaseMigrationTests(unittest.TestCase):
    """The live columns arrive by MIGRATION, so an old DB file keeps its rows.

    ``sql/schema.sql`` is a protected path in this repo, so the live-sync
    columns/index are added by :meth:`Repository.init_db` (additively) rather
    than edited into the canonical schema -- which also means a database created
    before Issue #117 must migrate cleanly, and that is what this pins down.
    """

    def test_columns_and_index_arrive_additively_and_rows_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "legacy-live.db")
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE google_accounts ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " name TEXT NOT NULL UNIQUE,"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')));"
                "CREATE TABLE business_locations ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER,"
                " location_name TEXT NOT NULL UNIQUE, title TEXT, city TEXT,"
                " state TEXT, postal_code TEXT, language TEXT,"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')));"
                "INSERT INTO google_accounts(name) VALUES ('accounts/1001');"
                "INSERT INTO business_locations(account_id, location_name, title)"
                " VALUES (1, 'accounts/1001/locations/old', 'Old Shop');"
            )
            conn.commit()
            conn.close()

            repo = Repository(db)
            repo.init_db(SCHEMA_PATH)
            columns = {row[1] for row in repo.conn.execute(
                "PRAGMA table_info(business_locations)")}
            self.assertTrue(
                {"tenant_id", "source", "sync_status", "last_sync_at"} <= columns)
            indexes = {row[1] for row in repo.conn.execute(
                "PRAGMA index_list(business_locations)")}
            self.assertIn("idx_business_locations_tenant_id", indexes)
            # The pre-existing row kept its data and is reported as fixture data.
            row = rows_for(repo, "accounts/1001/locations/old")
            self.assertEqual(row["title"], "Old Shop")
            self.assertIsNone(row["tenant_id"])
            # ...and a live sync can now claim and persist into that database.
            counts = persist_bundle(repo, LOC_A, bundle())
            self.assertEqual(counts["locations"], 2)
            self.assertEqual(rows_for(repo, LIVE_A)["tenant_id"], LOC_A)
            repo.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
