"""Group 6B hardening tests: persistent stores, revocation, IDOR, roles.

Standard library only, fully offline. HTTP tests bind 127.0.0.1 on an ephemeral
port; everything else is a direct unit test. Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v

SECURITY NOTE: MFA is **not** implemented in this build. ``mfa_required`` is
per-user readiness metadata only and the ``begin_mfa``/``verify_mfa`` hooks are
deliberate stubs -- see ``test_mfa_hooks_raise_and_we_do_not_claim_mfa``.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import sys
import tempfile
import threading
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import SESSION_COOKIE, create_server  # noqa: E402
from gbp.authz import AuthService, UserStore  # noqa: E402
from gbp.authz.auth import ROLE_PERMISSIONS  # noqa: E402
from gbp.authz.models import hash_password  # noqa: E402
from gbp.authz.store import RateLimitStore  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
TEST_SECRET = "hardening-test-secret-not-used-elsewhere"
TEST_ENV = {"GBP_SESSION_SECRET": TEST_SECRET}

LOC_A = "accounts/1001/locations/loc-001"
LOC_B = "accounts/1001/locations/loc-002"

PASSWORD = "Sup3rSecret!"  # used to prove it never leaks


def _seed_users(store: UserStore, loc_a: str) -> None:
    store.create_user("admin", hash_password(PASSWORD), ["admin"], [])
    store.create_user("owner1", hash_password(PASSWORD), ["owner"], [loc_a])
    store.create_user("reviewer1", hash_password(PASSWORD), ["reviewer"], [loc_a])


class HardeningHttpBase(unittest.TestCase):
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
            comment="Loved my gel manicure!", reply_text=None)
        self.draft_a = self.repo.insert_reply_draft(
            rid_a, draft_text="Thanks!", status="needs_review")
        rid_b = self.repo.upsert_review(
            location_id=self.loc_b, review_name="reviews/b1", rating=5,
            comment="Great service!", reply_text=None)
        self.draft_b = self.repo.insert_reply_draft(
            rid_b, draft_text="Cam on!", status="needs_review")

        # Seed users directly into the persistent store, then let AuthService
        # load them from the DB (users=None + an explicit db => load from store).
        seed = UserStore(self.repo.conn)
        _seed_users(seed, self.a_ids)

        self.service = ApprovalService(self.repo)
        self.auth = AuthService(self.repo.conn, env=TEST_ENV, ttl=3600)
        self.server = create_server(self.service, port=0, auth=self.auth,
                                    rate_limit=1000, login_limit=1000)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repo.close()

    # ----------------------------------------------------------- #
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

    def _login(self, username, password=PASSWORD):
        status, headers, body = self._req(
            "POST", "/api/login", {"username": username, "password": password})
        set_cookie = headers.get("Set-Cookie", "")
        cookie = set_cookie.split(";")[0] if set_cookie else None
        csrf = (body or {}).get("csrf_token") if isinstance(body, dict) else None
        return status, cookie, csrf, body


# ======================================================================= #
# IDOR / privilege escalation / cross-location leakage
# ======================================================================= #
class IdorAndRoleTest(HardeningHttpBase):
    def test_idor_guessed_draft_id_is_403_and_db_unchanged(self):
        _s, cookie, csrf, _b = self._login("reviewer1")
        before = self.repo.conn.execute(
            "SELECT status, version FROM reply_drafts WHERE id = ?", (self.draft_b,)
        ).fetchone()
        # A reviewer scoped to loc-A guesses a loc-B draft id and tries to act.
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_b}/approve", {},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "forbidden")
        # edit + reject + publish-guard are all denied by draft id too
        for action, payload in (
            ("edit", {"text": "leak"}), ("reject", {"note": "x"}),
            ("publish-guard", {}),
        ):
            st, _h, _b = self._req(
                "POST", f"/api/drafts/{self.draft_b}/{action}", payload,
                cookie=cookie, csrf=csrf)
            self.assertEqual(st, 403, msg=action)
        after = self.repo.conn.execute(
            "SELECT status, version FROM reply_drafts WHERE id = ?", (self.draft_b,)
        ).fetchone()
        self.assertEqual(before["status"], after["status"])
        self.assertEqual(before["version"], after["version"])
        # no approval record was written for the loc-B draft
        n = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM approval_records WHERE draft_id = ?",
            (self.draft_b,)).fetchone()["n"]
        self.assertEqual(int(n), 0)

    def test_bulk_approve_idor_is_denied(self):
        _s, cookie, csrf, _b = self._login("reviewer1")
        status, _h, body = self._req(
            "POST", "/api/drafts/bulk-approve",
            {"ids": [self.draft_a, self.draft_b]}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "forbidden")

    def test_reviewer_cannot_hit_admin_only_actions(self):
        _s, cookie, _csrf, _b = self._login("reviewer1")
        # /api/audit without a location scope is admin-only
        self.assertEqual(self._req("GET", "/api/audit", cookie=cookie)[0], 403)
        # /api/usage (global billing, no location column) is admin-only
        self.assertEqual(self._req("GET", "/api/usage", cookie=cookie)[0], 403)
        _s, admin_cookie, _csrf, _b = self._login("admin")
        self.assertEqual(self._req("GET", "/api/audit", cookie=admin_cookie)[0], 200)
        self.assertEqual(self._req("GET", "/api/usage", cookie=admin_cookie)[0], 200)

    def test_role_matrix_is_re_read_from_the_db(self):
        # The reviewer's DB roles are re-read on every access, so a DB change
        # takes effect immediately (no stale in-process cache).
        self.assertFalse(self.auth.authorize(
            self.auth.get_user("reviewer1"), "admin", None))
        self.assertFalse(self.auth.can_manage_users(self.auth.get_user("reviewer1")))
        UserStore(self.repo.conn).update_roles("reviewer1", ["admin"])
        promoted = self.auth.get_user("reviewer1")
        self.assertIn("admin", promoted.roles)
        self.assertTrue(self.auth.authorize(promoted, "admin", None))
        # and the static matrix still matches the documented contract
        self.assertEqual(ROLE_PERMISSIONS["reviewer"],
                         frozenset({"view", "edit", "approve", "reject"}))

    def test_cross_location_filter_leakage_is_blocked(self):
        _s, cookie, _csrf, _b = self._login("reviewer1")
        # queue
        st, _h, payload = self._req("GET", "/api/queue", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertTrue(payload["items"])
        self.assertTrue(all(str(r["location_id"]) == self.a_ids
                            for r in payload["items"]))
        # locations
        st, _h, locs = self._req("GET", "/api/locations", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertTrue(all(str(r["id"]) == self.a_ids for r in locs))
        # locations/status
        st, _h, sts = self._req("GET", "/api/locations/status", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertTrue(all(str(r["location_id"]) == self.a_ids for r in sts))
        # an explicit loc-B filter is denied outright
        self.assertEqual(
            self._req("GET", f"/api/queue?location_id={self.b_ids}",
                      cookie=cookie)[0], 403)

    def test_inactive_user_is_403_everywhere_and_at_login(self):
        _s, cookie, csrf, _b = self._login("reviewer1")
        UserStore(self.repo.conn).set_active("reviewer1", False)
        st, _h, body = self._req("GET", "/api/dashboard", cookie=cookie)
        self.assertEqual(st, 403)
        self.assertEqual(body.get("error"), "inactive")
        # a fresh login for a deactivated account is refused as well
        st2, cookie2, _csrf2, body2 = self._login("reviewer1")
        self.assertEqual(st2, 403)
        self.assertIsNone(cookie2)
        self.assertEqual(body2.get("error"), "inactive")
        # authorize() also denies the inactive user everywhere
        self.assertFalse(self.auth.authorize(
            self.auth.get_user("reviewer1"), "view", self.a_ids))


# ======================================================================= #
# session lifecycle: logout / revoke_all / expiry
# ======================================================================= #
class SessionRevocationTest(HardeningHttpBase):
    def test_logout_revokes_token_even_though_hmac_is_valid(self):
        _s, cookie, csrf, _b = self._login("reviewer1")
        token = cookie.split("=", 1)[1]
        # the HMAC still verifies after logout...
        self.assertEqual(self._req("GET", "/api/dashboard", cookie=cookie)[0], 200)
        st, _h, _b = self._req("POST", "/api/logout", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(st, 200)
        self.assertIsNotNone(self.auth._payload(token))  # signature still valid
        # ...but the persisted session is revoked -> 401
        st, _h, body = self._req("GET", "/api/dashboard", cookie=cookie)
        self.assertEqual(st, 401)
        self.assertEqual(body.get("error"), "unauthenticated")

    def test_revoke_all_invalidates_every_session(self):
        _s, c1, _x, _b = self._login("reviewer1")
        _s, c2, _x, _b = self._login("reviewer1")
        self.assertNotEqual(c1, c2)
        self.assertEqual(self._req("GET", "/api/dashboard", cookie=c1)[0], 200)
        self.assertEqual(self._req("GET", "/api/dashboard", cookie=c2)[0], 200)
        self.assertGreaterEqual(self.auth.revoke_all("reviewer1"), 2)
        self.assertEqual(self._req("GET", "/api/dashboard", cookie=c1)[0], 401)
        self.assertEqual(self._req("GET", "/api/dashboard", cookie=c2)[0], 401)

    def test_expired_session_is_401(self):
        _s, cookie, _csrf, _b = self._login("reviewer1")
        self.assertEqual(self._req("GET", "/api/dashboard", cookie=cookie)[0], 200)
        # expire the persisted row (the token's own exp is still in the future)
        self.repo.conn.execute(
            "UPDATE sessions SET expires_at = 1 WHERE username = ?", ("reviewer1",))
        self.repo.conn.commit()
        st, _h, body = self._req("GET", "/api/dashboard", cookie=cookie)
        self.assertEqual(st, 401)
        self.assertEqual(body.get("error"), "unauthenticated")


# ======================================================================= #
# persistent rate limiting (survives a restart / a second worker)
# ======================================================================= #
class PersistentRateLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "auth.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_counters_are_persisted(self):
        store = RateLimitStore(self.db_path)
        results = [store.hit("route:u|1.2.3.4", 60, 3) for _ in range(4)]
        self.assertEqual([r[0] for r in results], [True, True, True, False])
        self.assertEqual(results[0][1], 2)  # remaining after the first hit
        self.assertGreaterEqual(results[-1][2], 1)  # retry_after

    def test_limit_persists_across_a_new_authenticatorservice(self):
        # Service #1 exhausts the window, then a brand-new AuthService is built
        # on the SAME db (simulating a restart / a second worker process).
        s1 = AuthService(
            self.db_path, env=TEST_ENV, ttl=3600,
            users=[_mk_user("reviewer1", ["reviewer"], [LOC_A])],
        )
        bucket = "route:reviewer1|127.0.0.1"
        for _ in range(5):
            allowed, _rem, _ra = s1.hit_rate_limit(bucket, 60, 5)
            self.assertTrue(allowed)
        allowed, _rem, retry = s1.hit_rate_limit(bucket, 60, 5)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry, 1)

        s2 = AuthService(self.db_path, env=TEST_ENV, ttl=3600)  # users from db
        allowed, _rem, retry = s2.hit_rate_limit(bucket, 60, 5)
        self.assertFalse(allowed, "a second worker must see the same counters")
        self.assertGreaterEqual(retry, 1)

    def test_server_throttle_survives_a_restart(self):
        # End-to-end: exhaust the HTTP throttle, rebuild the whole server on the
        # same db, and confirm the caller is still limited (429).
        repo = Repository(":memory:")
        repo.init_db(SCHEMA_PATH)
        acct = repo.upsert_account("accounts/1001")
        loc = repo.upsert_location(acct, LOC_A, title="A", city="C", state="CA",
                                   language="en")
        UserStore(repo.conn).create_user(
            "reviewer1", hash_password(PASSWORD), ["reviewer"], [str(loc)])
        service = ApprovalService(repo)

        def run_server():
            auth = AuthService(repo.conn, env=TEST_ENV, ttl=3600)
            # A long window keeps now() in the same bucket for the whole test,
            # so the restart assertion cannot be defeated by a window rollover.
            srv = create_server(service, port=0, auth=auth, rate_limit=3,
                                rate_window=3600.0, login_limit=1000)
            host, port = srv.server_address[:2]
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            return srv, host, port, th

        srv, _host, port, th = run_server()
        try:
            # log in once to capture the session cookie
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"username": "reviewer1",
                               "password": PASSWORD}).encode()
            conn.request("POST", "/api/login", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            set_cookie = resp.getheader("Set-Cookie") or ""
            resp.read()
            conn.close()
            cookie = set_cookie.split(";")[0]
            codes = []
            for _ in range(4):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", "/api/dashboard", headers={"Cookie": cookie})
                r = c.getresponse()
                codes.append(r.status)
                r.read()
                c.close()
            self.assertEqual(codes[:3], [200, 200, 200])
            self.assertEqual(codes[3], 429)
        finally:
            srv.shutdown()
            srv.server_close()
            th.join(timeout=5)

        # restart: new AuthService + new server on the same db
        srv2, _h2, port2, th2 = run_server()
        try:
            c = http.client.HTTPConnection("127.0.0.1", port2, timeout=5)
            c.request("GET", "/api/dashboard", headers={"Cookie": cookie})
            r = c.getresponse()
            status = r.status
            retry = r.getheader("Retry-After")
            r.read()
            c.close()
            self.assertEqual(status, 429, "throttle must survive a restart")
            self.assertIsNotNone(retry)
        finally:
            srv2.shutdown()
            srv2.server_close()
            th2.join(timeout=5)
            repo.close()


# ======================================================================= #
# MFA readiness + secret hygiene
# ======================================================================= #
def _mk_user(username, roles, locations, *, active=True, mfa_required=False):
    from gbp.authz.models import User
    return User(username=username, password_hash=hash_password(PASSWORD),
                roles=roles, locations=locations, active=active,
                mfa_required=mfa_required)


class MfaReadinessTest(unittest.TestCase):
    def setUp(self):
        self.auth = AuthService(
            env=TEST_ENV, ttl=3600,
            users=[_mk_user("reviewer1", ["reviewer"], [LOC_A],
                            mfa_required=True)])

    def test_mfa_hooks_raise_and_we_do_not_claim_mfa(self):
        user = self.auth.get_user("reviewer1")
        # mfa_required is DATA ONLY -- it does not gate anything today.
        self.assertTrue(user.mfa_required)
        with self.assertRaises(NotImplementedError) as ctx:
            self.auth.begin_mfa(user)
        self.assertIn("not implemented", str(ctx.exception).lower())
        with self.assertRaises(NotImplementedError) as ctx2:
            self.auth.verify_mfa(user, "123456")
        self.assertIn("not implemented", str(ctx2.exception).lower())
        # A user with mfa_required can still use password-only login, which is
        # exactly the point: we do NOT claim MFA is enforced.
        self.assertIsNotNone(self.auth.login("reviewer1", PASSWORD))


class SecretHygieneTest(HardeningHttpBase):
    def test_password_never_appears_in_errors_or_logs(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        root = logging.getLogger("gbp")
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            # wrong password -> generic 401, no password echoed
            st, _h, body = self._req(
                "POST", "/api/login",
                {"username": "reviewer1", "password": PASSWORD + "-wrong"})
            self.assertEqual(st, 401)
            text = json.dumps(body).lower()
            self.assertNotIn(PASSWORD.lower(), text)
            self.assertNotIn("password", text)
            # a *successful* login must not leak it either
            _s, cookie, _csrf, body2 = self._login("reviewer1")
            self.assertNotIn(PASSWORD.lower(), json.dumps(body2).lower())
            # and nothing in the logs
            joined = "\n".join(records)
            self.assertNotIn(PASSWORD, joined)
            self.assertNotIn(PASSWORD + "-wrong", joined)
        finally:
            root.removeHandler(handler)


if __name__ == "__main__":
    unittest.main(verbosity=2)
