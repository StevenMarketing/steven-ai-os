"""Group 8A / T22 -- auth wiring enforced at the API layer.

Covered here (all offline, stdlib only, no network):

* no session on /api/dashboard -> 401 when auth is enabled
* a mutating POST without X-CSRF-Token -> 403; with the right token -> 200
* a bad-password login -> 401 and the body leaks no password/hash/token material
* a successful login: HttpOnly + SameSite=Lax cookie, NOT Secure by default;
  Secure ONLY when GBP_COOKIE_SECURE=1
* logout revokes the session: the same token is rejected afterwards (401)
* GET /api/me returns the current user but NEVER the session token / password hash
* with auth=None (mock mode) the original unauthenticated behaviour is intact

Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import SESSION_COOKIE, create_server  # noqa: E402
from gbp.authz import AuthService, UserStore  # noqa: E402
from gbp.authz.models import hash_password  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
TEST_SECRET = "auth-wiring-test-secret-not-used-elsewhere"
TEST_ENV = {"GBP_SESSION_SECRET": TEST_SECRET}

LOC_A = "accounts/1001/locations/loc-001"

PASSWORD = "Sup3rSecret-Admin!"  # used to prove it never leaks


class AuthWiringBase(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc_a = self.repo.upsert_location(
            acct, LOC_A, title="Demo Nails — Garden Grove", city="Garden Grove",
            state="CA", language="en")
        rid_a = self.repo.upsert_review(
            location_id=self.loc_a, review_name="reviews/a1", rating=5,
            comment="Loved my gel manicure!", reply_text=None)
        self.draft_a = self.repo.insert_reply_draft(
            rid_a, draft_text="Thanks!", status="needs_review")

        store = UserStore(self.repo.conn)
        store.create_user("admin", hash_password(PASSWORD), ["admin"], [])

        self.service = ApprovalService(self.repo)
        self.servers = []

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.repo.close()

    # ----------------------------------------------------------- #
    def _start(self, *, auth: bool, secure_env: bool = False):
        """Start a server in-process and return its port."""
        prev = os.environ.get("GBP_COOKIE_SECURE")
        if secure_env:
            os.environ["GBP_COOKIE_SECURE"] = "1"
        else:
            os.environ.pop("GBP_COOKIE_SECURE", None)
        try:
            auth_obj = AuthService(self.repo.conn, env=TEST_ENV, ttl=3600)
            server = create_server(
                self.service, port=0,
                auth=auth_obj if auth else None,
                rate_limit=1000, login_limit=1000,
            )
        finally:
            # Restore the env for the rest of the process.
            if prev is None:
                os.environ.pop("GBP_COOKIE_SECURE", None)
            else:
                os.environ["GBP_COOKIE_SECURE"] = prev
        host, port = server.server_address[:2]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return port

    def _req(self, port, method, path, body=None, cookie=None, csrf=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
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

    def _login(self, port, username="admin", password=PASSWORD):
        status, headers, body = self._req(
            port, "POST", "/api/login", {"username": username, "password": password})
        set_cookie = headers.get("Set-Cookie", "")
        cookie = set_cookie.split(";")[0] if set_cookie else None
        csrf = (body or {}).get("csrf_token") if isinstance(body, dict) else None
        return status, set_cookie, cookie, csrf, body


class AuthEnforcedTest(AuthWiringBase):
    def test_no_session_on_dashboard_is_401(self):
        port = self._start(auth=True)
        status, _h, body = self._req(port, "GET", "/api/dashboard")
        self.assertEqual(status, 401)
        self.assertEqual(body.get("error"), "unauthenticated")

    def test_post_without_csrf_is_403_and_with_csrf_is_200(self):
        port = self._start(auth=True)
        _s, _sc, cookie, csrf, _b = self._login(port)
        # no CSRF header -> 403, and the draft is untouched
        status, _h, body = self._req(
            port, "POST", f"/api/drafts/{self.draft_a}/approve", {}, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "csrf_failed")
        # wrong CSRF header -> 403
        status, _h, _b = self._req(
            port, "POST", f"/api/drafts/{self.draft_a}/approve", {},
            cookie=cookie, csrf="not-the-token")
        self.assertEqual(status, 403)
        # the correct token -> 200
        status, _h, _b = self._req(
            port, "POST", f"/api/drafts/{self.draft_a}/approve", {},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)

    def test_bad_password_login_is_401_with_no_secret_material(self):
        port = self._start(auth=True)
        status, set_cookie, _cookie, _csrf, body = self._login(
            port, password=PASSWORD + "-wrong")
        self.assertEqual(status, 401)
        self.assertNotIn(SESSION_COOKIE, set_cookie)
        text = json.dumps(body).lower()
        self.assertNotIn(PASSWORD.lower(), text)
        self.assertNotIn(PASSWORD.lower() + "-wrong", text)
        self.assertNotIn("password", text)
        self.assertNotIn("pbkdf2", text)

    def test_login_cookie_flags_default_no_secure(self):
        port = self._start(auth=True)
        status, set_cookie, cookie, csrf, _b = self._login(port)
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)
        self.assertIn("Path=/", set_cookie)
        self.assertNotIn("Secure", set_cookie)
        self.assertTrue(cookie.startswith(SESSION_COOKIE + "="))
        self.assertTrue(csrf)

    def test_cookie_is_secure_only_when_env_opts_in(self):
        port = self._start(auth=True, secure_env=True)
        status, set_cookie, _cookie, _csrf, _b = self._login(port)
        self.assertEqual(status, 200)
        self.assertIn("Secure", set_cookie)

    def test_logout_revokes_the_token(self):
        port = self._start(auth=True)
        _s, _sc, cookie, csrf, _b = self._login(port)
        self.assertEqual(self._req(port, "GET", "/api/dashboard", cookie=cookie)[0], 200)
        status, _h, _b = self._req(
            port, "POST", "/api/logout", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        status, _h, body = self._req(port, "GET", "/api/dashboard", cookie=cookie)
        self.assertEqual(status, 401)
        self.assertEqual(body.get("error"), "unauthenticated")

    def test_me_returns_user_but_never_token_or_hash(self):
        port = self._start(auth=True)
        status, _sc, cookie, csrf, _b = self._login(port)
        self.assertEqual(status, 200)
        status, _h, body = self._req(port, "GET", "/api/me", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("authenticated"))
        self.assertEqual(body.get("username"), "admin")
        self.assertIn("admin", body.get("roles", []))
        self.assertEqual(body.get("csrf_token"), csrf)
        # never the session token or the password hash
        token = cookie.split("=", 1)[1]
        text = json.dumps(body)
        self.assertNotIn(token, text)
        self.assertNotIn("pbkdf2", text)
        self.assertNotIn("password", text.lower())
        # the hash itself must not leak either
        stored = self.repo.conn.execute(
            "SELECT password_hash FROM users WHERE username = 'admin'").fetchone()
        self.assertNotIn(stored["password_hash"], text)

    def test_me_requires_a_session(self):
        port = self._start(auth=True)
        self.assertEqual(self._req(port, "GET", "/api/me")[0], 401)


class MockModeUnchangedTest(AuthWiringBase):
    def test_mock_mode_still_serves_without_auth(self):
        port = self._start(auth=False)
        # no cookie needed
        status, _h, body = self._req(port, "GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("mock_mode"))
        # a mutating POST needs no CSRF in mock mode
        status, _h, _b = self._req(
            port, "POST", f"/api/drafts/{self.draft_a}/approve", {"actor": "tester"})
        self.assertEqual(status, 200)
        # /api/me reports the unauthenticated mock state, no identity
        status, _h, me = self._req(port, "GET", "/api/me")
        self.assertEqual(status, 200)
        self.assertFalse(me.get("authenticated"))
        self.assertTrue(me.get("mock_mode"))
        self.assertIsNone(me.get("username"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
