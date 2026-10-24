"""Issue #123 -- GBP Inc 3: live review sync for the tenant's real locations.

Covers the increment that turns the dashboard's fixture reviews into REAL Google
reviews for the tenant's live locations:

* name composition: a SHORT v1 live row (``locations/{id}``) becomes the FULL v4
  resource name ``accounts/{accountId}/locations/{locationId}``; a name already
  in the full form passes through unchanged;
* ``ReviewSync.sync_location(..., location_row_id=...)`` attaches reviews to the
  EXISTING row and never runs the by-name upsert -- so no duplicate, bare
  location row is created;
* ``LiveReviewSync.sync_all`` isolates a per-location failure and reports a safe
  reason while the other locations still sync;
* ``POST /api/google/sync-reviews`` -- session-scoped, live-gated, read-only,
  returns the summary counts and records the tenant-level outcome;
* end-to-end: sign in, connect (fixture flow), sync locations, sync reviews, and
  find the reviews through the EXISTING ``/api/reviews`` for the live rows.

Fully OFFLINE: the token endpoint and the GBP API are ``MockTransport`` objects
(no socket is opened beyond the loopback test server), no credential is real and
``GBP_LIVE`` is only ever set inside a patched environment. No Google write call
is made anywhere (reply posting stays OFF).

Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import create_server  # noqa: E402
from gbp.auth.token_manager import Token  # noqa: E402
from gbp.authz import AuthService, UserStore, hash_password  # noqa: E402
from gbp.client.http_client import MockTransport  # noqa: E402
from gbp.oauth.config import LIVE_ENV  # noqa: E402
from gbp.oauth.vault import TOKEN_URL, TokenVault  # noqa: E402
from gbp.oauth.wiring import GoogleConnectWiring  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.sync.live_reviews import LiveReviewSync, compose_v4_name  # noqa: E402
from gbp.sync.reviews_sync import ReviewSync  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
TEST_SECRET = "gbp-live-review-sync-secret-not-used-elsewhere"
TEST_ENV = {"GBP_SESSION_SECRET": TEST_SECRET}
VAULT_KEY = "unit-test-vault-key-material-0123456789abcdef"
CANONICAL_REDIRECT = "https://gbp.adzdigital.com/api/google/callback"

#: The tenant anchor: a fixture location row whose resource name IS the tenant
#: key (the connect flow resolves the tenant from an authorised location row).
LOC_A = "accounts/1001/locations/loc-001"
LOC_B = "accounts/1001/locations/loc-002"

#: The tenant's LIVE locations, persisted by the Inc 2 sync in the SHORT v1 form
#: (that is the naming gotcha this increment must compose away).
SHORT_1 = "locations/777001"
SHORT_2 = "locations/777002"
ACCOUNT = "accounts/1001"

PASSWORD = "R3viewsSecret!"


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


def make_repo() -> Repository:
    repo = Repository(":memory:")
    repo.init_db(SCHEMA_PATH)
    return repo


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


def review(name, rating=5, comment="Great", update="2026-10-20T10:00:00Z",
           reviewer="Ada", reply=None):
    payload = {
        "name": name,
        "rating": rating,
        "comment": comment,
        "createTime": "2026-10-19T09:00:00Z",
        "updateTime": update,
        "reviewer": {"displayName": reviewer},
    }
    if reply is not None:
        payload["reviewReply"] = {"comment": reply,
                                  "updateTime": "2026-10-21T09:00:00Z"}
    return payload


def api_transport(reviews_by_location=None) -> MockTransport:
    """A fake GBP API: one account, two SHORT-named live locations, reviews.

    ``reviews_by_location`` maps a location resource name to the list of raw
    reviews served for it; a location that is NOT in the map is left unregistered
    (the fake API answers 404) so per-location failure isolation is exercised.
    """
    reviews_by_location = reviews_by_location if reviews_by_location is not None else {
        "locations/777001": [review("accounts/1001/locations/777001/reviews/r1",
                                    comment="Loved it")],
        "locations/777002": [review("accounts/1001/locations/777002/reviews/r2",
                                    rating=3, comment="It was fine")],
    }
    transport = MockTransport()
    transport.register(
        "GET", "https://mybusiness.googleapis.com/v1/accounts",
        {"accounts": [{"name": ACCOUNT, "accountName": "Acme Group",
                       "type": "LOCATION_GROUP"}]})
    transport.register(
        "GET", f"https://mybusiness.googleapis.com/v1/{ACCOUNT}/locations",
        {"locations": [
            {"name": SHORT_1, "title": "Live Nails A",
             "storefrontAddress": {"postalCode": "94103", "locality": "Garden Grove",
                                   "administrativeArea": "CA"}},
            {"name": SHORT_2, "title": "Live Nails B",
             "storefrontAddress": {"postalCode": "07001", "locality": "Avenel",
                                   "administrativeArea": "NJ"}},
        ]})
    for location, rows in reviews_by_location.items():
        transport.register(
            "GET",
            f"https://mybusiness.googleapis.com/v4/{ACCOUNT}/{location}/reviews",
            {"reviews": rows})
    return transport


# ===================================================================== #
# name composition (pure)
# ===================================================================== #
class ComposeV4NameTests(unittest.TestCase):
    def test_short_form_is_composed_with_its_account(self):
        self.assertEqual(compose_v4_name(ACCOUNT, SHORT_1),
                         "accounts/1001/locations/777001")

    def test_full_form_passes_through_unchanged(self):
        full = "accounts/1001/locations/777001"
        self.assertEqual(compose_v4_name(ACCOUNT, full), full)
        self.assertEqual(compose_v4_name("", full), full)

    def test_composition_is_idempotent(self):
        once = compose_v4_name(ACCOUNT, SHORT_1)
        self.assertEqual(compose_v4_name(ACCOUNT, once), once)

    def test_a_missing_account_leaves_the_short_name_alone(self):
        self.assertEqual(compose_v4_name("", SHORT_1), SHORT_1)


# ===================================================================== #
# ReviewSync pre-resolved target (offline)
# ===================================================================== #
class FakeReviewClient:
    """A GbpClient stand-in serving one page of reviews per location."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list_reviews(self, location, page_token=None):
        self.calls.append((location, page_token))
        return self.pages.get(location, {"reviews": []})


class PreResolvedTargetTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        acct = self.repo.upsert_account(ACCOUNT, "Acme Group", "LOCATION_GROUP",
                                        tenant_id=LOC_A)
        # The live row Inc 2 persisted: SHORT name, tenant-tagged, source=live.
        self.live_row = self.repo.upsert_location(
            acct, SHORT_1, title="Live Nails A", tenant_id=LOC_A, source="live")
        self.full_name = "accounts/1001/locations/777001"

    def test_reviews_land_on_the_existing_row_and_no_new_row_is_created(self):
        client = FakeReviewClient({self.full_name: {"reviews": [
            review("accounts/1001/locations/777001/reviews/r1"),
            review("accounts/1001/locations/777001/reviews/r2", rating=2),
        ]}})
        before = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM business_locations").fetchone()["n"]

        result = ReviewSync(self.repo, client).sync_location(
            self.full_name, location_row_id=self.live_row)

        self.assertEqual(result["upserted"], 2)
        self.assertEqual(result["status"], "ok")
        # Reviews are attached to the EXISTING live row...
        rows = self.repo.conn.execute(
            "SELECT location_id, review_name FROM reviews ORDER BY review_name"
        ).fetchall()
        self.assertEqual([r["location_id"] for r in rows], [self.live_row] * 2)
        # ...and NO bare, full-name location row was inserted.
        after = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM business_locations").fetchone()["n"]
        self.assertEqual(before, after)
        self.assertIsNone(self.repo.conn.execute(
            "SELECT id FROM business_locations WHERE location_name = ?",
            (self.full_name,)).fetchone())

    def test_default_behaviour_still_upserts_by_name(self):
        # No row id -> the offline by-name path is unchanged (creates the row).
        client = FakeReviewClient({self.full_name: {"reviews": [
            review("accounts/1001/locations/777001/reviews/r1")]}})
        ReviewSync(self.repo, client).sync_location(self.full_name)
        row = self.repo.conn.execute(
            "SELECT id FROM business_locations WHERE location_name = ?",
            (self.full_name,)).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(int(row["id"]), int(self.live_row))


