"""Issue #114 (GBP increment 1c) -- the Connect Google button + OAuth routes.

Wires the Inc 1 (issue #110) OAuth engine end-to-end over HTTP:

* ``GET  /api/google/connect``    -- session-scoped, live-gated, 302 to Google;
* ``GET  /api/google/callback``   -- Google's top-level redirect, validated by
  the single-use ``state`` (tenant derived server-side), then the read-only
  accounts/locations sync, then 302 back to the dashboard;
* ``POST /api/google/disconnect`` -- session-scoped, drops only the caller's
  tenant token.

Fully OFFLINE: the token endpoint and the GBP API are both a ``MockTransport``
(no socket is opened), and the only loopback server is the repo's HTTP harness.
No credential is real and ``GBP_LIVE`` is never enabled outside a test's patched
environment -- every case that needs live mode patches it in and out again.

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
from gbp.client.http_client import HttpClient, MockTransport  # noqa: E402
from gbp.oauth.config import LIVE_ENV  # noqa: E402
from gbp.oauth.vault import (  # noqa: E402
    STATE_CONNECTED,
    STATE_NOT_CONNECTED,
    TOKEN_URL,
    TokenVault,
)
from gbp.oauth.wiring import GoogleConnectWiring  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
TEST_SECRET = "gbp-connect-inc1c-secret-not-used-elsewhere"
TEST_ENV = {"GBP_SESSION_SECRET": TEST_SECRET}
VAULT_KEY = "unit-test-vault-key-material-0123456789abcdef"

#: The canonical production redirect the owner registers, used in tests/examples.
CANONICAL_REDIRECT = "https://gbp.adzdigital.com/api/google/callback"

LOC_A = "accounts/1001/locations/loc-001"
LOC_B = "accounts/1001/locations/loc-002"

PASSWORD = "C0nnectSecret!"
USERNAMES = {"admin": "admin", "owner": "owner1"}


def full_env(vault_dir: str, **over: str) -> dict:
    """A complete live configuration, overridable per test."""
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


def sync_transport() -> MockTransport:
    transport = MockTransport()
    transport.register(
        "GET", "https://mybusiness.googleapis.com/v1/accounts",
        {"accounts": [{"name": "accounts/1001", "accountName": "Acme Group"}]})
    transport.register(
        "GET", "https://mybusiness.googleapis.com/v1/accounts/1001/locations",
        {"locations": [
            {"name": LOC_A, "title": "Demo Nails A",
             "storefrontAddress": {"postalCode": "94103"}},
        ]})
    return transport


class ConnectRouteBase(unittest.TestCase):
    """A live-capable server with injected offline OAuth transports."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gbp-connect-")
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc_a = self.repo.upsert_location(
            acct, LOC_A, title="Demo Nails A", city="Garden Grove",
            state="CA", language="en")
        self.loc_b = self.repo.upsert_location(
            acct, LOC_B, title="Demo Nails B", city="Westminster",
            state="CA", language="vi")
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
        cookie = set_cookie.split(";")[0] if set_cookie else None
        csrf = (body or {}).get("csrf_token") if isinstance(body, dict) else None
        return status, cookie, csrf

    def _state_from_location(self, location):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        return (query.get("state") or [None])[0]

    def _vault(self):
        """The same encrypted vault the routes use (read/write in assertions)."""
        return TokenVault(self.tmp, VAULT_KEY, cipher=None)


