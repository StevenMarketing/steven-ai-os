"""Issue #110 (GBP increment 1) -- per-tenant OAuth connect, encrypted token
vault, read-only accounts/locations sync and the dashboard connection state.

Fully OFFLINE: the token endpoint and the GBP API are both a ``MockTransport``
(no socket is opened for the API), pacing/backoff sleeps are injected so no test
ever waits, and the only loopback server is the repo's existing HTTP harness.

Covered here:

* the live gate (``GBP_LIVE`` + credentials) -- fixture mode otherwise;
* the connect flow: consent URL, per-tenant state/nonce, single-use burning;
* strict callback validation: state mismatch, nonce mismatch, expiry, replay,
  in-flight -- each rejected BEFORE any exchange;
* encrypted-at-rest storage, tamper detection, wrong-key refusal, tenant
  isolation of records;
* auto-refresh: expired token refreshed, refresh failure surfacing the
  ``token_expired`` state;
* read-only accounts/locations sync mapped into the dashboard model;
* pacing (token bucket well under 300 QPM) and exponential backoff + jitter on
  HTTP 429;
* the ``/api/google/connections`` dashboard endpoint staying inert by default.

Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import _google_connections, create_server  # noqa: E402
from gbp.auth.token_manager import Token  # noqa: E402
from gbp.client.gbp_client import GbpClient  # noqa: E402
from gbp.client.http_client import (  # noqa: E402
    ApiError,
    HttpClient,
    MockTransport,
)
from gbp.client.mock_client import AuthError, RateLimitedError  # noqa: E402
from gbp.oauth.config import (  # noqa: E402
    LiveNotConfigured,
    encryption_key,
    live_enabled,
    load_config,
    require_config,
    vault_dir,
)
from gbp.oauth.flow import (  # noqa: E402
    OAuthExchangeError,
    connection_rows,
    disconnect,
    handle_callback,
    start_connect,
)
from gbp.oauth.state import FlowStateError, StateStore  # noqa: E402
from gbp.oauth.tenancy import (  # noqa: E402
    TenantIsolationError,
    assert_owner,
    normalize_tenant,
    same_tenant,
    scope_rows,
    tenant_key,
)
from gbp.oauth.vault import (  # noqa: E402
    STATE_CONNECTED,
    STATE_NOT_CONNECTED,
    STATE_TOKEN_EXPIRED,
    TOKEN_URL,
    StdlibCipher,
    TokenExpiredError,
    TokenVault,
    VaultError,
)
from gbp.repo import Repository  # noqa: E402
from gbp.sync.live_sync import LiveLocationSync, AuthorizedHttp  # noqa: E402
from gbp.sync.pacing import DEFAULT_QPM, TokenBucket  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")

#: A vault secret that is NOT credential-shaped (tests must never hold a real key).
VAULT_KEY = "unit-test-vault-key-material-0123456789abcdef"

REDIRECT = "https://gbp.example.com/api/google/oauth/callback"


def full_env(**over: str) -> dict:
    """A complete live configuration, overridable per test."""
    env = {
        "GBP_LIVE": "1",
        "GBP_OAUTH_CLIENT_ID": "client-id-probe",
        "GBP_OAUTH_CLIENT_SECRET": "client-secret-probe",
        "GBP_OAUTH_REDIRECT_URI": REDIRECT,
        "GBP_TOKEN_ENCRYPTION_KEY": VAULT_KEY,
    }
    env.update(over)
    return env


def fixed_cipher(secret: str = VAULT_KEY) -> StdlibCipher:
    """A deterministic cipher so encrypted bytes can be compared across runs."""
    return StdlibCipher(secret, nonce_source=lambda n: b"\x07" * n)


def token_response(access: str = "access-token-probe", refresh: str = "refresh-token-probe",
                   expires_in: int = 3600, status: int = 200) -> MockTransport:
    transport = MockTransport()
    transport.register("POST", TOKEN_URL, {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
        "scope": "https://www.googleapis.com/auth/business.manage",
        "token_type": "Bearer",
    }, status=status)
    return transport


class VaultTestCase(unittest.TestCase):
    """A per-test temp vault directory with a fixed cipher."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gbp-vault-")
        self.vault = TokenVault(self.tmp, VAULT_KEY, cipher=fixed_cipher(),
                                client_id="client-id-probe",
                                client_secret="client-secret-probe")

    def saved_token(self, tenant: str = "loc-1", *, access: str = "access-token-probe",
                    refresh: str = "refresh-token-probe", expires_at: float = 10_000.0) -> None:
        self.vault.save(tenant, Token(access_token=access, refresh_token=refresh,
                                      expires_at=expires_at, scopes=["scope/a"]))


