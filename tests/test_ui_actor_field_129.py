"""Issue #129 -- hotfix: UI draft actions 400 in auth mode (the ``actor`` field).

The shipped UI (``web/app.js``) sends ``actor: actor()`` in EVERY draft-action
body (edit / approve / reject / publish / bulk-approve). In AUTH mode the
recorded actor IS the authenticated session user, never a client-supplied value,
so the server used to reject the field outright with 400 "unknown field(s):
actor" -- which broke every draft action from the live dashboard.

The fix (``src/gbp/api/server.py``, server-side only) treats a client-supplied
``actor`` as KNOWN-BUT-IGNORED on the draft-action route and bulk-approve **in
auth mode only**: the field is dropped before the strict unknown-field check,
so the request is processed while the recorded actor stays the session user (a
spoofed actor has zero effect). Any OTHER unknown field still 400s, and no-auth
(demo) mode is unchanged.

Fully OFFLINE: fake transports only (no socket beyond the loopback test
server), no credential is real and ``GBP_LIVE`` is set only inside a patched
environment.

Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest \\
        tests.test_ui_actor_field_129
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
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
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
TEST_SECRET = "gbp-ui-actor-field-secret-not-used-elsewhere"
TEST_ENV = {"GBP_SESSION_SECRET": TEST_SECRET}
VAULT_KEY = "unit-test-vault-key-material-0123456789abcdef"
CANONICAL_REDIRECT = "https://gbp.adzdigital.com/api/google/callback"

ACCOUNT = "accounts/1001"
LOC_A = "accounts/1001/locations/loc-001"     # the tenant anchor (fixture row)
SHORT_1 = "locations/777001"
FULL_1 = "accounts/1001/locations/777001"
REVIEW_NAME = f"{FULL_1}/reviews/r1"
REPLY_URL = f"https://mybusiness.googleapis.com/v4/{REVIEW_NAME}/reply"

PASSWORD = "UiActorSecret!"

APPROVED_TEXT = "Thank you so much for visiting — we hope to see you again!"


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


def api_transport() -> MockTransport:
    """A fake GBP API: one live location whose review can be replied to."""
    transport = MockTransport()
    transport.register(
        "GET", "https://mybusiness.googleapis.com/v1/accounts",
        {"accounts": [{"name": ACCOUNT, "accountName": "Acme Group",
                       "type": "LOCATION_GROUP"}]})
    transport.register(
        "GET", f"https://mybusiness.googleapis.com/v1/{ACCOUNT}/locations",
        {"locations": [{"name": SHORT_1, "title": "Live Nails A",
                        "storefrontAddress": {"postalCode": "94103",
                                              "locality": "Garden Grove",
                                              "administrativeArea": "CA"}}]})
    transport.register(
        "GET", f"https://mybusiness.googleapis.com/v4/{FULL_1}/reviews",
        {"reviews": [{
            "name": REVIEW_NAME, "rating": 5, "comment": "Loved it",
            "createTime": "2026-10-19T09:00:00Z",
            "updateTime": "2026-10-20T10:00:00Z",
            "reviewer": {"displayName": "Ada"},
        }]})
    # The ONE write: the reply PUT.
    transport.register(
        "PUT", REPLY_URL,
        {"name": REPLY_URL, "comment": APPROVED_TEXT,
         "updateTime": "2026-10-24T12:00:00Z"}, status=200)
    return transport


class AuthUiActorBase(unittest.TestCase):
    """AUTH mode: a live location, a complete brand, a fake GBP transport."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gbp-ui-actor-")
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account(ACCOUNT, tenant_id=LOC_A)
        self.loc_live = self.repo.upsert_location(
            acct, SHORT_1, title="Live Nails A", tenant_id=LOC_A, source="live")
        self.loc_fixture = self.repo.upsert_location(
            acct, "accounts/1001/locations/fixture", title="Fixture")
        # A COMPLETE brand profile with auto-publish eligibility, so 4-5 star
        # drafts are publishable (publish-guard returns 200, not 423).
        self.repo.conn.execute(
            "INSERT INTO brand_profiles(location_id, business_name, city,"
            " language, services_json, seo_keywords_json, auto_publish_allowed)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            (self.loc_live, "Live Nails A", "Garden Grove", "en",
             json.dumps(["Manicure"]), json.dumps(["Nail Salon"])))
        self.repo.conn.commit()

        self.review_id = self.repo.upsert_review(
            self.loc_live, REVIEW_NAME, rating=5, comment="Loved it")
        self.draft_id = self.repo.insert_reply_draft(
            self.review_id, draft_text=APPROVED_TEXT, status="draft")

        seed = UserStore(self.repo.conn)
        seed.create_user("admin", hash_password(PASSWORD), ["admin"], [])
        seed.create_user("owner1", hash_password(PASSWORD), ["owner"],
                         [str(self.loc_live)])

        self.service = ApprovalService(self.repo)
        self.auth = AuthService(self.repo.conn, env=TEST_ENV, ttl=3600)
        self.token_transport = token_transport()
        self.api_transport = api_transport()
        self.wiring = GoogleConnectWiring(
            token_transport=self.token_transport, api_transport=self.api_transport)
        self.server = create_server(
            self.service, port=0, auth=self.auth, rate_limit=100000,
            login_limit=100000, google_oauth=self.wiring)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repo.close()

    # -- helpers --------------------------------------------------------- #
    def _mk_draft(self, loc_id, status="draft", rating=5, review_name=None,
                  comment="Great!"):
        """Insert a FRESH review + draft; return (review_id, draft_id)."""
        rid = self.repo.upsert_review(
            loc_id,
            review_name or f"accounts/1001/locations/x/reviews/{os.urandom(8).hex()}",
            rating=rating, comment=comment)
        did = self.repo.insert_reply_draft(
            rid, draft_text=APPROVED_TEXT, status=status)
        return rid, did

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

    def _save_token(self):
        TokenVault(self.tmp, VAULT_KEY).save(LOC_A, Token(
            access_token="access-token-probe", refresh_token="refresh-token-probe",
            expires_at=1e12, scopes=["scope/a"]))

    def _puts(self):
        return [c for c in self.api_transport.calls if c["method"] == "PUT"]

    def _approvals(self, draft_id, action=None):
        sql = "SELECT action, actor FROM approval_records WHERE draft_id = ?"
        params = [draft_id]
        if action:
            sql += " AND action = ?"
            params.append(action)
        return [dict(r) for r in self.repo.conn.execute(sql, params).fetchall()]