# ===================================================================== #
# fixture mode: refuse honestly, never a fake redirect
# ===================================================================== #
class FixtureModeRefusalTests(ConnectRouteBase):
    def test_connect_callback_and_disconnect_refuse_when_not_configured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            _s, cookie, csrf = self._login("owner1")

            status, headers, body = self._req(
                "GET", f"/api/google/connect?location_id={self.a_ids}", cookie=cookie)
            self.assertEqual(status, 409)
            self.assertEqual(body.get("error"), "live_not_configured")
            self.assertNotIn("Location", headers)

            status, _h, body = self._req(
                "GET", "/api/google/callback?code=probe&state=probe")
            self.assertEqual(status, 409)
            self.assertEqual(body.get("error"), "live_not_configured")

            status, _h, body = self._req(
                "POST", "/api/google/disconnect", {"location_id": self.a_ids},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 409)
            self.assertEqual(body.get("error"), "live_not_configured")

    def test_connect_requires_a_session(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            status, _h, body = self._req(
                "GET", f"/api/google/connect?location_id={self.a_ids}")
            self.assertEqual(status, 401)
            self.assertEqual(body.get("error"), "unauthenticated")


# ===================================================================== #
# live mode: 302 to Google, valid callback stores + syncs + redirects
# ===================================================================== #
class ConnectCallbackLiveTests(ConnectRouteBase):
    def test_connect_302s_to_google_with_state_and_never_leaks_the_secret(self):
        env = full_env(self.tmp)
        with mock.patch.dict(os.environ, env, clear=True):
            _s, cookie, _csrf = self._login("owner1")
            status, headers, _body = self._req(
                "GET", f"/api/google/connect?location_id={self.a_ids}", cookie=cookie)
        self.assertEqual(status, 302)
        location = headers.get("Location", "")
        self.assertTrue(location.startswith(
            "https://accounts.google.com/o/oauth2/v2/auth?"), msg=location)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        self.assertTrue(query.get("state"))
        self.assertEqual(query.get("redirect_uri"), [CANONICAL_REDIRECT])
        self.assertIn("business.manage", query.get("scope", [""])[0])
        # No credential ever rides the redirect.
        self.assertNotIn("client-secret-probe", location)

    def test_valid_callback_stores_the_token_syncs_and_redirects(self):
        env = full_env(self.tmp)
        with mock.patch.dict(os.environ, env, clear=True):
            _s, cookie, _csrf = self._login("owner1")
            status, headers, _b = self._req(
                "GET", f"/api/google/connect?location_id={self.a_ids}", cookie=cookie)
            self.assertEqual(status, 302)
            state = self._state_from_location(headers["Location"])
            self.assertTrue(state)

            calls_before = len(self.token_transport.calls)
            status, headers, _b = self._req(
                "GET", "/api/google/callback?code=code-probe&state="
                     + urllib.parse.quote(state))
            self.assertEqual(status, 302)
            self.assertEqual(headers.get("Location"),
                             "/?area=google&google=connected")
            # The exchange ran exactly once and the read-only sync hit the API.
            self.assertEqual(len(self.token_transport.calls), calls_before + 1)
            self.assertTrue(self.api_transport.calls)

            # The token landed in the tenant's encrypted vault slot...
            self.assertEqual(
                self._vault().connection_state(LOC_A), STATE_CONNECTED)
            # ...and the dashboard endpoint now reports it connected.
            status, _h, payload = self._req(
                "GET", "/api/google/connections", cookie=cookie)
            self.assertEqual(status, 200)
            row = next(r for r in payload["connections"]
                       if r["location_name"] == LOC_A)
            self.assertEqual(row["connection_state"], STATE_CONNECTED)
            self.assertNotIn("access-token-probe", json.dumps(payload))

    def test_a_bad_state_is_rejected_and_a_replay_burns_nothing_twice(self):
        env = full_env(self.tmp)
        with mock.patch.dict(os.environ, env, clear=True):
            # Unknown state: rejected, nothing minted.
            status, _h, body = self._req(
                "GET", "/api/google/callback?code=code-probe&state=not-a-state")
            self.assertEqual(status, 400)
            self.assertEqual(body.get("error"), "oauth_failed")
            self.assertEqual(self.token_transport.calls, [])

            _s, cookie, _csrf = self._login("owner1")
            _st, headers, _b = self._req(
                "GET", f"/api/google/connect?location_id={self.a_ids}", cookie=cookie)
            state = self._state_from_location(headers["Location"])

            _st, _h, _b = self._req(
                "GET", "/api/google/callback?code=code-probe&state=" + state)
            calls_after_first = len(self.token_transport.calls)

            # A second callback with the same state is a replay: no second token.
            status, _h, body = self._req(
                "GET", "/api/google/callback?code=code-probe&state=" + state)
            self.assertEqual(status, 400)
            self.assertEqual(body.get("reason"), "state_replayed")
            self.assertEqual(len(self.token_transport.calls), calls_after_first)
            self.assertEqual(self._vault().connection_state(LOC_A), STATE_CONNECTED)

    def test_a_cross_tenant_location_cannot_be_connected(self):
        env = full_env(self.tmp)
        with mock.patch.dict(os.environ, env, clear=True):
            _s, cookie, _csrf = self._login("owner1")  # scoped to loc-A only
            status, _h, body = self._req(
                "GET", f"/api/google/connect?location_id={self.b_ids}", cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "forbidden")
        self.assertEqual(self.token_transport.calls, [])

    def test_a_callback_writes_only_to_the_states_tenant(self):
        env = full_env(self.tmp)
        with mock.patch.dict(os.environ, env, clear=True):
            _s, admin_cookie, _csrf = self._login("admin")
            _st, headers, _b = self._req(
                "GET", f"/api/google/connect?location_id={self.b_ids}",
                cookie=admin_cookie)
            state = self._state_from_location(headers["Location"])
            status, _h, _b = self._req(
                "GET", "/api/google/callback?code=code-probe&state=" + state)
            self.assertEqual(status, 302)
        vault = self._vault()
        self.assertEqual(vault.connection_state(LOC_B), STATE_CONNECTED)
        # The state's tenant is derived server-side: loc-A is untouched.
        self.assertEqual(vault.connection_state(LOC_A), STATE_NOT_CONNECTED)
        self.assertIsNone(vault.load(LOC_A))


# ===================================================================== #
# disconnect: clears only the caller's tenant
# ===================================================================== #
class DisconnectTests(ConnectRouteBase):
    def test_disconnect_clears_only_the_callers_tenant(self):
        env = full_env(self.tmp)
        vault = self._vault()
        vault.save(LOC_A, Token(access_token="a", refresh_token="r",
                                expires_at=1e12, scopes=["scope/a"]))
        vault.save(LOC_B, Token(access_token="b", refresh_token="r",
                                expires_at=1e12, scopes=["scope/a"]))
        with mock.patch.dict(os.environ, env, clear=True):
            _s, cookie, csrf = self._login("owner1")  # scoped to loc-A only
            status, _h, body = self._req(
                "POST", "/api/google/disconnect", {"location_id": self.a_ids},
                cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("disconnected"))
        self.assertEqual(vault.connection_state(LOC_A), STATE_NOT_CONNECTED)
        self.assertEqual(vault.connection_state(LOC_B), STATE_CONNECTED)

    def test_disconnect_of_a_foreign_location_is_forbidden(self):
        env = full_env(self.tmp)
        with mock.patch.dict(os.environ, env, clear=True):
            _s, cookie, csrf = self._login("owner1")
            status, _h, body = self._req(
                "POST", "/api/google/disconnect", {"location_id": self.b_ids},
                cookie=cookie, csrf=csrf)
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "forbidden")

    def test_disconnect_requires_csrf(self):
        env = full_env(self.tmp)
        with mock.patch.dict(os.environ, env, clear=True):
            _s, cookie, _csrf = self._login("owner1")
            status, _h, body = self._req(
                "POST", "/api/google/disconnect", {"location_id": self.a_ids},
                cookie=cookie)  # no X-CSRF-Token
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "csrf_failed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
