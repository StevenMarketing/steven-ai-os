"""Google OAuth web-flow glue: fail-closed ladder, CSRF/PKCE, state burning, redaction.

Each security fix in `scripts/google_flow.py` has a regression test here.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import google_flow as gf  # noqa: E402  (scripts/google_flow.py)

SECRET = "client-secret-probe-value"
CODE = "authorization-code-probe"
FULL = {"GBP_ALLOW_LIVE_GOOGLE": "1", "GBP_GOOGLE_CLIENT_ID": "client-id-probe",
        "GBP_GOOGLE_CLIENT_SECRET": SECRET,
        "GBP_OAUTH_REDIRECT_URI": "https://gbp.adzdigital.com/api/google/oauth/callback"}


class FakeExchanger:
    """An exchanger that mimics the real one: it accepts the PKCE verifier and records it."""

    def __init__(self, token=None, error=None):
        self.token = token if token is not None else {"token": "access-token-probe"}
        self.error = error
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.token


class LegacyExchanger:
    """An exchanger without verifier support -- the shape the protected module has today."""

    def __init__(self):
        self.calls = []

    def __call__(self, client_id, code, redirect_uri, client_secret=None):
        self.calls.append(code)
        return {"token": "access-token-probe"}


class LiveReadyTests(unittest.TestCase):
    def test_nothing_is_ready_without_the_deliberate_opt_in(self):
        info = gf.live_ready({})
        self.assertFalse(info["ready"])
        self.assertEqual(info["reason"], "live_opt_in_missing")

    def test_the_first_missing_piece_is_named_in_order(self):
        for drop, reason in (("GBP_GOOGLE_CLIENT_ID", "client_id_missing"),
                             ("GBP_GOOGLE_CLIENT_SECRET", "client_secret_missing"),
                             ("GBP_OAUTH_REDIRECT_URI", "redirect_uri_missing")):
            env = dict(FULL); env.pop(drop)
            self.assertEqual(gf.live_ready(env)["reason"], reason)

    def test_an_unsafe_redirect_uri_is_refused_by_the_ladder(self):
        env = dict(FULL); env["GBP_OAUTH_REDIRECT_URI"] = "http://gbp.adzdigital.com/cb"
        self.assertEqual(gf.live_ready(env)["reason"], "redirect_uri_not_https")

    def test_ready_reports_presence_and_never_a_value(self):
        info = gf.live_ready(FULL)
        self.assertTrue(info["ready"])
        self.assertNotIn(SECRET, json.dumps(info))


class RedirectUriTests(unittest.TestCase):
    def test_https_is_accepted(self):
        self.assertTrue(gf.check_redirect_uri("https://gbp.adzdigital.com/api/google/oauth/callback")["ok"])

    def test_plain_http_is_refused_except_on_loopback(self):
        self.assertEqual(gf.check_redirect_uri("http://gbp.adzdigital.com/cb")["reason"], "redirect_uri_not_https")
        for loop in ("http://127.0.0.1:8000/cb", "http://localhost/cb"):
            info = gf.check_redirect_uri(loop)
            self.assertTrue(info["ok"], loop)
            self.assertTrue(info["loopback"])

    def test_a_fragment_or_a_relative_value_is_refused(self):
        self.assertEqual(gf.check_redirect_uri("https://gbp.adzdigital.com/cb#token")["reason"],
                         "redirect_uri_has_fragment")
        self.assertEqual(gf.check_redirect_uri("/cb")["reason"], "redirect_uri_not_absolute")
        self.assertEqual(gf.check_redirect_uri("")["reason"], "redirect_uri_missing")


class StartFlowTests(unittest.TestCase):
    def test_start_refuses_when_the_setup_is_incomplete(self):
        with self.assertRaises(gf.GoogleSetupRequired) as ctx:
            gf.start_flow(environ={})
        self.assertEqual(ctx.exception.reason, "live_opt_in_missing")

    def test_the_url_carries_state_pkce_and_the_redirect_uri(self):
        flow = gf.start_flow(environ=FULL)
        url = flow["authorization_url"]
        for needle in ("state=", "code_challenge=", "code_challenge_method=S256",
                       "redirect_uri=https%3A%2F%2Fgbp.adzdigital.com", "scope="):
            self.assertIn(needle, url)
        self.assertNotIn(SECRET, url)
        self.assertNotIn(flow["code_verifier"], url)
        self.assertGreaterEqual(len(flow["state"]), 20)


class FinishFlowTests(unittest.TestCase):
    def setUp(self):
        self.flow = gf.start_flow(environ=FULL)
        self.stored = []
        self.exchanger = FakeExchanger()

    def _finish(self, session, exchanger=None, store=None, state=None):
        return gf.finish_flow(CODE, state if state is not None else session["state"], session,
                              environ=FULL, exchanger=exchanger or self.exchanger,
                              store=store or self.stored.append)

    def test_a_callback_without_a_flow_in_the_session_is_rejected(self):
        with self.assertRaises(gf.FlowError) as ctx:
            gf.finish_flow(CODE, "any-state", None, environ=FULL, exchanger=self.exchanger,
                           store=self.stored.append)
        self.assertEqual(ctx.exception.reason, "no_flow_in_session")
        self.assertEqual(self.exchanger.calls, [])

    def test_a_mismatched_or_missing_state_is_rejected_before_any_call(self):
        for state in ("wrong-state-probe", ""):
            with self.assertRaises(gf.FlowError) as ctx:
                self._finish(dict(self.flow), state=state)
            self.assertEqual(ctx.exception.reason, "state_mismatch")
        self.assertEqual(self.exchanger.calls, [])

    def test_an_expired_state_is_rejected(self):
        session = dict(self.flow)
        with self.assertRaises(gf.FlowError) as ctx:
            gf.finish_flow(CODE, session["state"], session, environ=FULL,
                           now=session["created_at"] + gf.FLOW_TTL_SECONDS + 1,
                           exchanger=self.exchanger, store=self.stored.append)
        self.assertEqual(ctx.exception.reason, "state_expired")
        self.assertEqual(self.exchanger.calls, [])

    def test_a_good_callback_stores_once_and_burns_the_state(self):
        session = dict(self.flow)
        out = self._finish(session)
        self.assertTrue(out["connected"])
        self.assertTrue(session["used"])
        self.assertEqual(len(self.stored), 1)
        self.assertNotIn("access-token-probe", json.dumps(out))
        with self.assertRaises(gf.FlowError) as ctx:
            self._finish(session)
        self.assertEqual(ctx.exception.reason, "state_replayed")
        self.assertEqual(len(self.stored), 1)
        self.assertEqual(len(self.exchanger.calls), 1)

    def test_a_state_already_in_flight_is_rejected(self):
        """A concurrent callback must not race the first one for a token."""
        session = dict(self.flow)
        session["in_flight"] = True
        with self.assertRaises(gf.FlowError) as ctx:
            self._finish(session)
        self.assertEqual(ctx.exception.reason, "state_in_flight")
        self.assertEqual(self.exchanger.calls, [])

    def test_the_state_is_burned_even_when_the_exchange_fails(self):
        """A failed exchange must not leave the state reusable for a retry."""
        session = dict(self.flow)
        failing = FakeExchanger(error=RuntimeError("provider said no"))
        with self.assertRaises(gf.FlowError) as ctx:
            self._finish(session, exchanger=failing)
        self.assertEqual(ctx.exception.reason.split(":")[0], "exchange_error")
        self.assertTrue(session["used"])
        self.assertFalse(session["in_flight"])
        with self.assertRaises(gf.FlowError) as again:
            self._finish(session)
        self.assertEqual(again.exception.reason, "state_replayed")
        self.assertEqual(len(failing.calls), 1)          # không thử exchange lần hai

    def test_pkce_is_never_silently_dropped(self):
        """An exchanger that cannot take the verifier is refused, not downgraded."""
        session = dict(self.flow)
        legacy = LegacyExchanger()
        with self.assertRaises(gf.FlowError) as ctx:
            self._finish(session, exchanger=legacy)
        self.assertEqual(ctx.exception.reason, "pkce_not_threaded")
        self.assertEqual(legacy.calls, [])
        # Refused before the attempt, so nothing was consumed: a config fix may retry
        # within the TTL, and no exchange ever happened without PKCE.
        self.assertFalse(session.get("used"))

    def test_the_verifier_reaches_an_exchanger_that_accepts_it(self):
        session = dict(self.flow)
        self._finish(session)
        sent = self.exchanger.calls[0]
        self.assertEqual(sent["code_verifier"], session["code_verifier"])
        self.assertEqual(sent["code"], CODE)

    def test_an_error_from_the_provider_cannot_leak_secrets(self):
        leaky = FakeExchanger(error=RuntimeError(
            "bad request body=%s code=%s token=sk-or-v1-abcdefghijklmnopqrstuvwxyz123456"
            % (SECRET, CODE)))
        session = dict(self.flow)
        with self.assertRaises(gf.FlowError) as ctx:
            self._finish(session, exchanger=leaky)
        message = str(ctx.exception)
        self.assertNotIn(SECRET, message)
        self.assertNotIn(CODE, message)
        self.assertNotIn("sk-or-v1-", message)
        self.assertIn("[redacted]", message)

    def test_a_storage_failure_is_reported_without_leaking(self):
        def bad_store(token):
            raise RuntimeError("cannot write %s" % SECRET)
        session = dict(self.flow)
        with self.assertRaises(gf.FlowError) as ctx:
            self._finish(session, store=bad_store)
        self.assertTrue(str(ctx.exception).startswith("google_flow_error: store_error"))
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_finish_refuses_first_when_the_setup_is_incomplete(self):
        with self.assertRaises(gf.GoogleSetupRequired):
            gf.finish_flow(CODE, "state", dict(self.flow), environ={},
                           exchanger=self.exchanger, store=self.stored.append)

    def test_the_default_store_writes_through_the_token_manager_with_mode_600(self):
        """Issue 3: the storage gap is closed with the manager's real API."""
        from gbp.auth.token_manager import Token
        path = os.path.join(tempfile.mkdtemp(prefix="gbp-store-"), ".gbp_token.json")
        token = Token.from_dict({"access_token": "at-probe", "refresh_token": "rt-probe",
                                 "expires_at": 0.0, "scopes": ["s"], "token_type": "Bearer"})
        session = dict(self.flow)
        out = gf.finish_flow(CODE, session["state"], session, environ=FULL,
                             exchanger=FakeExchanger(token=token),
                             store=lambda t: gf._default_store()(t, path))
        self.assertTrue(out["connected"])
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertNotIn("at-probe", json.dumps(out))       # kết quả trả về không chứa token