# ===================================================================== #
# LiveReviewSync per-location isolation (offline)
# ===================================================================== #
class LiveReviewSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gbp-live-reviews-")
        self.repo = make_repo()
        acct = self.repo.upsert_account(ACCOUNT, "Acme Group", "LOCATION_GROUP",
                                        tenant_id=LOC_A)
        self.repo.upsert_location(acct, SHORT_1, title="Live Nails A",
                                  tenant_id=LOC_A, source="live")
        self.repo.upsert_location(acct, SHORT_2, title="Live Nails B",
                                  tenant_id=LOC_A, source="live")
        self.vault = TokenVault(self.tmp, VAULT_KEY)
        self.vault.save(LOC_A, Token(access_token="a", refresh_token="r",
                                     expires_at=1e12, scopes=["scope/a"]))

    def _engine(self, transport):
        return LiveReviewSync(self.vault, LOC_A, self.repo,
                              http=transport, transport=token_transport())

    def test_syncs_all_live_locations_and_composes_full_names(self):
        transport = api_transport()
        engine = self._engine(transport)
        summary = engine.sync_all(self.repo.live_locations(LOC_A))

        self.assertEqual(summary["locations"], 2)
        self.assertEqual(summary["reviews_upserted"], 2)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["sync_status"], "ok")
        # The FULL v4 names were the ones actually requested.
        urls = [c["url"] for c in transport.calls]
        self.assertIn(
            "https://mybusiness.googleapis.com/v4/accounts/1001/locations/777001/reviews",
            urls)
        self.assertIn(
            "https://mybusiness.googleapis.com/v4/accounts/1001/locations/777002/reviews",
            urls)
        # No duplicate location rows.
        self.assertEqual(self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM business_locations").fetchone()["n"], 2)

    def test_one_failing_location_does_not_abort_the_other(self):
        # Only the FIRST location has a registered reviews route.
        transport = api_transport({
            "locations/777001": [review(
                "accounts/1001/locations/777001/reviews/r1")]})
        engine = self._engine(transport)
        summary = engine.sync_all(self.repo.live_locations(LOC_A))

        self.assertEqual(summary["locations"], 2)
        self.assertEqual(summary["sync_status"], "partial")
        self.assertEqual(summary["errors"], 1)
        by_name = {e["business_name"]: e for e in summary["per_location"]}
        self.assertEqual(by_name["Live Nails A"]["upserted"], 1)
        self.assertEqual(by_name["Live Nails A"].get("reason"), None)
        self.assertEqual(by_name["Live Nails B"]["errors"], 1)
        self.assertTrue(by_name["Live Nails B"]["reason"])  # a safe reason only
        # The good location's review really landed.
        self.assertEqual(self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews").fetchone()["n"], 1)


# ===================================================================== #
# HTTP: POST /api/google/sync-reviews
# ===================================================================== #
class LiveReviewRouteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gbp-live-reviews-http-")
        self.repo = make_repo()
        acct = self.repo.upsert_account(ACCOUNT)
        self.loc_a = self.repo.upsert_location(
            acct, LOC_A, title="Demo Nails A", city="Garden Grove", state="CA")
        self.loc_b = self.repo.upsert_location(
            acct, LOC_B, title="Demo Nails B", city="Westminster", state="CA")
        self.a_ids = str(self.loc_a)

        seed = UserStore(self.repo.conn)
        seed.create_user("admin", hash_password(PASSWORD), ["admin"], [])
        seed.create_user("owner1", hash_password(PASSWORD), ["owner"], [self.a_ids])

        self.service = ApprovalService(self.repo)
        self.auth = AuthService(self.repo.conn, env=TEST_ENV, ttl=3600)
        self.token_transport = token_transport()
        self.api_transport = api_transport()
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
        """Run the whole connect + locations-sync flow; returns (cookie, csrf)."""
        _s, cookie, csrf = self._login(username)
        _st, headers, _b = self._req(
            "GET", f"/api/google/connect?location_id={self.a_ids}", cookie=cookie)
        state = self._state_from_location(headers.get("Location", ""))
        status, _h, _b = self._req(
            "GET", "/api/google/callback?code=code-probe&state=" + str(state))
        assert status == 302, status
        return cookie, csrf


class SyncReviewsRouteTests(LiveReviewRouteBase):
    def test_end_to_end_connect_sync_locations_sync_reviews(self):
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            cookie, csrf = self._connect()

            # Inc 2 persisted the two LIVE rows (SHORT names) for this tenant.
            self.assertEqual(sorted(self.repo.live_location_names(LOC_A)),
                             sorted([SHORT_1, SHORT_2]))
            before = self.repo.conn.execute(
                "SELECT COUNT(*) AS n FROM business_locations").fetchone()["n"]

            status, _h, body = self._req(
                "POST", "/api/google/sync-reviews", {"location_id": self.a_ids},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 200, body)
            self.assertTrue(body["ok"])
            self.assertEqual(body["locations"], 2)
            self.assertEqual(body["reviews_upserted"], 2)
            self.assertEqual(body["errors"], 0)
            self.assertEqual(body["sync_status"], "ok")
            self.assertEqual(len(body["per_location"]), 2)
            self.assertNotIn("access-token-probe", json.dumps(body))

            # ZERO duplicate location rows were created.
            after = self.repo.conn.execute(
                "SELECT COUNT(*) AS n FROM business_locations").fetchone()["n"]
            self.assertEqual(before, after)
            self.assertIsNone(self.repo.conn.execute(
                "SELECT id FROM business_locations WHERE location_name = ?",
                (f"{ACCOUNT}/{SHORT_1}",)).fetchone())

            # The REAL reviews are visible through the existing /api/reviews,
            # attached to the live-source rows.
            _s, admin_cookie, _csrf = self._login("admin")
            status, _h, page = self._req("GET", "/api/reviews", cookie=admin_cookie)
            self.assertEqual(status, 200)
            items = page["items"]
            by_comment = {i["comment"]: i for i in items}
            self.assertIn("Loved it", by_comment)
            self.assertEqual(by_comment["Loved it"]["business_name"], "Live Nails A")
            self.assertEqual(by_comment["It was fine"]["business_name"],
                             "Live Nails B")

        # The tenant-level outcome was recorded on the vault meta.
        meta = self._vault().meta(LOC_A)
        self.assertEqual(meta["sync_status"], "ok")
        self.assertIsNotNone(meta["last_sync_at"])

    def test_per_location_failure_is_isolated_and_reported(self):
        # Rebuild the wiring with only ONE location's reviews registered.
        self.wiring = GoogleConnectWiring(
            token_transport=self.token_transport,
            api_transport=api_transport({"locations/777001": [review(
                "accounts/1001/locations/777001/reviews/r1")]}))
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.server = create_server(
            self.service, port=0, auth=self.auth,
            rate_limit=100000, login_limit=100000, google_oauth=self.wiring)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            cookie, csrf = self._connect()
            status, _h, body = self._req(
                "POST", "/api/google/sync-reviews", {"location_id": self.a_ids},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["locations"], 2)
            self.assertEqual(body["errors"], 1)
            self.assertEqual(body["sync_status"], "partial")
            by_name = {e["business_name"]: e for e in body["per_location"]}
            self.assertEqual(by_name["Live Nails A"]["upserted"], 1)
            self.assertEqual(by_name["Live Nails B"]["errors"], 1)
            self.assertTrue(by_name["Live Nails B"]["reason"])  # safe reason only
            # The healthy location still synced its review.
            self.assertEqual(self.repo.conn.execute(
                "SELECT COUNT(*) AS n FROM reviews").fetchone()["n"], 1)

    def test_is_session_scoped_and_csrf_guarded(self):
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            _s, cookie, csrf = self._login("owner1")
            self._vault().save(LOC_A, Token(
                access_token="a", refresh_token="r", expires_at=1e12,
                scopes=["scope/a"]))
            # No session -> 401.
            status, _h, _b = self._req(
                "POST", "/api/google/sync-reviews", {"location_id": self.a_ids})
            self.assertEqual(status, 401)
            # Session without CSRF -> refused.
            status, _h, body = self._req(
                "POST", "/api/google/sync-reviews", {"location_id": self.a_ids},
                cookie=cookie)
            self.assertEqual(status, 403)
            self.assertEqual(body.get("error"), "csrf_failed")
            # A foreign location is forbidden (tenant never taken from the body).
            _s, other_cookie, other_csrf = self._login("owner1")
            status, _h, body = self._req(
                "POST", "/api/google/sync-reviews", {"location_id": str(self.loc_b)},
                cookie=other_cookie, csrf=other_csrf)
            self.assertEqual(status, 403)

    def test_refuses_honestly_in_fixture_mode(self):
        # No GBP_LIVE in the environment -> the honest 409, never a fake sync.
        _s, cookie, csrf = self._login("owner1")
        status, _h, body = self._req(
            "POST", "/api/google/sync-reviews", {"location_id": self.a_ids},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 409)
        self.assertEqual(body.get("error"), "live_not_configured")


if __name__ == "__main__":
    unittest.main()
