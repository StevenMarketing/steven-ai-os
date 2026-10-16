"""Google OAuth web-flow glue: fail-closed, CSRF/PKCE, read-only listing."""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import google_flow as gf  # noqa: E402  (scripts/google_flow.py)

SECRET = "client-secret-probe-value"
FULL = {"GBP_ALLOW_LIVE_GOOGLE": "1", "GBP_GOOGLE_CLIENT_ID": "client-id-probe",
        "GBP_GOOGLE_CLIENT_SECRET": SECRET,
        "GBP_OAUTH_REDIRECT_URI": "https://greply.adzdigital.com/api/google/oauth/callback"}


class LiveReadyTests(unittest.TestCase):
    def test_nothing_is_ready_without_the_deliberate_opt_in(self):
        info = gf.live_ready({})
        self.assertFalse(info["ready"])
        self.assertEqual(info["reason"], "live_opt_in_missing")

    def test_the_first_missing_piece_is_named_in_order(self):
        no_id = dict(FULL); no_id.pop("GBP_GOOGLE_CLIENT_ID")
        self.assertEqual(gf.live_ready(no_id)["reason"], "client_id_missing")
        no_secret = dict(FULL); no_secret.pop("GBP_GOOGLE_CLIENT_SECRET")
        self.assertEqual(gf.live_ready(no_secret)["reason"], "client_secret_missing")
        no_uri = dict(FULL); no_uri.pop("GBP_OAUTH_REDIRECT_URI")
        self.assertEqual(gf.live_ready(no_uri)["reason"], "redirect_uri_missing")

    def test_ready_reports_presence_and_never_a_value(self):
        info = gf.live_ready(FULL)
        self.assertTrue(info["ready"])
        self.assertEqual(info["have"], {"client_id": True, "client_secret": True,
                                        "redirect_uri": True, "opt_in": True})
        self.assertNotIn(SECRET, json.dumps(info))


class StartFlowTests(unittest.TestCase):
    def test_start_refuses_when_the_setup_is_incomplete(self):
        with self.assertRaises(gf.GoogleSetupRequired) as ctx:
            gf.start_flow(environ={})
        self.assertEqual(ctx.exception.reason, "live_opt_in_missing")

    def test_the_url_carries_state_pkce_and_the_redirect_uri(self):
        flow = gf.start_flow(environ=FULL)
        url = flow["authorization_url"]
        for needle in ("state=", "code_challenge=", "code_challenge_method=S256",
                       "redirect_uri=https%3A%2F%2Fgreply.adzdigital.com", "scope="):
            self.assertIn(needle, url)
        self.assertNotIn(SECRET, url)
        self.assertGreaterEqual(len(flow["state"]), 20)
        self.assertGreaterEqual(len(flow["code_verifier"]), 40)
        self.assertNotEqual(flow["state"], flow["code_verifier"])


class FinishFlowTests(unittest.TestCase):
    def setUp(self):
        self.flow = gf.start_flow(environ=FULL)
        self.stored = []

    def _exchange(self, **kw):
        self.assertNotIn("code_verifier", kw)
        return {"token": "access-token-probe"}

    def test_a_callback_without_a_flow_in_the_session_is_rejected(self):
        with self.assertRaises(gf.FlowError) as ctx:
            gf.finish_flow("code", "state", None, environ=FULL, exchanger=self._exchange, store=self.stored.append)
        self.assertEqual(ctx.exception.reason, "no_flow_in_session")

    def test_a_mismatched_or_missing_state_is_rejected(self):
        for state in ("wrong-state-probe", ""):
            with self.assertRaises(gf.FlowError) as ctx:
                gf.finish_flow("code", state, dict(self.flow), environ=FULL,
                               exchanger=self._exchange, store=self.stored.append)
            self.assertEqual(ctx.exception.reason, "state_mismatch")
        self.assertEqual(self.stored, [])

    def test_an_expired_state_is_rejected(self):
        with self.assertRaises(gf.FlowError) as ctx:
            gf.finish_flow("code", self.flow["state"], dict(self.flow), environ=FULL,
                           now=self.flow["created_at"] + gf.FLOW_TTL_SECONDS + 1,
                           exchanger=self._exchange, store=self.stored.append)
        self.assertEqual(ctx.exception.reason, "state_expired")

    def test_a_good_callback_stores_the_token_and_burns_the_state(self):
        session = dict(self.flow)
        out = gf.finish_flow("code-probe", session["state"], session, environ=FULL,
                             exchanger=self._exchange, store=self.stored.append)
        self.assertTrue(out["connected"])
        self.assertTrue(session.get("used"))
        self.assertEqual(len(self.stored), 1)
        self.assertNotIn("access-token-probe", json.dumps(out))
        with self.assertRaises(gf.FlowError) as ctx:
            gf.finish_flow("code-probe", session["state"], session, environ=FULL,
                           exchanger=self._exchange, store=self.stored.append)
        self.assertEqual(ctx.exception.reason, "state_replayed")
        self.assertEqual(len(self.stored), 1)          # không lưu lần hai

    def test_finish_refuses_first_when_the_setup_is_incomplete(self):
        with self.assertRaises(gf.GoogleSetupRequired):
            gf.finish_flow("code", "state", dict(self.flow), environ={},
                           exchanger=self._exchange, store=self.stored.append)


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
            seen["account"] = account
            seen["limit"] = limit
            return rows

        out = gf.list_locations_read_only(environ=FULL, fetcher=fetcher, account="accounts/1001")
        self.assertEqual(len(out), gf.MAX_TEST_LOCATIONS)
        self.assertEqual(seen["limit"], gf.MAX_TEST_LOCATIONS)
        for row in out:
            self.assertEqual(sorted(row), ["address", "business_name", "location_id", "zip"])
        self.assertTrue(all(r["location_id"].startswith("loc-") for r in out))


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

    def test_the_token_path_is_configurable_and_only_a_path(self):
        p = gf.token_path({"GBP_TOKEN_PATH": "/tmp/probe-token.json"})
        self.assertEqual(p, "/tmp/probe-token.json")
        self.assertNotIn("token-", gf.token_path({}).replace(".gbp_token.json", ""))


if __name__ == "__main__":
    unittest.main()