class ReadOnlyListTests(unittest.TestCase):
    def test_without_credentials_it_refuses_instead_of_faking_rows(self):
        with self.assertRaises(gf.GoogleSetupRequired):
            gf.list_locations_read_only(environ={})

    def test_without_a_wired_fetcher_it_still_refuses(self):
        with self.assertRaises(gf.GoogleSetupRequired) as ctx:
            gf.list_locations_read_only(environ=FULL)
        self.assertEqual(ctx.exception.reason, "fetcher_not_wired")

    def test_at_most_five_locations_with_the_four_required_fields(self):
        rows = [{"location_id": "loc-%d" % i, "business_name": "Nail Salon %d" % i,
                 "address": "%d Main St" % i, "zip": "9410%d" % (i % 10)} for i in range(7)]
        seen = {}

        def fetcher(*, account=None, limit=None):
            seen.update(account=account, limit=limit)
            return rows

        out = gf.list_locations_read_only(environ=FULL, fetcher=fetcher, account="accounts/1001")
        self.assertEqual(len(out), gf.MAX_TEST_LOCATIONS)
        self.assertEqual(seen, {"account": "accounts/1001", "limit": gf.MAX_TEST_LOCATIONS})
        for row in out:
            self.assertEqual(sorted(row), ["address", "business_name", "location_id", "zip"])


