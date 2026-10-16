"""Group 8B / T25 -- authentication hardening.

Covers the brute-force controls (per-source lockout, escalating duration,
account-wide throttle ONLY across several distinct addresses, persistence
across a restart, expiry, recovery), the password policy, and a review of
session/CSRF/cookie/no-leak behaviour.

Anti-DoS is the load-bearing property: one attacker on one address must never
be able to lock a legitimate admin out.

Offline, stdlib only. Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests
"""

from __future__ import annotations

import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from gbp.api.server import SESSION_COOKIE, create_server  # noqa: E402
from gbp.authz import AuthService, UserStore  # noqa: E402
from gbp.authz.models import hash_password  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.security.policy import (  # noqa: E402
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    password_is_acceptable,
    password_problems,
    validate_password,
)
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
TEST_ENV = {"GBP_SESSION_SECRET": "t25-test-secret-not-used-anywhere-else"}
PASSWORD = "GardenGrove-Nails-2026!"      # policy-compliant, never leaked
OTHER = "Different-Owner-2026-x"


class _Clock:
    def __init__(self, start=1_800_000_000):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LockoutServiceTests(unittest.TestCase):
    """Direct AuthService tests (explicit source addresses)."""

    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        store = UserStore(self.repo.conn)
        store.create_user("admin", hash_password(PASSWORD), ["admin"], [])
        self.clock = _Clock()
        self.slept: list = []
        self.svc = AuthService(
            self.repo.conn, env=TEST_ENV, now_fn=self.clock,
            users=[], sleep_fn=self.slept.append,
        )

    def tearDown(self):
        self.repo.close()

    def _fail(self, n, ip="10.0.0.1", user="admin"):
        out = None
        for _ in range(n):
            out = self.svc.login_with_status(user, "wrong-password", ip=ip)
        return out

    # --- per-source lock -------------------------------------------------
    def test_source_locks_only_after_the_threshold(self):
        for i in range(1, self.svc.lockout_threshold):
            r = self.svc.login_with_status("admin", "bad", ip="10.0.0.1")
            self.assertFalse(r["locked"], f"locked too early at attempt {i}")
            self.assertEqual(r["failures"], i)
        r = self._fail(1, ip="10.0.0.1")          # crosses the threshold
        self.assertTrue(r["locked"])
        self.assertGreaterEqual(r["retry_after"], 1)

    def test_lock_escalates_and_is_capped(self):
        self._fail(self.svc.lockout_threshold, ip="10.0.0.1")
        first = self.svc.lock_retry_after("admin", "10.0.0.1")
        self._fail(2, ip="10.0.0.1")              # two more failures
        second = self.svc.lock_retry_after("admin", "10.0.0.1")
        self.assertGreater(second, first)
        self.assertLessEqual(second, self.svc.lockout_max_seconds)

    def test_locked_caller_cannot_even_try_the_right_password(self):
        self._fail(self.svc.lockout_threshold, ip="10.0.0.1")
        r = self.svc.login_with_status("admin", PASSWORD, ip="10.0.0.1")
        self.assertTrue(r["locked"])
        self.assertIsNone(r["token"])

    # --- anti-DoS: one address can never lock the account ----------------
    def test_one_address_cannot_lock_the_account_for_others(self):
        self._fail(50, ip="10.0.0.1")
        self.assertTrue(self.svc.login_with_status(
            "admin", "bad", ip="10.0.0.1")["locked"])
        # A different address is completely unaffected and can log in.
        ok = self.svc.login_with_status("admin", PASSWORD, ip="10.0.0.2")
        self.assertTrue(ok["ok"], "a second source must not be collateral damage")
        self.assertIsNotNone(ok["token"])
        status = self.svc.lock_status("admin", "10.0.0.2")
        self.assertIsNone(status["account_locked_until"])

    def test_account_lock_requires_several_distinct_addresses(self):
        # 19 failures spread over 3 addresses -> below the account threshold
        for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
            self._fail(6, ip=ip)
        total, distinct = self.svc.login_guard.account_failures(
            "admin", self.svc.failure_window_seconds)
        self.assertGreaterEqual(distinct, 3)
        self.assertLess(total, self.svc.global_lockout_threshold)
        self.assertIsNone(self.svc.login_guard.locked_until("user", "admin"))

        # push the total over the threshold -> the account IS throttled
        self._fail(self.svc.global_lockout_threshold - total, ip="10.0.0.4")
        self.assertIsNotNone(self.svc.login_guard.locked_until("user", "admin"))
        self.assertTrue(self.svc.login_with_status(
            "admin", PASSWORD, ip="10.0.0.9")["locked"])

    # --- persistence / expiry / recovery --------------------------------
    def test_lock_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "auth.db")
            first = AuthService(db, env=TEST_ENV, now_fn=self.clock,
                                users=[], sleep_fn=lambda _s: None)
            store = UserStore(first.conn)
            store.create_user("admin", hash_password(PASSWORD), ["admin"], [])
            for _ in range(first.lockout_threshold):
                first.login_with_status("admin", "bad", ip="10.0.0.1")
            self.assertTrue(first.lock_retry_after("admin", "10.0.0.1") > 0)

            # A brand new process/service on the same DB still sees the lock.
            second = AuthService(db, env=TEST_ENV, now_fn=self.clock,
                                 users=[], sleep_fn=lambda _s: None)
            self.assertGreater(second.lock_retry_after("admin", "10.0.0.1"), 0)
            self.assertTrue(second.login_with_status(
                "admin", PASSWORD, ip="10.0.0.1")["locked"])

    def test_login_succeeds_once_the_lock_expires(self):
        self._fail(self.svc.lockout_threshold, ip="10.0.0.1")
        self.assertEqual(self.svc.login_with_status(
            "admin", PASSWORD, ip="10.0.0.1")["ok"], False)
        self.clock.advance(self.svc.lockout_max_seconds + 1)
        r = self.svc.login_with_status("admin", PASSWORD, ip="10.0.0.1")
        self.assertTrue(r["ok"])
        self.assertIsNotNone(r["token"])

    def test_success_clears_only_that_source(self):
        self._fail(2, ip="10.0.0.1")
        self._fail(2, ip="10.0.0.2")
        self.assertTrue(self.svc.login_with_status(
            "admin", PASSWORD, ip="10.0.0.1")["ok"])
        self.assertEqual(self.svc.login_guard.failures(
            "user_ip", "admin|10.0.0.1", self.svc.failure_window_seconds), 0)
        self.assertEqual(self.svc.login_guard.failures(
            "user_ip", "admin|10.0.0.2", self.svc.failure_window_seconds), 2)

    def test_unlock_user_clears_every_counter_and_lock(self):
        self._fail(self.svc.lockout_threshold, ip="10.0.0.1")
        self.assertTrue(self.svc.login_with_status(
            "admin", PASSWORD, ip="10.0.0.1")["locked"])
        cleared = self.svc.unlock_user("admin")
        self.assertGreaterEqual(cleared, 1)
        self.assertEqual(self.svc.lock_retry_after("admin", "10.0.0.1"), 0)
        self.assertTrue(self.svc.login_with_status(
            "admin", PASSWORD, ip="10.0.0.1")["ok"])

    def test_backoff_is_progressive_and_capped(self):
        self.assertEqual(self.svc.backoff_delay(1), 0.0)
        self.assertEqual(self.svc.backoff_delay(self.svc.backoff_after), 0.0)
        first = self.svc.backoff_delay(self.svc.backoff_after + 1)
        self.assertGreater(first, 0.0)
        self.assertGreater(self.svc.backoff_delay(self.svc.backoff_after + 3),
                           first)
        self.assertLessEqual(self.svc.backoff_delay(50),
                             self.svc.max_backoff_seconds)
        # ... and the delays are actually applied on real failed attempts,
        # strictly increasing, never above the cap.
        for ip in ("10.0.0.7", "10.0.0.8"):
            self.slept.clear()
            for _ in range(self.svc.backoff_after + 3):
                self.svc.login_with_status("admin", "bad", ip=ip)
            self.assertTrue(self.slept, "the delay must be applied")
            self.assertEqual(self.slept, sorted(self.slept))
            self.assertLessEqual(max(self.slept),
                                 self.svc.max_backoff_seconds)

    def test_lock_status_never_exposes_secrets(self):
        self._fail(self.svc.lockout_threshold, ip="10.0.0.1")
        blob = json.dumps(self.svc.lock_status("admin", "10.0.0.1"))
        self.assertNotIn(PASSWORD, blob)
        self.assertNotIn("pbkdf2", blob)

    def test_mfa_is_still_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.svc.begin_mfa(None)
        with self.assertRaises(NotImplementedError):
            self.svc.verify_mfa(None, "123456")