# ===================================================================== #
# live gate
# ===================================================================== #
class LiveGateTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(live_enabled({}))
        self.assertFalse(load_config({}).complete)

    def test_needs_the_switch_even_when_configured(self):
        env = full_env()
        env.pop("GBP_LIVE")
        self.assertFalse(live_enabled(env))

    def test_needs_every_credential(self):
        for drop in ("GBP_OAUTH_CLIENT_ID", "GBP_OAUTH_CLIENT_SECRET",
                     "GBP_OAUTH_REDIRECT_URI"):
            env = full_env()
            env.pop(drop)
            self.assertFalse(live_enabled(env), msg=f"{drop} dropped but still live")

    def test_enabled_only_with_switch_and_credentials(self):
        self.assertTrue(live_enabled(full_env()))

    def test_require_config_names_the_first_missing_value_in_order(self):
        env = full_env()
        env.pop("GBP_OAUTH_CLIENT_ID")
        with self.assertRaises(LiveNotConfigured) as ctx:
            require_config(env)
        self.assertEqual(ctx.exception.reason, "credential_missing")
        self.assertIn("GBP_OAUTH_CLIENT_ID", str(ctx.exception))

    def test_require_config_refuses_when_live_mode_is_off(self):
        with self.assertRaises(LiveNotConfigured) as ctx:
            require_config({})
        self.assertEqual(ctx.exception.reason, "live_mode_off")

    def test_start_connect_refuses_without_live_mode(self):
        with self.assertRaises(LiveNotConfigured):
            start_connect("loc-1", store=StateStore(), environ={})

    def test_the_key_is_required_and_never_returned_by_accident(self):
        self.assertEqual(encryption_key({"GBP_TOKEN_ENCRYPTION_KEY": VAULT_KEY}), VAULT_KEY)
        with self.assertRaises(LiveNotConfigured):
            encryption_key({})

    def test_vault_dir_defaults_outside_the_repository(self):
        self.assertNotIn(_ROOT, vault_dir({}))
        configured = self.tmp_dir()
        self.assertEqual(vault_dir({"GBP_TOKEN_VAULT_DIR": configured}), configured)

    @staticmethod
    def tmp_dir() -> str:
        return tempfile.mkdtemp(prefix="gbp-vaultdir-")


# ===================================================================== #
# connect flow
# ===================================================================== #
class StartConnectTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore()
        self.env = full_env()

    def test_the_url_carries_state_scope_and_redirect(self):
        flow = start_connect("loc-1", store=self.store, environ=self.env,
                             state="state-probe", nonce="nonce-probe")
        url = flow["authorization_url"]
        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertIn("state=state-probe", url)
        self.assertIn("business.manage", url)
        self.assertIn("access_type=offline", url)
        self.assertIn("prompt=consent", url)
        self.assertIn("redirect_uri=https%3A%2F%2Fgbp.example.com", url)

    def test_state_and_nonce_are_random_distinct_values(self):
        first = start_connect("loc-1", store=self.store, environ=self.env)
        second = start_connect("loc-1", store=self.store, environ=self.env)
        self.assertNotEqual(first["state"], first["nonce"])
        self.assertNotEqual(first["state"], second["state"])
        self.assertNotEqual(first["nonce"], second["nonce"])

    def test_the_client_secret_never_appears_in_the_start_payload(self):
        flow = start_connect("loc-1", store=self.store, environ=self.env)
        self.assertNotIn("client-secret-probe", json.dumps(flow))

    def test_a_pending_flow_is_per_tenant(self):
        start_connect("loc-1", store=self.store, environ=self.env,
                      state="s1", nonce="n1")
        start_connect("loc-2", store=self.store, environ=self.env,
                      state="s2", nonce="n2")
        self.assertEqual(self.store.peek("loc-1")["state"], "s1")
        self.assertEqual(self.store.peek("loc-2")["state"], "s2")