class FetcherAdapterTests(unittest.TestCase):
    """Issue 4: the read-only fetcher is wired through an injected client, no live call."""

    class FakeClient:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None):
            self.calls.append((url, params))
            return {"locations": [
                {"name": "locations/111", "title": "Nail Salon A",
                 "storefrontAddress": {"addressLines": ["1 Main St", "Suite 2"], "postalCode": "94101"}},
                {"name": "locations/222", "title": "Nail Salon B",
                 "storefrontAddress": {"addressLines": ["2 Oak Ave"], "postalCode": "94102"}},
            ]}

    def test_the_adapter_reads_only_and_keeps_four_fields(self):
        client = self.FakeClient()
        rows = gf.locations_fetcher(client)(account="accounts/1001", limit=5)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"location_id": "locations/111", "business_name": "Nail Salon A",
                                   "address": "1 Main St, Suite 2", "zip": "94101"})
        url, params = client.calls[0]
        self.assertIn("/accounts/1001/locations", url)
        self.assertEqual(params["pageSize"], gf.MAX_TEST_LOCATIONS)
        self.assertIn("readMask", params)

    def test_the_adapter_cannot_be_reached_without_the_live_ladder(self):
        client = self.FakeClient()
        with self.assertRaises(gf.GoogleSetupRequired):
            gf.list_locations_read_only(environ={}, fetcher=gf.locations_fetcher(client))
        self.assertEqual(client.calls, [])


class DisconnectTests(unittest.TestCase):
    def test_disconnect_removes_a_stored_token_and_reports_it(self):
        path = os.path.join(tempfile.mkdtemp(prefix="gbp-tok-"), ".gbp_token.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"refresh_token": "probe"}')
        os.chmod(path, 0o600)
        out = gf.disconnect(store_path=path)
        self.assertTrue(out["disconnected"])
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("probe", json.dumps(out))

    def test_disconnect_is_honest_when_nothing_is_stored(self):
        out = gf.disconnect(store_path=os.path.join(tempfile.mkdtemp(prefix="gbp-none-"), ".gbp_token.json"))
        self.assertFalse(out["disconnected"])
        self.assertEqual(out["reason"], "no_token_stored")

    def test_the_token_path_defaults_outside_the_repository(self):
        default = gf.token_path({})
        self.assertTrue(default.endswith(".gbp_token.json"))
        self.assertFalse(os.path.abspath(default).startswith(os.getcwd()))
        self.assertEqual(gf.token_path({"GBP_TOKEN_PATH": "/tmp/probe-token.json"}), "/tmp/probe-token.json")


if __name__ == "__main__":
    unittest.main()
