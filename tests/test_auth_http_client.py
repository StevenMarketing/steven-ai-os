"""Tests for the OAuth token manager and the retrying HTTP client.

Fully offline: no network, no Google, standard library only.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest

from gbp.auth.token_manager import Token, TokenManager
from gbp.client.http_client import (
    ApiError,
    AuthError,
    HttpClient,
    MockTransport,
    RateLimitedError,
)

REFRESH_URL = "https://oauth2.googleapis.com/token"
URL = "https://api.example.com/thing"


class RecordingSleep:
    """Injectable sleep that records the requested delays (never really waits)."""

    def __init__(self) -> None:
        self.delays = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# --------------------------------------------------------------------------- #
# Token file handling
# --------------------------------------------------------------------------- #
class TokenFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Nested path exercises the mkdir(parents=True) branch.
        self.path = os.path.join(self.tmp.name, "nested", "token.json")

    def test_save_creates_file_with_mode_0600(self) -> None:
        manager = TokenManager(self.path, refresh_url=REFRESH_URL)
        manager.token = Token(access_token="a", refresh_token="r", expires_at=123.0, scopes=["s"])
        manager.save()

        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["refresh_token"], "r")
        self.assertEqual(data["scopes"], ["s"])

    def test_save_reuses_existing_file_with_0600(self) -> None:
        manager = TokenManager(self.path, refresh_url=REFRESH_URL)
        manager.token = Token(access_token="a")
        manager.save()
        # Loosen the mode, then save again: it must be tightened back.
        os.chmod(self.path, 0o644)
        manager.save()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_load_missing_file_returns_none(self) -> None:
        manager = TokenManager(self.path, refresh_url=REFRESH_URL)
        self.assertIsNone(manager.load())
        self.assertIsNone(manager.token)

    def test_save_load_round_trip(self) -> None:
        manager = TokenManager(self.path, refresh_url=REFRESH_URL)
        manager.token = Token(access_token="a2", refresh_token="r2", expires_at=999.0, scopes=["x", "y"])
        manager.save()

        other = TokenManager(self.path, refresh_url=REFRESH_URL)
        loaded = other.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.access_token, "a2")
        self.assertEqual(loaded.refresh_token, "r2")
        self.assertEqual(loaded.expires_at, 999.0)
        self.assertEqual(loaded.scopes, ["x", "y"])

    def test_repr_redacts_token_values(self) -> None:
        text = repr(Token(access_token="super-secret", refresh_token="also-secret"))
        self.assertNotIn("super-secret", text)
        self.assertNotIn("also-secret", text)


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #
class TokenExpiryTests(unittest.TestCase):
    def test_expired_when_in_the_past(self) -> None:
        manager = TokenManager("unused.json", clock=lambda: 1000.0)
        manager.token = Token(access_token="a", expires_at=900.0)
        self.assertTrue(manager.is_expired())

    def test_valid_when_in_the_future(self) -> None:
        manager = TokenManager("unused.json", clock=lambda: 1000.0)
        manager.token = Token(access_token="a", expires_at=2000.0)
        self.assertFalse(manager.is_expired())

    def test_skew_seconds_is_honoured(self) -> None:
        manager = TokenManager("unused.json", clock=lambda: 1000.0)
        manager.token = Token(access_token="a", expires_at=1030.0)
        self.assertFalse(manager.is_expired(skew_seconds=10))
        self.assertTrue(manager.is_expired(skew_seconds=60))

    def test_missing_token_is_expired(self) -> None:
        self.assertTrue(TokenManager("unused.json", clock=lambda: 0.0).is_expired())

    def test_empty_access_token_is_expired(self) -> None:
        manager = TokenManager("unused.json", clock=lambda: 0.0)
        manager.token = Token(access_token="", expires_at=99999.0)
        self.assertTrue(manager.is_expired())


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #
class TokenRefreshTests(unittest.TestCase):
    def _manager(self, clock=lambda: 1000.0) -> TokenManager:
        return TokenManager(
            "unused.json",
            refresh_url=REFRESH_URL,
            client_id="cid",
            client_secret="csecret",
            clock=clock,
        )

    def test_refresh_success(self) -> None:
        manager = self._manager()
        manager.token = Token(access_token="old", refresh_token="r1", expires_at=0.0)

        transport = MockTransport()
        transport.register(
            "POST", REFRESH_URL, {"access_token": "new", "expires_in": 3600, "token_type": "Bearer"}
        )

        token = manager.refresh(transport)
        self.assertEqual(token.access_token, "new")
        self.assertEqual(manager.token.access_token, "new")
        self.assertEqual(token.expires_at, 4600.0)  # clock 1000 + 3600
        self.assertEqual(token.refresh_token, "r1")  # preserved when not re-issued

        call = transport.calls[0]
        self.assertEqual(call["url"], REFRESH_URL)
        self.assertEqual(call["data"]["grant_type"], "refresh_token")
        self.assertEqual(call["data"]["refresh_token"], "r1")

    def test_refresh_401_raises_auth_error(self) -> None:
        manager = self._manager()
        manager.token = Token(access_token="old", refresh_token="r1", expires_at=0.0)
        transport = MockTransport()
        transport.always_401(REFRESH_URL)
        with self.assertRaises(AuthError):
            manager.refresh(transport)

    def test_refresh_without_refresh_token_raises_auth_error(self) -> None:
        manager = self._manager()
        manager.token = Token(access_token="old", refresh_token=None)
        with self.assertRaises(AuthError):
            manager.refresh(MockTransport())

    def test_refresh_without_access_token_in_body_raises(self) -> None:
        manager = self._manager()
        manager.token = Token(access_token="old", refresh_token="r1")
        transport = MockTransport()
        transport.register("POST", REFRESH_URL, {"expires_in": 3600})
        with self.assertRaises(AuthError):
            manager.refresh(transport)

    def test_ensure_valid_does_not_refresh_when_valid(self) -> None:
        manager = self._manager()
        manager.token = Token(access_token="valid", refresh_token="r1", expires_at=99999.0)
        transport = MockTransport()
        transport.register("POST", REFRESH_URL, {"access_token": "new", "expires_in": 3600})
        manager.ensure_valid(transport)
        self.assertEqual(manager.token.access_token, "valid")
        self.assertEqual(transport.calls, [])

    def test_ensure_valid_refreshes_when_expired(self) -> None:
        manager = self._manager()
        manager.token = Token(access_token="old", refresh_token="r1", expires_at=0.0)
        transport = MockTransport()
        transport.register("POST", REFRESH_URL, {"access_token": "new", "expires_in": 3600})
        manager.ensure_valid(transport)
        self.assertEqual(manager.token.access_token, "new")
        self.assertEqual(len(transport.calls), 1)


# --------------------------------------------------------------------------- #
# HTTP client: retry / classification
# --------------------------------------------------------------------------- #
class HttpClientRetryTests(unittest.TestCase):
    def test_429_then_success_retries_with_backoff(self) -> None:
        transport = MockTransport()
        transport.register("GET", URL, {"ok": True})
        transport.fail_429(URL, times=2)
        sleep = RecordingSleep()
        client = HttpClient(transport, max_attempts=3, base_delay=0.01, sleep=sleep, jitter=lambda: 1.0)

        self.assertEqual(client.request("GET", URL), {"ok": True})
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(len(sleep.delays), 2)
        # Exponential: the second wait is strictly longer than the first.
        self.assertLess(sleep.delays[0], sleep.delays[1])
        self.assertAlmostEqual(sleep.delays[0], 0.02)

    def test_429_exhausts_attempts_then_rate_limited(self) -> None:
        transport = MockTransport()
        transport.register("GET", URL, {"ok": True})
        transport.fail_429(URL, times=99)
        sleep = RecordingSleep()
        client = HttpClient(transport, max_attempts=3, base_delay=0.01, sleep=sleep, jitter=lambda: 1.0)

        with self.assertRaises(RateLimitedError):
            client.request("GET", URL)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(len(sleep.delays), 2)

    def test_retry_after_header_is_honoured(self) -> None:
        transport = MockTransport()
        transport.register("GET", URL, {"ok": True})
        transport.fail_429(URL, times=1, retry_after=7)
        sleep = RecordingSleep()
        client = HttpClient(transport, max_attempts=3, base_delay=0.01, sleep=sleep, jitter=lambda: 1.0)

        self.assertEqual(client.request("GET", URL), {"ok": True})
        self.assertEqual(sleep.delays, [7.0])

    def test_401_raises_auth_error_and_is_not_retried(self) -> None:
        transport = MockTransport()
        transport.register("GET", URL, {"ok": True})
        transport.always_401(URL)
        sleep = RecordingSleep()
        client = HttpClient(transport, max_attempts=3, sleep=sleep)

        with self.assertRaises(AuthError):
            client.request("GET", URL)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleep.delays, [])

    def test_5xx_is_retried_then_raises_api_error(self) -> None:
        transport = MockTransport()
        transport.register("GET", URL, {"error": {"message": "boom"}}, status=503)
        sleep = RecordingSleep()
        client = HttpClient(transport, max_attempts=2, base_delay=0.01, sleep=sleep, jitter=lambda: 0.0)

        with self.assertRaises(ApiError):
            client.request("GET", URL)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(sleep.delays), 1)

    def test_other_4xx_raises_api_error_without_retry(self) -> None:
        transport = MockTransport()
        transport.register("GET", URL, {"error": {"message": "nope"}}, status=404)
        sleep = RecordingSleep()
        client = HttpClient(transport, max_attempts=3, sleep=sleep)

        with self.assertRaises(ApiError) as ctx:
            client.request("GET", URL)
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleep.delays, [])

    def test_unregistered_route_surfaces_as_api_error(self) -> None:
        client = HttpClient(MockTransport())
        with self.assertRaises(ApiError):
            client.request("GET", "https://api.example.com/missing")

    def test_post_sends_data_and_records_it(self) -> None:
        transport = MockTransport()
        transport.register("POST", URL, {"id": "1"})
        transport.register("PUT", URL, {"id": "1"}, params={})  # not used; documents intent
        client = HttpClient(transport, sleep=RecordingSleep())
        # MockTransport.post records the body under "data".
        result = client.request("POST", URL, data={"hello": "world"})
        self.assertEqual(result, {"id": "1"})
        self.assertEqual(transport.calls[0]["data"], {"hello": "world"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
