"""Offline tests for authentication + role/per-location authorization (5D).

Standard library only, no network: the HTTP tests bind 127.0.0.1 on an
ephemeral port and everything else is a direct unit test. Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import warnings

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import SESSION_COOKIE, create_server  # noqa: E402
from gbp.authz import AuthService, User, hash_password, verify_password  # noqa: E402
from gbp.authz.auth import ROLE_PERMISSIONS  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")

TEST_SECRET = "unit-test-secret-not-used-anywhere-else"
TEST_ENV = {"GBP_SESSION_SECRET": TEST_SECRET}

LOC_A = "accounts/1001/locations/loc-001"
LOC_B = "accounts/1001/locations/loc-002"


def _users(auth_username_locs):
    """Build User objects: [(username, password, roles, locations), ...]."""
    return [
        User(username=u, password_hash=hash_password(p), roles=r, locations=l)
        for (u, p, r, l) in auth_username_locs
    ]


# ======================================================================= #
# direct unit tests -- role matrix, hashing, sessions, secrets
# ======================================================================= #
class AuthServiceUnitTest(unittest.TestCase):
    def setUp(self):
        self.auth = AuthService(
            env=TEST_ENV,
            ttl=3600,
            users=_users([
                ("admin", "admin-pass", ["admin"], []),
                ("owner1", "owner-pass", ["owner"], [LOC_A]),
                ("reviewer1", "reviewer-pass", ["reviewer"], [LOC_A]),
            ]),
        )

    # ----------------------------------------------------------- #
    def test_role_matrix_admin_can_do_all(self):
        admin = self.auth.get_user("admin")
        for action in ("view", "edit", "approve", "reject", "admin"):
            for loc in (None, LOC_A, LOC_B, "some/other/place"):
                self.assertTrue(
                    self.auth.authorize(admin, action, loc),
                    msg=f"admin denied {action} @ {loc}",
                )
        self.assertTrue(self.auth.can_manage_users(admin))
        self.assertEqual(ROLE_PERMISSIONS["admin"], frozenset(
            {"view", "edit", "approve", "reject", "admin"}))

    def test_owner_limited_to_its_own_locations(self):
        owner = self.auth.get_user("owner1")
        self.assertTrue(self.auth.authorize(owner, "view", LOC_A))
        self.assertTrue(self.auth.authorize(owner, "edit", LOC_A))
        self.assertTrue(self.auth.authorize(owner, "approve", LOC_A))
        self.assertTrue(self.auth.authorize(owner, "reject", LOC_A))
        # Other locations are denied for every action.
        for action in ("view", "edit", "approve", "reject"):
            self.assertFalse(self.auth.authorize(owner, action, LOC_B))
        # No location, no admin action.
        self.assertFalse(self.auth.authorize(owner, "view", None))
        self.assertFalse(self.auth.authorize(owner, "admin", LOC_A))
        self.assertFalse(self.auth.can_manage_users(owner))

    def test_reviewer_limited_and_cannot_manage_users(self):
        reviewer = self.auth.get_user("reviewer1")
        self.assertTrue(self.auth.authorize(reviewer, "view", LOC_A))
        self.assertTrue(self.auth.authorize(reviewer, "edit", LOC_A))
        self.assertTrue(self.auth.authorize(reviewer, "approve", LOC_A))
        self.assertTrue(self.auth.authorize(reviewer, "reject", LOC_A))
        self.assertFalse(self.auth.authorize(reviewer, "view", LOC_B))
        self.assertFalse(self.auth.authorize(reviewer, "approve", LOC_B))
        # reviewers can never manage users / act as admin
        self.assertFalse(self.auth.can_manage_users(reviewer))
        self.assertFalse(self.auth.authorize(reviewer, "admin", LOC_A))
        self.assertFalse(self.auth.authorize(reviewer, "admin", None))

    def test_unknown_action_and_anonymous_always_denied(self):
        admin = self.auth.get_user("admin")
        self.assertFalse(self.auth.authorize(admin, "delete_everything", LOC_A))
        self.assertFalse(self.auth.authorize(None, "view", LOC_A))
        self.assertFalse(self.auth.authorize(None, "admin", None))

    def test_password_hashing_is_salted_and_verifiable(self):
        h1 = hash_password("correct horse battery staple")
        h2 = hash_password("correct horse battery staple")
        self.assertNotIn("correct horse", h1)
        self.assertTrue(h1.startswith("pbkdf2_sha256$"))
        # per-user salt -> two hashes of the same password differ
        self.assertNotEqual(h1, h2)
        self.assertTrue(verify_password("correct horse battery staple", h1))
        self.assertFalse(verify_password("wrong password", h1))
        self.assertFalse(verify_password("x", "garbage$not$a$hash"))
        with self.assertRaises(ValueError):
            hash_password("")

    def test_login_roundtrip_wrong_password_and_validate(self):
        self.assertIsNone(self.auth.login("admin", "nope"))
        self.assertIsNone(self.auth.login("ghost", "nope"))
        token = self.auth.login("owner1", "owner-pass")
        self.assertIsInstance(token, str)
        self.assertEqual(token.count("."), 1)
        user = self.auth.validate(token)
        self.assertIsNotNone(user)
        self.assertEqual(user.username, "owner1")
        # tampered token is rejected
        self.assertIsNone(self.auth.validate(token + "x"))
        self.assertIsNone(self.auth.validate(token.rsplit(".", 1)[0] + ".deadbeef"))
        self.assertIsNone(self.auth.validate(None))
        self.assertIsNone(self.auth.validate("not-a-token"))

    def test_expired_session_is_rejected(self):
        expired = AuthService(
            env=TEST_ENV, ttl=-1,
            users=_users([("owner1", "owner-pass", ["owner"], [LOC_A])]),
        )
        token = expired.login("owner1", "owner-pass")
        self.assertIsNotNone(token)
        self.assertIsNone(expired.validate(token))

    def test_no_hardcoded_secret_warns_when_env_missing(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            svc = AuthService(env={}, users=_users(
                [("admin", "admin-pass", ["admin"], [])]))
        self.assertTrue(svc.ephemeral_secret)
        self.assertTrue(any(issubclass(w.category, RuntimeWarning) for w in caught))
        self.assertTrue(any("GBP_SESSION_SECRET" in str(w.message) for w in caught))
        # with the env var set there is no warning and no ephemeral secret
        with warnings.catch_warnings(record=True) as caught2:
            warnings.simplefilter("always")
            svc2 = AuthService(env=TEST_ENV, users=_users(
                [("admin", "admin-pass", ["admin"], [])]))
        self.assertFalse(svc2.ephemeral_secret)
        self.assertEqual(caught2, [])

    def test_csrf_token_is_derived_and_differs_from_session(self):
        token = self.auth.login("admin", "admin-pass")
        csrf = self.auth.csrf_token(token)
        self.assertNotEqual(csrf, token)
        self.assertEqual(csrf, self.auth.csrf_token(token))
        self.assertNotEqual(csrf, self.auth.csrf_token("other.token"))

    def test_seeded_demo_users_from_env(self):
        svc = AuthService(env={
            "GBP_SESSION_SECRET": TEST_SECRET,
            "GBP_DEMO_ADMIN_PASSWORD": "env-admin-pass",
        })
        self.assertIsNotNone(svc.get_user("admin"))
        self.assertIsNotNone(svc.login("admin", "env-admin-pass"))
        self.assertIsNone(svc.login("admin", "demo-admin-not-a-secret"))


# ======================================================================= #
# HTTP tests -- server-side enforcement
# ======================================================================= #
class AuthHttpTestBase(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc_a = self.repo.upsert_location(
            acct, LOC_A, title="Demo Nails — Garden Grove", city="Garden Grove",
            state="CA", language="en")
        self.loc_b = self.repo.upsert_location(
            acct, LOC_B, title="Demo Nails — Westminster", city="Westminster",
            state="CA", language="vi")
        self.a_ids = str(self.loc_a)
        self.b_ids = str(self.loc_b)

        rid_a = self.repo.upsert_review(
            location_id=self.loc_a, review_name="reviews/a1", rating=5,
            comment="Loved my gel manicure, thank you so much!", reply_text=None)
        self.draft_a = self.repo.insert_reply_draft(
            rid_a, draft_text="Thanks!", status="needs_review")
        rid_b = self.repo.upsert_review(
            location_id=self.loc_b, review_name="reviews/b1", rating=5,
            comment="Great service!", reply_text=None)
        self.draft_b = self.repo.insert_reply_draft(
            rid_b, draft_text="Cam on!", status="needs_review")

        self.tmp = tempfile.TemporaryDirectory()
        self.service = ApprovalService(self.repo)

        self.auth = AuthService(
            env=TEST_ENV, ttl=3600,
            users=_users([
                ("admin", "admin-pass", ["admin"], []),
                ("owner1", "owner-pass", ["owner"], [self.a_ids]),
                ("reviewer1", "reviewer-pass", ["reviewer"], [self.a_ids]),
            ]),
        )
        self.server = create_server(self.service, port=0, auth=self.auth, rate_limit=5)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repo.close()
        self.tmp.cleanup()

    # ----------------------------------------------------------- #
    def _req(self, method, path, body=None, cookie=None, csrf=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = dict(headers or {})
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

    def _login(self, username, password):
        status, headers, body = self._req(
            "POST", "/api/login", {"username": username, "password": password})
        set_cookie = headers.get("Set-Cookie", "")
        cookie = None
        if set_cookie:
            cookie = set_cookie.split(";")[0]
        csrf = (body or {}).get("csrf_token") if isinstance(body, dict) else None
        return status, set_cookie, cookie, csrf, body


class AuthHttpTest(AuthHttpTestBase):
    # ----------------------------------------------------------- #
    def test_login_wrong_password_401_and_no_cookie(self):
        status, set_cookie, _cookie, _csrf, body = self._login("admin", "wrong")
        self.assertEqual(status, 401)
        self.assertNotIn(SESSION_COOKIE, set_cookie)
        self.assertNotIn("password", json.dumps(body).lower())

    def test_login_cookie_is_httponly_samesite_lax(self):
        status, set_cookie, cookie, csrf, body = self._login("reviewer1", "reviewer-pass")
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", set_cookie)
        # Group 8A / T22: SameSite=Lax (not Strict) so a top-level navigation
        # back into the app still carries the session; Secure stays OFF on the
        # plain-HTTP localhost demo (only GBP_COOKIE_SECURE=1 turns it on).
        self.assertIn("SameSite=Lax", set_cookie)
        self.assertNotIn("Secure", set_cookie)
        self.assertTrue(cookie.startswith(SESSION_COOKIE + "="))
        self.assertTrue(csrf)
        # the session token is never echoed in the JSON body
        self.assertNotIn(cookie.split("=", 1)[1], json.dumps(body))

    def test_unauthenticated_request_is_401(self):
        for path in ("/api/dashboard", "/api/queue", "/api/locations", "/api/audit"):
            status, _h, body = self._req("GET", path)
            self.assertEqual(status, 401, msg=path)
            self.assertEqual(body.get("error"), "unauthenticated")

    def test_session_token_not_accepted_from_query_param(self):
        _s, _sc, cookie, _csrf, _b = self._login("admin", "admin-pass")
        token = cookie.split("=", 1)[1]
        status, _h, _b = self._req("GET", f"/api/dashboard?token={token}")
        self.assertEqual(status, 400)
        status, _h, _b = self._req("GET", f"/api/dashboard?session={token}")
        self.assertEqual(status, 400)

    def test_state_changing_post_requires_matching_csrf(self):
        _s, _sc, cookie, csrf, _b = self._login("admin", "admin-pass")
        # missing header
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_a}/approve", {}, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "csrf_failed")
        # wrong token
        status, _h, _b = self._req(
            "POST", f"/api/drafts/{self.draft_a}/approve", {}, cookie=cookie,
            csrf="0" * 64)
        self.assertEqual(status, 403)
        # correct token
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_a}/approve", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual(body.get("actor"), "admin")

    def test_login_is_not_rate_limited_but_routes_are(self):
        # /api/login is exempt so a throttled client can still authenticate.
        for _ in range(8):
            status, _sc, _c, _csrf, _b = self._login("admin", "admin-pass")
            self.assertEqual(status, 200)
        status, _sc, cookie, csrf, _b = self._login("owner1", "owner-pass")
        self.assertEqual(status, 200)
        codes = []
        for _ in range(6):
            st, hdrs, _b = self._req("GET", "/api/dashboard", cookie=cookie)
            codes.append(st)
            if st == 429:
                self.assertIn("Retry-After", hdrs)
                self.assertTrue(int(hdrs["Retry-After"]) >= 1)
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5], 429)

    def test_expired_session_is_401(self):
        expired = AuthService(
            env=TEST_ENV, ttl=-1,
            users=_users([("owner1", "owner-pass", ["owner"], [self.a_ids])]))
        token = expired.login("owner1", "owner-pass")
        status, _h, body = self._req(
            "GET", "/api/dashboard", cookie=f"{SESSION_COOKIE}={token}")
        self.assertEqual(status, 401)

    # ----------------------------------------------------------- #
    # per-location authorization (server-side)
    # ----------------------------------------------------------- #
    def test_queue_is_filtered_to_the_users_locations(self):
        _s, _sc, cookie, _csrf, _b = self._login("reviewer1", "reviewer-pass")
        status, _h, payload = self._req("GET", "/api/queue", cookie=cookie)
        self.assertEqual(status, 200)
        rows = payload["items"]
        self.assertTrue(rows)
        self.assertTrue(all(str(r["location_id"]) == self.a_ids for r in rows))
        self.assertEqual(len(rows), 1)

        _s, _sc, admin_cookie, _csrf, _b = self._login("admin", "admin-pass")
        status, _h, all_payload = self._req("GET", "/api/queue", cookie=admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(all_payload["items"]), 2)

    def test_cross_location_view_denied_403(self):
        _s, _sc, cookie, _csrf, _b = self._login("reviewer1", "reviewer-pass")
        status, _h, body = self._req(
            "GET", f"/api/queue?location_id={self.b_ids}", cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "forbidden")
        # within its own location it is allowed
        status, _h, _b = self._req(
            "GET", f"/api/queue?location_id={self.a_ids}", cookie=cookie)
        self.assertEqual(status, 200)

    def test_cross_location_approve_denied_403(self):
        _s, _sc, cookie, csrf, _b = self._login("reviewer1", "reviewer-pass")
        # loc-B draft: denied even with a valid CSRF token
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_b}/approve", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "forbidden")
        # loc-B draft edit also denied
        status, _h, _b = self._req(
            "POST", f"/api/drafts/{self.draft_b}/edit", {"text": "hi"},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 403)
        # bulk-approve across locations denied
        status, _h, _b = self._req(
            "POST", "/api/drafts/bulk-approve", {"ids": [self.draft_a, self.draft_b]},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 403)
        # its own location is allowed and the actor is the session user
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_a}/approve", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual(body.get("actor"), "reviewer1")

    def test_reviewer_cannot_read_audit(self):
        _s, _sc, cookie, _csrf, _b = self._login("reviewer1", "reviewer-pass")
        status, _h, _b = self._req("GET", "/api/audit", cookie=cookie)
        self.assertEqual(status, 403)
        # with an explicit (granted) location scope it is allowed
        status, _h, _b = self._req(
            "GET", f"/api/audit?location_id={self.a_ids}", cookie=cookie)
        self.assertEqual(status, 200)
        # admin needs no scope
        _s, _sc, admin_cookie, _csrf, _b = self._login("admin", "admin-pass")
        status, _h, _b = self._req("GET", "/api/audit", cookie=admin_cookie)
        self.assertEqual(status, 200)

    def test_owner_cannot_act_on_other_location_but_can_on_own(self):
        _s, _sc, cookie, csrf, _b = self._login("owner1", "owner-pass")
        status, _h, _b = self._req(
            "GET", f"/api/queue?location_id={self.b_ids}", cookie=cookie)
        self.assertEqual(status, 403)
        status, _h, _b = self._req(
            "POST", f"/api/drafts/{self.draft_a}/reject", {"note": "no"},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)

    # ----------------------------------------------------------- #
    # input validation
    # ----------------------------------------------------------- #
    def test_malformed_id_is_rejected_400(self):
        _s, _sc, cookie, csrf, _b = self._login("admin", "admin-pass")
        status, _h, body = self._req(
            "GET", "/api/queue?location_id=bad$id;rm", cookie=cookie)
        self.assertEqual(status, 400)
        status, _h, body = self._req(
            "POST", "/api/drafts/1;drop/approve", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 400)
        self.assertIn("invalid", json.dumps(body).lower())

    def test_unknown_fields_and_oversized_text_rejected(self):
        _s, _sc, cookie, csrf, _b = self._login("admin", "admin-pass")
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_a}/approve", {"evil": "1"},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 400)
        self.assertIn("unknown field", json.dumps(body))
        # actor spoofing is an unknown field when auth is on
        status, _h, _b = self._req(
            "POST", f"/api/drafts/{self.draft_a}/approve", {"actor": "someone-else"},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 400)
        status, _h, _b = self._req(
            "POST", f"/api/drafts/{self.draft_a}/edit", {"text": "x" * 6000},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 400)

    def test_logout_clears_cookie_and_needs_csrf(self):
        _s, _sc, cookie, csrf, _b = self._login("admin", "admin-pass")
        status, _h, _b = self._req("POST", "/api/logout", {}, cookie=cookie)
        self.assertEqual(status, 403)  # CSRF required
        status, headers, body = self._req(
            "POST", "/api/logout", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertIn(SESSION_COOKIE + "=", headers.get("Set-Cookie", ""))
        self.assertIn("Max-Age=0", headers.get("Set-Cookie", ""))


class MockModeUnaffectedTest(AuthHttpTestBase):
    """``auth=None`` must keep the legacy unauthenticated MOCK behaviour."""

    def setUp(self):
        super().setUp()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.server = create_server(self.service, port=0)  # no auth
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def test_no_auth_keeps_dashboard_open(self):
        status, _h, body = self._req("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("needs_review", body)

    def test_no_auth_post_needs_no_csrf(self):
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_a}/approve", {"actor": "tester"})
        self.assertEqual(status, 200)
        self.assertEqual(body.get("actor"), "tester")


if __name__ == "__main__":
    unittest.main(verbosity=2)