# ===================================================================== #
# 1. Auth mode: UI-shaped bodies (WITH actor) succeed for every action
# ===================================================================== #
class UiShapedBodyTests(AuthUiActorBase):
    def test_edit_with_actor_succeeds_and_keeps_the_session_actor(self):
        _s, cookie, csrf = self._login("admin")
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_id}/edit",
            {"text": "edited via UI", "actor": "UI Box"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["draft_text"], "edited via UI")
        self.assertEqual(body["actor"], "admin")

    def test_approve_with_actor_succeeds(self):
        _s, cookie, csrf = self._login("admin")
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_id}/approve",
            {"actor": "UI Box"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["actor"], "admin")

    def test_reject_with_actor_succeeds(self):
        _s, cookie, csrf = self._login("admin")
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_id}/reject",
            {"actor": "UI Box", "note": "no thanks"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "rejected")

    def test_publish_guard_with_actor_succeeds(self):
        # An approved, eligible draft with no reply -> the guard allows it.
        self.service.approve(self.draft_id, "admin")
        _s, cookie, csrf = self._login("admin")
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_id}/publish-guard",
            {"actor": "UI Box"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["publishable"])

    def test_bulk_approve_with_actor_succeeds(self):
        _s, cookie, csrf = self._login("admin")
        status, _h, body = self._req(
            "POST", "/api/drafts/bulk-approve",
            {"ids": [self.draft_id], "actor": "UI Box"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, body)
        self.assertEqual([a["draft_id"] for a in body["approved"]],
                         [self.draft_id])

    def test_publish_with_actor_succeeds_and_posts_one_reply(self):
        self.service.approve(self.draft_id, "admin")
        self._save_token()
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            _s, cookie, csrf = self._login("admin")
            status, _h, body = self._req(
                "POST", f"/api/drafts/{self.draft_id}/publish",
                {"actor": "UI Box"}, cookie=cookie, csrf=csrf)
            self.assertEqual(status, 200, body)
            self.assertTrue(body["ok"], body)
            self.assertEqual(body["state"], "sent")
            puts = self._puts()
            self.assertEqual(len(puts), 1)
            self.assertEqual(puts[0]["url"], REPLY_URL)


# ===================================================================== #
# 2. Auth mode: a spoofed actor has ZERO effect
# ===================================================================== #
class SpoofedActorTests(AuthUiActorBase):
    def test_body_actor_naming_another_user_is_ignored(self):
        # ''owner1'' is a real, but DIFFERENT user. The request runs as the
        # SESSION user (admin) and every record keeps the session actor.
        _s, cookie, csrf = self._login("admin")
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_id}/approve",
            {"actor": "owner1"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["actor"], "admin")
        row = self._approvals(self.draft_id, "approve")[-1]
        self.assertEqual(row["actor"], "admin")
        # The audit trail carries the session actor too, never the spoof.
        audits = [dict(r) for r in self.repo.conn.execute(
            "SELECT actor FROM audit_logs WHERE entity = 'reply_draft'"
            " AND entity_id = ?", (str(self.draft_id),)).fetchall()]
        self.assertTrue(audits)
        self.assertTrue(all(a["actor"] == "admin" for a in audits))

    def test_spoofing_does_not_place_a_foreign_action(self):
        # A scoped user (owner1) cannot use a spoofed ``actor`` to act on a
        # draft outside its scope: the field is ignored and RBAC still denies.
        _r, foreign = self._mk_draft(self.loc_fixture)
        _s, cookie, csrf = self._login("owner1")
        status, _h, _b = self._req(
            "POST", f"/api/drafts/{foreign}/approve",
            {"actor": "admin"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 403)
        row = self.repo.conn.execute(
            "SELECT status FROM reply_drafts WHERE id = ?", (foreign,)).fetchone()
        self.assertNotEqual(row["status"], "approved")


# ===================================================================== #
# 3. Auth mode: a genuinely unknown field still 400s
# ===================================================================== #
class UnknownFieldStillRejectedTests(AuthUiActorBase):
    def test_unknown_field_on_the_draft_action_route_still_400(self):
        _s, cookie, csrf = self._login("admin")
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_id}/edit",
            {"text": "x", "actor": "UI Box", "bogus": 1},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 400, body)
        self.assertIn("bogus", json.dumps(body))
        self.assertNotIn("actor", json.dumps(body))

    def test_unknown_field_on_bulk_approve_still_400(self):
        _s, cookie, csrf = self._login("admin")
        status, _h, body = self._req(
            "POST", "/api/drafts/bulk-approve",
            {"ids": [self.draft_id], "actor": "UI Box", "bogus": 1},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 400, body)
        self.assertIn("bogus", json.dumps(body))