class PasswordPolicyTests(unittest.TestCase):
    def test_short_password_is_rejected(self):
        problems = password_problems("Ab1!xy", username="admin")
        self.assertTrue(any("at least" in p for p in problems))
        self.assertFalse(password_is_acceptable("Ab1!xy", username="admin"))

    def test_well_known_password_is_rejected(self):
        self.assertFalse(password_is_acceptable("password1234"))
        self.assertFalse(password_is_acceptable("qwerty123456"))

    def test_straight_runs_and_single_character_rejected(self):
        self.assertFalse(password_is_acceptable("abcd-Efgh-1234"))
        self.assertFalse(password_is_acceptable("aaaaaaaaaaaaaa"))

    def test_name_derived_password_is_rejected(self):
        self.assertFalse(password_is_acceptable(
            "GardenGroveAdmin-9", username="admin",
            business_name="Garden Grove Nails"))

    def test_whitespace_padding_is_rejected(self):
        self.assertFalse(password_is_acceptable(" Good-Password-2026 "))

    def test_strong_password_is_accepted(self):
        self.assertTrue(password_is_acceptable(PASSWORD, username="admin"))

    def test_policy_error_never_echoes_the_password(self):
        secret_looking = "hunter2-hunter2-hunter2"
        try:
            validate_password(secret_looking, username="hunter2")
        except PasswordPolicyError as exc:
            self.assertNotIn(secret_looking, str(exc))
            return
        self.fail("expected PasswordPolicyError")

    def test_min_length_is_documented_and_used(self):
        self.assertGreaterEqual(MIN_PASSWORD_LENGTH, 12)
        short = "a" * (MIN_PASSWORD_LENGTH - 1)
        self.assertFalse(password_is_acceptable(short))