class CallbackValidationTests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.env = full_env()
        self.clock = FakeClock()
        self.now = 1_000_000.0
        self.clock.t = self.now
        self.store = StateStore(clock=self.clock.now)
        start_connect("loc-1", store=self.store, environ=self.env,
                      state="state-probe", nonce="nonce-probe")
        self.transport = token_response()

    def _callback(self, **over):
        kwargs = dict(code="code-probe", state="state-probe", nonce="nonce-probe",
                      tenant="loc-1", store=self.store, vault=self.vault,
                      transport=self.transport, environ=self.env, now=self.clock.t)
        kwargs.update(over)
        return handle_callback(**kwargs)

    def test_a_good_callback_connects_and_stores_once(self):
        result = self._callback()
        self.assertTrue(result["connected"])
        self.assertEqual(result["connection_state"], STATE_CONNECTED)
        self.assertEqual(self.vault.connection_state("loc-1", now=1010.0), STATE_CONNECTED)

    def test_the_result_never_carries_a_token(self):
        result = self._callback()
        blob = json.dumps(result)
        self.assertNotIn("access-token-probe", blob)
        self.assertNotIn("refresh-token-probe", blob)
        self.assertTrue(result["token_redacted"])

    def test_a_mismatched_state_is_rejected_before_any_exchange(self):
        with self.assertRaises(FlowStateError) as ctx:
            self._callback(state="wrong-state")
        self.assertEqual(ctx.exception.reason, "state_mismatch")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.vault.connection_state("loc-1"), STATE_NOT_CONNECTED)

    def test_a_mismatched_nonce_is_rejected_before_any_exchange(self):
        with self.assertRaises(FlowStateError) as ctx:
            self._callback(nonce="wrong-nonce")
        self.assertEqual(ctx.exception.reason, "nonce_mismatch")
        self.assertEqual(self.transport.calls, [])

    def test_a_missing_state_or_nonce_is_rejected(self):
        for over, reason in (({"state": ""}, "state_mismatch"),
                             ({"nonce": ""}, "nonce_mismatch")):
            with self.assertRaises(FlowStateError) as ctx:
                self._callback(**over)
            self.assertEqual(ctx.exception.reason, reason)

    def test_a_callback_without_any_flow_is_rejected(self):
        with self.assertRaises(FlowStateError) as ctx:
            self._callback(tenant="loc-3")
        self.assertEqual(ctx.exception.reason, "no_flow_in_session")

    def test_an_expired_state_is_rejected(self):
        self.clock.t = self.now + self.store.ttl_seconds + 1
        with self.assertRaises(FlowStateError) as ctx:
            self._callback()
        self.assertEqual(ctx.exception.reason, "state_expired")
        self.assertEqual(self.transport.calls, [])

    def test_a_replayed_state_is_rejected(self):
        self._callback()
        with self.assertRaises(FlowStateError) as ctx:
            self._callback()
        self.assertEqual(ctx.exception.reason, "state_replayed")

    def test_the_state_is_burned_even_when_the_exchange_fails(self):
        failing = MockTransport()
        failing.register("POST", TOKEN_URL, {"error": "invalid_grant"}, status=400)
        with self.assertRaises(OAuthExchangeError):
            self._callback(transport=failing)
        self.assertEqual(self.vault.connection_state("loc-1"), STATE_NOT_CONNECTED)
        with self.assertRaises(FlowStateError) as ctx:
            self._callback()
        self.assertEqual(ctx.exception.reason, "state_replayed")

    def test_a_missing_code_is_rejected(self):
        with self.assertRaises(OAuthExchangeError) as ctx:
            self._callback(code="")
        self.assertEqual(ctx.exception.reason, "missing_code")

    def test_another_tenant_cannot_use_this_flow(self):
        with self.assertRaises(FlowStateError):
            self._callback(tenant="loc-2")
        self.assertEqual(self.vault.connection_state("loc-2"), STATE_NOT_CONNECTED)
        self.assertEqual(self.transport.calls, [])

    def test_an_exchange_failure_message_leaks_nothing(self):
        failing = MockTransport()
        failing.register("POST", TOKEN_URL, {
            "error": "invalid_grant",
            "error_description": "code-probe client-secret-probe rejected",
        }, status=400)
        with self.assertRaises(OAuthExchangeError) as ctx:
            self._callback(transport=failing)
        text = str(ctx.exception)
        self.assertNotIn("client-secret-probe", text)
        self.assertNotIn("code-probe", text)