# ===================================================================== #
# 4. No-auth (demo) mode is UNCHANGED: actor is accepted and USED
# ===================================================================== #
class NoAuthModeUnchangedTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account(ACCOUNT)
        self.loc = self.repo.upsert_location(acct, "accounts/1001/locations/loc-001",
                                             title="Demo Nails A")
        self.review_id = self.repo.upsert_review(
            self.loc, "reviews/n1", rating=5, comment="Nice!")
        self.draft_id = self.repo.insert_reply_draft(
            self.review_id, draft_text="Thank you!", status="needs_review")
        self.service = ApprovalService(self.repo)
        # NO auth service -> legacy MOCK mode.
        self.server = create_server(self.service, port=0, rate_limit=100000)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repo.close()

    def _post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode("utf-8")
        hdrs = {"Content-Type": "application/json",
                "Content-Length": str(len(payload))}
        try:
            conn.request("POST", path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else None)
        finally:
            conn.close()

    def test_no_auth_mode_still_accepts_and_uses_the_body_actor(self):
        status, body = self._post(
            f"/api/drafts/{self.draft_id}/approve", {"actor": "demo-user"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["actor"], "demo-user")
        row = self.repo.conn.execute(
            "SELECT actor FROM approval_records WHERE draft_id = ?",
            (self.draft_id,)).fetchone()
        self.assertEqual(row["actor"], "demo-user")


if __name__ == "__main__":
    unittest.main()