class AuthHTTPTests(unittest.TestCase):
    """End-to-end through the real HTTP server."""

    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-001", title="Demo Nails",
            city="Garden Grove", state="CA", language="en")
        store = UserStore(self.repo.conn)
        store.create_user("admin", hash_password(PASSWORD), ["admin"], [])
        store.create_user("gone", hash_password(OTHER), ["reviewer"],
                          [str(self.loc)], active=0)
        self.clock = _Clock()
        self.service = ApprovalService(self.repo)
        self.servers: list = []

    def tearDown(self):
        for key in ("GBP_COOKIE_SECURE", "GBP_TRUSTED_PROXIES"):
            os.environ.pop(key, None)
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.repo.close()

    def _env(self, *, trusted_proxy=False, cookie_secure=False):
        """Set the process env for the WHOLE test.

        The server reads these per request (client IP, cookie flags), so they
        must stay set while the requests are made -- not just while the server
        object is built.
        """
        if trusted_proxy:
            os.environ["GBP_TRUSTED_PROXIES"] = "127.0.0.1"
        else:
            os.environ.pop("GBP_TRUSTED_PROXIES", None)
        if cookie_secure:
            os.environ["GBP_COOKIE_SECURE"] = "1"
        else:
            os.environ.pop("GBP_COOKIE_SECURE", None)

    def _start(self, **_ignored):
        auth = AuthService(self.repo.conn, env=TEST_ENV, users=[],
                           now_fn=self.clock, sleep_fn=lambda _s: None)
        server = create_server(self.service, port=0, auth=auth,
                               rate_limit=1000, login_limit=1000)
        host, port = server.server_address[:2]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return port

    def _req(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(payload))
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            return (resp.status, dict(resp.getheaders()),
                    json.loads(raw.decode("utf-8")) if raw else None)
        finally:
            conn.close()

    def _login(self, port, username="admin", password=PASSWORD, xff=None):
        headers = {"X-Forwarded-For": xff} if xff else None
        status, hdrs, body = self._req(
            port, "POST", "/api/login",
            {"username": username, "password": password}, headers=headers)
        set_cookie = hdrs.get("Set-Cookie", "")
        cookie = set_cookie.split(";")[0] if set_cookie else None
        csrf = (body or {}).get("csrf_token") if isinstance(body, dict) else None
        return status, set_cookie, cookie, csrf, body

    # --- HTTP lock behaviour ---------------------------------------------
    def test_locked_source_gets_429_with_retry_after(self):
        port = self._start()
        # Attempts below the threshold are ordinary credential failures (401);
        # the attempt that crosses the threshold already reports the lock.
        for _ in range(4):
            self.assertEqual(self._login(port, password="bad")[0], 401)
        status, hdrs, body = self._req(
            port, "POST", "/api/login",
            {"username": "admin", "password": "bad"})
        self.assertEqual(status, 429)
        self.assertEqual(body.get("error"), "rate_limited")
        self.assertGreaterEqual(int(hdrs.get("Retry-After", "0")), 1)
        # even the correct password is refused while locked
        status2, hdrs2, _b2 = self._req(
            port, "POST", "/api/login",
            {"username": "admin", "password": PASSWORD})
        self.assertEqual(status2, 429)
        self.assertGreaterEqual(int(hdrs2.get("Retry-After", "0")), 1)

    def test_other_source_is_not_affected_behind_a_trusted_proxy(self):
        self._env(trusted_proxy=True)
        port = self._start()
        for _ in range(6):
            self._login(port, password="bad", xff="10.0.0.9")
        self.assertEqual(self._login(port, password="bad", xff="10.0.0.9")[0], 429)
        # a different client behind the same proxy still logs in fine
        status, _sc, cookie, csrf, body = self._login(port, xff="10.0.0.10")
        self.assertEqual(status, 200)
        self.assertEqual(body.get("username"), "admin")
        self.assertIsNotNone(cookie)

    def test_forwarded_for_is_ignored_when_the_peer_is_not_trusted(self):
        """With no trusted proxy configured a spoofed header must not help."""
        self._env(trusted_proxy=False)
        port = self._start()
        for i in range(6):
            self._login(port, password="bad", xff=f"10.0.0.{i}")
        # every request shares the real peer address, so the lock applies
        self.assertEqual(self._login(port, password="bad", xff="10.9.9.9")[0], 429)
        self.assertEqual(self._login(port)[0], 429)

    # --- no enumeration / no leakage --------------------------------------
    def test_unknown_user_and_wrong_password_are_indistinguishable(self):
        port = self._start()
        s1, _h1, b1 = self._req(port, "POST", "/api/login",
                                {"username": "admin", "password": "nope-nope-nope"})
        s2, _h2, b2 = self._req(port, "POST", "/api/login",
                                {"username": "nosuchuser", "password": "nope-nope-nope"})
        self.assertEqual((s1, b1), (s2, b2))
        self.assertEqual(s1, 401)

    def test_inactive_user_cannot_log_in(self):
        port = self._start()
        status, _sc, _c, _csrf, _b = self._login(port, "gone", OTHER)
        self.assertIn(status, (401, 403))

    def test_login_responses_leak_no_credential_material(self):
        port = self._start()
        _s, _sc, _c, _csrf, body = self._login(port)
        blob = json.dumps(body)
        for needle in (PASSWORD, "pbkdf2", "password_hash", "salt"):
            self.assertNotIn(needle, blob)
        # the CSRF token is expected, the SESSION token is not
        self.assertIn("csrf_token", body)
        self.assertNotIn(SESSION_COOKIE + "=", blob)

    def test_no_password_reaches_stdout_or_stderr(self):
        port = self._start()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self._login(port)
            self._login(port, password="wrong-wrong-wrong")
            self._login(port, username="nosuchuser", password="wrong-wrong-wrong")
        blob = out.getvalue() + err.getvalue()
        self.assertNotIn(PASSWORD, blob)
        self.assertNotIn("wrong-wrong-wrong", blob)

    # --- cookie / CSRF / session review ------------------------------------
    def test_cookie_flags(self):
        self._env(cookie_secure=False)
        port = self._start()
        _s, set_cookie, _c, _csrf, _b = self._login(port)
        low = set_cookie.lower()
        self.assertIn("httponly", low)
        self.assertIn("samesite=lax", low)
        self.assertNotIn("secure", low)          # plain-HTTP localhost demo
        self._env(cookie_secure=True)
        port2 = self._start()
        _s2, set_cookie2, _c2, _csrf2, _b2 = self._login(port2)
        self.assertIn("secure", set_cookie2.lower())

    def test_csrf_token_rotates_on_each_new_login(self):
        port = self._start()
        _s, _sc, _c, csrf1, _b = self._login(port)
        _s2, _sc2, _c2, csrf2, _b2 = self._login(port)
        self.assertTrue(csrf1 and csrf2)
        self.assertNotEqual(csrf1, csrf2)

    def test_logout_still_revokes_the_session(self):
        port = self._start()
        _s, _sc, cookie, csrf, _b = self._login(port)
        self.assertEqual(self._req(port, "GET", "/api/me",
                                   headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self._req(port, "POST", "/api/logout", {},
                                   headers={"Cookie": cookie,
                                            "X-CSRF-Token": csrf})[0], 200)
        self.assertEqual(self._req(port, "GET", "/api/me",
                                   headers={"Cookie": cookie})[0], 401)

    def test_unlock_cli_is_the_local_recovery_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "users.db")
            store = UserStore(db)
            store.create_user("admin", hash_password(PASSWORD), ["admin"], [])
            svc = AuthService(db, env=TEST_ENV, users=[],
                              sleep_fn=lambda _s: None)
            for _ in range(svc.lockout_threshold):
                svc.login_with_status("admin", "bad", ip="10.0.0.1")
            self.assertGreater(svc.lock_retry_after("admin", "10.0.0.1"), 0)

            import manage_users
            out = io.StringIO()
            with redirect_stdout(out):
                rc = manage_users.main(["--db", db, "unlock",
                                        "--username", "admin"])
            self.assertEqual(rc, 0)
            self.assertNotIn(PASSWORD, out.getvalue())
            check = AuthService(db, env=TEST_ENV, users=[],
                                sleep_fn=lambda _s: None)
            self.assertEqual(check.lock_retry_after("admin", "10.0.0.1"), 0)

    def test_manage_users_rejects_a_weak_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "users.db")
            UserStore(db)
            import manage_users
            os.environ[manage_users.PASSWORD_ENV] = "short"
            try:
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = manage_users.main(
                        ["--db", db, "create", "--username", "weakling",
                         "--role", "owner"])
            finally:
                os.environ.pop(manage_users.PASSWORD_ENV, None)
            self.assertEqual(rc, 2)
            self.assertIn("rejected", err.getvalue())
            self.assertNotIn("short", err.getvalue())


if __name__ == "__main__":
    unittest.main()
