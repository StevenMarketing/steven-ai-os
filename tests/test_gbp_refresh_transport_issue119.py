"""Issue #119 -- the token-refresh transport must reach LiveLocationSync.

Verified root cause (Muse, 2026-10-23): ``GoogleConnectWiring._sync`` built
``LiveLocationSync`` WITHOUT passing a refresh transport, so the token provider
always called ``TokenVault.get_valid(tenant, transport=None)``. ``get_valid``
raises ``TokenExpiredError("refresh_transport_missing")`` the moment the 1-hour
access token lapses, so EVERY sync more than 1h after connect failed with
``token_expired`` even though the refresh token was healthy. The connect-time
sync only worked because the access token was seconds old.

These tests drive the WIRING sync path entirely offline:

* an EXPIRED access token + a healthy refresh token syncs through the wiring
  with a fake refresh transport -> the sync succeeds, the refreshed token is
  saved, ``last_error`` is cleared and ``connection_state`` is ``connected``;
* the sync path no longer reaches ``get_valid`` with a None transport -- the
  fake refresh transport records the refresh POST and a spy confirms the
  transport handed to ``get_valid`` is never None;
* a negative control reproduces the original bug (a None transport still fails)
  so the regression test has teeth.

No live Google call: both transports are a ``MockTransport`` and no socket is
opened. No token or secret appears in any bundle, meta or assertion message.

Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.auth.token_manager import Token  # noqa: E402
from gbp.client.http_client import HttpClient, MockTransport  # noqa: E402
from gbp.oauth.config import LIVE_ENV  # noqa: E402
from gbp.oauth.vault import (  # noqa: E402
    STATE_CONNECTED,
    STATE_TOKEN_EXPIRED,
    TOKEN_URL,
    TokenVault,
)
from gbp.oauth.wiring import GoogleConnectWiring  # noqa: E402
from gbp.sync.live_sync import LiveLocationSync  # noqa: E402

CANONICAL_REDIRECT = "https://gbp.adzdigital.com/api/google/callback"
VAULT_KEY = "issue-119-vault-key-material-0123456789abcdef"
TENANT = "accounts/1001/locations/loc-119"

#: A fixed clock so expiry is deterministic (no wall-clock dependence).
CLOCK = 1_700_000_000.0

#: The stale access token minted at connect time; expired long before CLOCK.
STALE_ACCESS = "stale-access-token-probe"
#: The access token the fake refresh endpoint hands back.
FRESH_ACCESS = "fresh-access-token-probe"
REFRESH_TOKEN = "refresh-token-probe"


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


def refresh_transport() -> MockTransport:
    """The fake OAuth token endpoint (records every POST)."""
    transport = MockTransport()
    transport.register("POST", TOKEN_URL, {
        "access_token": FRESH_ACCESS,
        "refresh_token": REFRESH_TOKEN,
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/business.manage",
        "token_type": "Bearer",
    }, status=200)
    return transport


def api_transport() -> MockTransport:
    """The fake read-only GBP API (accounts + one location)."""
    transport = MockTransport()
    transport.register(
        "GET", "https://mybusiness.googleapis.com/v1/accounts",
        {"accounts": [{"name": "accounts/1001", "accountName": "Acme Group",
                       "type": "LOCATION_GROUP"}]})
    transport.register(
        "GET", "https://mybusiness.googleapis.com/v1/accounts/1001/locations",
        {"locations": [{"name": TENANT, "title": "Refresh Nails",
                        "storefrontAddress": {"postalCode": "94103"}}]})
    return transport


class RefreshThroughWiringTests(unittest.TestCase):
    """The server sync path refreshes an expired token through the wiring."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gbp-refresh-119-")
        self.env = full_env(self.tmp)
        self.clock = lambda: CLOCK
        # One shared vault (injected via vault_factory) so the test can both
        # seed an expired token and read the refreshed record back.
        self.vault = TokenVault(self.tmp, VAULT_KEY, clock=self.clock)
        self.token_transport = refresh_transport()
        self.api_transport = api_transport()
        self.wiring = GoogleConnectWiring(
            token_transport=self.token_transport,
            api_transport=self.api_transport,
            vault_factory=lambda environ, config: self.vault,
        )

    # -- helpers --------------------------------------------------------- #
    def _seed_expired_connection(self):
        """Store an expired access token + a healthy refresh token, flagged."""
        self.vault.save(TENANT, Token(
            access_token=STALE_ACCESS, refresh_token=REFRESH_TOKEN,
            expires_at=CLOCK - 600, scopes=["scope/a"]))
        # Simulate the state a prior failed sync leaves behind.
        self.vault.mark_error(TENANT, "token_expired")
        self.assertEqual(
            self.vault.connection_state(TENANT), STATE_TOKEN_EXPIRED)

    def _refresh_posts(self):
        return [c for c in self.token_transport.calls
                if c["method"] == "POST" and c["url"] == TOKEN_URL]

    # -- the acceptance regression -------------------------------------- #
    def test_expired_token_refreshes_and_sync_succeeds_through_the_wiring(self):
        self._seed_expired_connection()

        bundle = self.wiring.sync(TENANT, environ=self.env)

        # The sync completed with real data instead of token_expired.
        self.assertEqual(bundle["sync_status"], "ok")
        self.assertEqual(len(bundle["accounts"]), 1)
        self.assertEqual([loc["location_id"] for loc in bundle["locations"]],
                         [TENANT])

        # The refresh token was actually exchanged (exactly once) ...
        self.assertEqual(len(self._refresh_posts()), 1)
        # ... and the read-only API still went through the api transport.
        self.assertTrue(self.api_transport.calls)
        self.assertFalse(
            [c for c in self.api_transport.calls if c["method"] == "POST"])

        # The refreshed token is saved ...
        stored = self.vault.load(TENANT)
        self.assertEqual(stored.access_token, FRESH_ACCESS)
        # ... last_error is cleared ...
        self.assertIsNone(self.vault.meta(TENANT)["last_error"])
        # ... and the connection is back to connected.
        self.assertEqual(self.vault.connection_state(TENANT), STATE_CONNECTED)

        # No token or secret ever rides the returned bundle.
        blob = json.dumps(bundle)
        self.assertNotIn(STALE_ACCESS, blob)
        self.assertNotIn(FRESH_ACCESS, blob)
        self.assertNotIn(REFRESH_TOKEN, blob)
        self.assertNotIn("client-secret-probe", blob)

    def test_sync_never_reaches_get_valid_with_a_none_transport(self):
        self._seed_expired_connection()

        seen = []
        original = self.vault.get_valid

        def spy(tenant, transport=None, **kwargs):
            seen.append(transport)
            return original(tenant, transport, **kwargs)

        self.vault.get_valid = spy
        try:
            bundle = self.wiring.sync(TENANT, environ=self.env)
        finally:
            del self.vault.get_valid  # restore the bound method

        self.assertEqual(bundle["sync_status"], "ok")
        self.assertTrue(seen, "get_valid was never called")
        self.assertTrue(all(t is not None for t in seen),
                        "the sync path handed get_valid a None transport")
        # The recorded refresh call is the same proof, seen from the transport.
        self.assertEqual(len(self._refresh_posts()), 1)

    def test_a_valid_token_syncs_without_any_refresh_call(self):
        self.vault.save(TENANT, Token(
            access_token="fresh-enough", refresh_token=REFRESH_TOKEN,
            expires_at=CLOCK + 3600, scopes=["scope/a"]))

        bundle = self.wiring.sync(TENANT, environ=self.env)

        self.assertEqual(bundle["sync_status"], "ok")
        self.assertEqual(self._refresh_posts(), [])
        self.assertEqual(self.vault.connection_state(TENANT), STATE_CONNECTED)

    # -- negative control: the bug this regression guards against -------- #
    def test_a_none_transport_still_fails_so_the_regression_has_teeth(self):
        self._seed_expired_connection()
        # The pre-#119 shape: an http built from the api transport, but NO
        # refresh transport. This is exactly what wiring._sync used to build.
        sync = LiveLocationSync(
            self.vault, TENANT, http=HttpClient(self.api_transport),
            client_id="client-id-probe", client_secret="client-secret-probe")

        bundle = sync.sync()

        self.assertEqual(bundle["sync_status"], "token_expired")
        self.assertEqual(self._refresh_posts(), [])
        self.assertEqual(self.vault.meta(TENANT)["last_error"], "token_expired")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