# ===================================================================== #
# vault
# ===================================================================== #
class VaultEncryptionTests(VaultTestCase):
    def test_the_token_is_not_present_in_the_file(self):
        self.saved_token()
        with open(self.vault.path_for("loc-1"), "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"access-token-probe", raw)
        self.assertNotIn(b"refresh-token-probe", raw)

    def test_the_file_mode_is_600(self):
        self.saved_token()
        self.assertEqual(os.stat(self.vault.path_for("loc-1")).st_mode & 0o777, 0o600)

    def test_tampered_ciphertext_is_refused(self):
        self.saved_token()
        path = self.vault.path_for("loc-1")
        with open(path, "rb") as fh:
            blob = bytearray(fh.read())
        blob[-1] ^= 0x5A
        with open(path, "wb") as fh:
            fh.write(bytes(blob))
        with self.assertRaises(VaultError):
            self.vault.load("loc-1")

    def test_a_wrong_key_cannot_decrypt(self):
        self.saved_token()
        other = TokenVault(self.tmp, "a-completely-different-secret", cipher=None)
        with self.assertRaises(VaultError):
            other.load("loc-1")

    def test_a_record_naming_another_tenant_is_refused(self):
        self.vault._write_record("loc-1", {
            "tenant_id": "loc-2", "access_token": "x", "refresh_token": None,
            "expires_at": 1.0, "scopes": [], "token_type": "Bearer",
        })
        with self.assertRaises(TenantIsolationError):
            self.vault.load("loc-1")

    def test_metadata_never_exposes_a_token(self):
        self.saved_token()
        meta = self.vault.meta("loc-1")
        self.assertNotIn("access_token", meta)
        self.assertNotIn("refresh_token", meta)
        self.assertTrue(meta["has_refresh_token"])
        self.assertNotIn("access-token-probe", json.dumps(meta))


class TenantIsolationTests(VaultTestCase):
    def test_another_tenant_reads_nothing(self):
        self.saved_token("loc-1")
        self.assertIsNone(self.vault.load("loc-2"))
        self.assertIsNone(self.vault.meta("loc-2"))
        self.assertEqual(self.vault.connection_state("loc-2"), STATE_NOT_CONNECTED)

    def test_two_tenants_get_two_files(self):
        self.saved_token("loc-1")
        self.saved_token("loc-2")
        self.assertNotEqual(self.vault.path_for("loc-1"), self.vault.path_for("loc-2"))

    def test_deleting_one_tenant_leaves_the_other(self):
        self.saved_token("loc-1")
        self.saved_token("loc-2")
        self.assertTrue(self.vault.delete("loc-1"))
        self.assertFalse(self.vault.delete("loc-1"))
        self.assertIsNotNone(self.vault.load("loc-2"))

    def test_helpers_enforce_the_same_rules(self):
        self.assertEqual(normalize_tenant("  loc-1  "), "loc-1")
        with self.assertRaises(TenantIsolationError):
            normalize_tenant("")
        with self.assertRaises(TenantIsolationError):
            normalize_tenant(None)
        self.assertTrue(same_tenant("loc-1", "loc-1"))
        self.assertFalse(same_tenant("loc-1", "LOC-1"))
        self.assertFalse(same_tenant("loc-1", "loc-2"))
        assert_owner("loc-1", "loc-1")
        with self.assertRaises(TenantIsolationError):
            assert_owner("loc-2", "loc-1")

    def test_scope_rows_drops_untagged_and_foreign_rows(self):
        rows = [{"tenant_id": "loc-1", "v": 1}, {"tenant_id": "loc-2", "v": 2},
                {"v": 3}, "not-a-row"]
        self.assertEqual(scope_rows(rows, "loc-1"), [{"tenant_id": "loc-1", "v": 1}])

    def test_tenant_keys_are_distinct_and_path_safe(self):
        self.assertNotEqual(tenant_key("loc-1"), tenant_key("loc-2"))
        self.assertNotIn("/", tenant_key("../../etc/passwd"))


# ===================================================================== #
# refresh
# ===================================================================== #
class RefreshTests(VaultTestCase):
    def test_an_expired_token_is_refreshed_and_saved(self):
        self.saved_token(expires_at=1000.0)
        transport = MockTransport()
        transport.register("POST", TOKEN_URL,
                           {"access_token": "fresh-token", "expires_in": 3600})
        token = self.vault.get_valid("loc-1", transport, now=5000.0)
        self.assertEqual(token.access_token, "fresh-token")
        self.assertEqual(token.expires_at, 5000.0 + 3600)
        self.assertEqual(self.vault.load("loc-1").access_token, "fresh-token")
        self.assertEqual(self.vault.connection_state("loc-1", now=5001.0), STATE_CONNECTED)

    def test_a_valid_token_is_not_refreshed(self):
        self.saved_token(expires_at=10_000.0)
        transport = token_response()
        token = self.vault.get_valid("loc-1", transport, now=5000.0)
        self.assertEqual(token.access_token, "access-token-probe")
        self.assertEqual(transport.calls, [])

    def test_a_rejected_refresh_surfaces_the_token_expired_state(self):
        self.saved_token(expires_at=1000.0)
        transport = MockTransport()
        transport.register("POST", TOKEN_URL, {"error": "invalid_grant"}, status=400)
        with self.assertRaises(TokenExpiredError):
            self.vault.get_valid("loc-1", transport, now=5000.0)
        self.assertEqual(self.vault.connection_state("loc-1", now=5000.0), STATE_TOKEN_EXPIRED)
        self.assertEqual(self.vault.meta("loc-1")["last_error"], "token_expired")

    def test_a_transport_failure_surfaces_the_token_expired_state(self):
        self.saved_token(expires_at=1000.0)

        class Boom:
            def post(self, *a, **k):
                raise OSError("connection reset")

        with self.assertRaises(TokenExpiredError):
            self.vault.get_valid("loc-1", Boom(), now=5000.0)
        self.assertEqual(self.vault.connection_state("loc-1", now=5000.0), STATE_TOKEN_EXPIRED)

    def test_an_expired_token_without_a_refresh_token_is_token_expired(self):
        self.vault.save("loc-1", Token(access_token="only-access", refresh_token=None,
                                       expires_at=1000.0))
        with self.assertRaises(TokenExpiredError) as ctx:
            self.vault.get_valid("loc-1", token_response(), now=5000.0)
        self.assertEqual(str(ctx.exception), "no_refresh_token")
        self.assertEqual(self.vault.connection_state("loc-1", now=5000.0), STATE_TOKEN_EXPIRED)

    def test_a_refresh_token_keeps_the_connection_alive_while_the_access_token_ages(self):
        self.saved_token(expires_at=1000.0)
        self.assertEqual(self.vault.connection_state("loc-1", now=5000.0), STATE_CONNECTED)


# ===================================================================== #
# pacing + backoff
# ===================================================================== #
class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class PacingTests(unittest.TestCase):
    def test_the_default_is_well_under_300_qpm(self):
        self.assertLess(DEFAULT_QPM, 300)
        self.assertEqual(TokenBucket().qpm, DEFAULT_QPM)

    def test_a_rate_at_or_above_300_is_refused(self):
        for qpm in (300, 500):
            with self.assertRaises(ValueError):
                TokenBucket(qpm=qpm)

    def test_a_burst_is_allowed_then_the_bucket_paces(self):
        clock = FakeClock()
        bucket = TokenBucket(qpm=60, burst=2, clock=clock.now, sleep=clock.sleep)
        bucket.acquire()
        bucket.acquire()
        self.assertEqual(clock.slept, [])
        bucket.acquire()
        self.assertEqual(len(clock.slept), 1)
        self.assertAlmostEqual(clock.slept[0], 1.0, places=6)
        self.assertEqual(bucket.calls, 3)

    def test_would_wait_reports_exhaustion(self):
        clock = FakeClock()
        bucket = TokenBucket(qpm=60, burst=1, clock=clock.now, sleep=clock.sleep)
        self.assertFalse(bucket.would_wait())
        bucket.acquire()
        self.assertTrue(bucket.would_wait())


class BackoffTests(unittest.TestCase):
    def test_429_is_retried_with_exponential_backoff_and_jitter(self):
        transport = MockTransport()
        transport.fail_429("https://api.example.com/v1/accounts", times=2)
        transport.register("GET", "https://api.example.com/v1/accounts", {"accounts": []})
        clock = FakeClock()
        client = HttpClient(transport, max_attempts=3, base_delay=1.0,
                            sleep=clock.sleep, jitter=lambda: 1.0)
        payload = client.request("GET", "https://api.example.com/v1/accounts")
        self.assertEqual(payload, {"accounts": []})
        self.assertEqual(len(clock.slept), 2)
        # base * 2**attempt * (1 + jitter) with jitter fixed at 1.0 -> 2x, 4x
        self.assertAlmostEqual(clock.slept[0], 2.0, places=6)
        self.assertAlmostEqual(clock.slept[1], 4.0, places=6)
        self.assertGreater(clock.slept[1], clock.slept[0])

    def test_a_persistent_429_becomes_a_rate_limited_error(self):
        transport = MockTransport()
        transport.fail_429("https://api.example.com/v1/accounts", times=99)
        clock = FakeClock()
        client = HttpClient(transport, max_attempts=3, sleep=clock.sleep,
                            jitter=lambda: 0.0)
        with self.assertRaises(RateLimitedError):
            client.request("GET", "https://api.example.com/v1/accounts")
        self.assertEqual(len(clock.slept), 2)

    def test_retry_after_is_honoured(self):
        transport = MockTransport()
        transport.fail_429("https://api.example.com/v1/accounts", times=1, retry_after=7.0)
        transport.register("GET", "https://api.example.com/v1/accounts", {"ok": True})
        clock = FakeClock()
        client = HttpClient(transport, max_attempts=3, base_delay=1.0,
                            sleep=clock.sleep, jitter=lambda: 0.0)
        client.request("GET", "https://api.example.com/v1/accounts")
        self.assertEqual(clock.slept, [7.0])

    def test_401_is_never_retried(self):
        transport = MockTransport()
        transport.always_401("https://api.example.com/v1/accounts")
        clock = FakeClock()
        client = HttpClient(transport, max_attempts=3, sleep=clock.sleep)
        with self.assertRaises(AuthError):
            client.request("GET", "https://api.example.com/v1/accounts")
        self.assertEqual(clock.slept, [])

    def test_the_gbp_client_paces_every_call(self):
        clock = FakeClock()
        bucket = TokenBucket(qpm=60, burst=1, clock=clock.now, sleep=clock.sleep)
        transport = MockTransport()
        transport.register("GET", "https://api.example.com/v1/accounts", {"accounts": []})
        client = GbpClient(http=transport, quota=bucket, base="https://api.example.com")
        client.list_accounts()
        client.list_accounts()
        self.assertEqual(bucket.calls, 2)
        self.assertEqual(len(clock.slept), 1)


# ===================================================================== #
# read-only live sync
# ===================================================================== #
def sync_transport() -> MockTransport:
    transport = MockTransport()
    transport.register(
        "GET", "https://mybusiness.googleapis.com/v1/accounts",
        {"accounts": [{"name": "accounts/1001", "accountName": "Acme Group"}]})
    transport.register(
        "GET", "https://mybusiness.googleapis.com/v1/accounts/1001/locations",
        {"locations": [
            {"name": "accounts/1001/locations/loc-1", "title": "Sunny Nails Spa",
             "storefrontAddress": {"postalCode": "94103", "locality": "San Francisco"}},
            {"name": "accounts/1001/locations/loc-2", "title": "Sunny Nails 2",
             "storefrontAddress": {"postalCode": "94104"}},
        ]})
    return transport


class LiveSyncTests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.saved_token(expires_at=1e12)

    def _sync(self, transport=None, *, http=None, tenant="loc-1", **kwargs):
        return LiveLocationSync(self.vault, tenant, transport=transport,
                                http=http if http is not None
                                else HttpClient(transport if transport is not None
                                                else sync_transport()),
                                **kwargs)

    def test_locations_are_mapped_into_the_dashboard_model(self):
        bundle = self._sync().sync()
        self.assertEqual(bundle["sync_status"], "ok")
        first = bundle["locations"][0]
        self.assertEqual(first["business_name"], "Sunny Nails Spa")
        self.assertEqual(first["zip"], "94103")
        self.assertEqual(first["city"], "San Francisco")
        self.assertEqual(first["location_id"], "accounts/1001/locations/loc-1")
        self.assertEqual(first["sync_status"], "ok")
        self.assertTrue(first["last_sync"])
        self.assertEqual(bundle["accounts"][0]["account_name"], "Acme Group")

    def test_the_sync_is_read_only_and_authorized(self):
        transport = sync_transport()
        self._sync(http=HttpClient(transport)).sync()
        self.assertTrue(transport.calls)
        for call in transport.calls:
            self.assertEqual(call["method"], "GET")
            self.assertEqual(call["headers"].get("Authorization"),
                             "Bearer access-token-probe")

    def test_every_row_carries_its_tenant(self):
        bundle = self._sync().sync()
        for row in bundle["locations"]:
            self.assertEqual(row["tenant_id"], "loc-1")
        self.assertEqual(scope_rows(bundle["locations"], "loc-1"), bundle["locations"])
        self.assertEqual(scope_rows(bundle["locations"], "loc-2"), [])

    def test_the_sync_result_is_recorded_for_the_dashboard(self):
        bundle = self._sync().sync()
        meta = self.vault.meta("loc-1")
        self.assertEqual(meta["sync_status"], "ok")
        self.assertEqual(meta["last_sync_at"], bundle["last_sync"])

    def test_the_authorized_http_wrapper_never_reuses_another_tenants_token(self):
        self.saved_token("loc-2", access="other-access", expires_at=1e12)
        transport = MockTransport()
        transport.register("GET", "https://api.example.com/x", {"ok": True})
        wrapper = AuthorizedHttp(HttpClient(transport),
                                 lambda: self.vault.get_valid("loc-2", None))
        wrapper.request("GET", "https://api.example.com/x")
        # loc-2's token is the one presented, and loc-1's is untouched.
        self.assertEqual(transport.calls[0]["headers"]["Authorization"],
                         "Bearer other-access")
        self.assertEqual(self.vault.load("loc-1").access_token, "access-token-probe")

    def test_a_token_expired_tenant_surfaces_the_state_instead_of_raising(self):
        self.vault.save("loc-1", Token(access_token="stale", refresh_token=None,
                                       expires_at=1.0))
        bundle = self._sync().sync()
        self.assertEqual(bundle["sync_status"], "token_expired")
        self.assertEqual(bundle["locations"], [])

    def test_a_429_is_absorbed_by_the_backoff(self):
        transport = sync_transport()
        transport.fail_429("https://mybusiness.googleapis.com/v1/accounts", times=1)
        clock = FakeClock()
        http = HttpClient(transport, max_attempts=3, base_delay=0.5,
                          sleep=clock.sleep, jitter=lambda: 0.0)
        bundle = self._sync(http=http).sync()
        self.assertEqual(bundle["sync_status"], "ok")
        self.assertEqual(len(clock.slept), 1)

    def test_a_persistent_429_surfaces_as_a_rate_limited_error(self):
        transport = sync_transport()
        transport.fail_429("https://mybusiness.googleapis.com/v1/accounts", times=99)
        http = HttpClient(transport, max_attempts=2, base_delay=0.0,
                          sleep=lambda s: None, jitter=lambda: 0.0)
        with self.assertRaises(RateLimitedError):
            self._sync(http=http).sync()

    def test_a_5xx_is_retried_then_surfaces(self):
        transport = MockTransport()
        transport.register("GET", "https://mybusiness.googleapis.com/v1/accounts",
                           {"error": {"message": "backend error"}}, status=500)
        clock = FakeClock()
        http = HttpClient(transport, max_attempts=2, base_delay=1.0,
                          sleep=clock.sleep, jitter=lambda: 0.0)
        with self.assertRaises(ApiError):
            self._sync(http=http).sync()
        self.assertEqual(len(clock.slept), 1)


# ===================================================================== #
# dashboard projection + endpoint
# ===================================================================== #
class ConnectionRowsTests(VaultTestCase):
    def test_unknown_businesses_report_not_connected(self):
        rows = connection_rows(
            [{"location_id": 1, "location_name": "accounts/1001/locations/loc-1",
              "business_name": "Sunny Nails Spa", "last_sync_at": None}],
            self.vault, now=1000.0)
        self.assertEqual(rows[0]["connection_state"], STATE_NOT_CONNECTED)
        self.assertEqual(rows[0]["connection_label"], "Not connected")

    def test_connected_and_expired_are_reported_with_a_last_sync(self):
        self.saved_token("accounts/1001/locations/loc-1", expires_at=1e12)
        self.vault.note_sync("accounts/1001/locations/loc-1", "ok", "2026-01-01T00:00:00Z")
        self.vault.save("accounts/1001/locations/loc-2",
                        Token(access_token="", refresh_token=None, expires_at=1.0))
        rows = connection_rows([
            {"location_id": 1, "location_name": "accounts/1001/locations/loc-1",
             "business_name": "A", "last_sync_at": None},
            {"location_id": 2, "location_name": "accounts/1001/locations/loc-2",
             "business_name": "B", "last_sync_at": None},
        ], self.vault, now=2000.0)
        self.assertEqual(rows[0]["connection_state"], STATE_CONNECTED)
        self.assertEqual(rows[0]["google_last_sync_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(rows[1]["connection_state"], STATE_TOKEN_EXPIRED)
        self.assertEqual(rows[1]["connection_label"], "Token expired or revoked")

    def test_disconnect_forgets_only_one_tenant(self):
        self.saved_token("loc-1")
        self.saved_token("loc-2")
        result = disconnect("loc-1", self.vault)
        self.assertTrue(result["disconnected"])
        self.assertEqual(result["connection_state"], STATE_NOT_CONNECTED)
        self.assertIsNotNone(self.vault.load("loc-2"))


class DashboardEndpointTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.repo.upsert_location(acct, "accounts/1001/locations/loc-001",
                                  title="Demo Nails A", city="Garden Grove",
                                  state="CA", language="en")

    def test_fixture_mode_is_the_default_and_touches_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            payload = _google_connections(self.repo)
        self.assertEqual(payload, {"live": False, "connections": []})

    def test_live_requires_every_credential(self):
        env = full_env()
        env.pop("GBP_OAUTH_CLIENT_SECRET")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(_google_connections(self.repo)["live"], False)

    def test_live_mode_reports_per_business_connection_state(self):
        tmp = tempfile.mkdtemp(prefix="gbp-endpoint-vault-")
        env = full_env(GBP_TOKEN_VAULT_DIR=tmp)
        vault = TokenVault(tmp, VAULT_KEY, cipher=None)
        vault.save("accounts/1001/locations/loc-001",
                   Token(access_token="access-token-probe", refresh_token="refresh-token-probe",
                         expires_at=4_000_000_000.0))
        with mock.patch.dict(os.environ, env, clear=True):
            payload = _google_connections(self.repo)
        self.assertTrue(payload["live"])
        row = payload["connections"][0]
        self.assertEqual(row["connection_state"], STATE_CONNECTED)
        self.assertEqual(row["google_last_sync_at"], None)
        self.assertNotIn("access-token-probe", json.dumps(payload))

    def test_the_route_answers_over_http_in_fixture_mode(self):
        service = ApprovalService(self.repo)
        server = create_server(service, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            with mock.patch.dict(os.environ, {}, clear=True):
                with urllib.request.urlopen(
                        f"http://{host}:{port}/api/google/connections", timeout=10) as resp:
                    body = json.loads(resp.read().decode())
            self.assertEqual(body, {"live": False, "connections": []})
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
