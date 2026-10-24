"""The REAL (socket) transport for live use — stdlib ``urllib`` only.

Group 8B / T26 discovered that the repository had **no live transport at all**:
``HttpClient`` takes a transport but only :class:`~gbp.client.http_client.MockTransport`
existed, so ``cli._live_client()`` could never have worked (it also called
``HttpClient(timeout=...)`` without the required positional argument).

This module supplies that missing piece, with defence in depth:

* every request to a **Google host** is refused unless ``GBP_ALLOW_LIVE_GOOGLE=1``
  is set explicitly (so no code path in this repository can reach Google by
  accident, even if a caller forgets the outer guard), and
* error statuses are returned as :class:`Response` objects (not raised) so the
  retry/backoff/``Retry-After`` logic stays in exactly one place:
  :class:`gbp.client.http_client.HttpClient`.

Only socket-level failures raise (:class:`ApiTimeout`), which both the HTTP
client and the sync queue treat as retryable.
"""

from __future__ import annotations

import json as _json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from gbp.auth.oauth import LiveGoogleNotAllowedError, live_google_allowed
from gbp.client.http_client import ApiTimeout, Response

__all__ = ["UrllibTransport", "GOOGLE_HOSTS"]

#: Hosts treated as Google. Anything else is not blocked by the guard.
GOOGLE_HOSTS: Tuple[str, ...] = ("googleapis.com", "google.com")


class UrllibTransport:
    """A minimal real transport satisfying the :class:`Transport` protocol.

    ``opener`` is injectable so a deployment (or a test) can supply a custom
    opener without touching the network; the default is
    :func:`urllib.request.build_opener`.
    """

    def __init__(
        self,
        *,
        opener: Any = None,
        allowed_hosts: Tuple[str, ...] = GOOGLE_HOSTS,
        user_agent: str = "steven-ai-os-gbp/1.0 (offline-ready)",
    ) -> None:
        self._opener = opener if opener is not None else urllib.request.build_opener()
        self.allowed_hosts = tuple(allowed_hosts)
        self.user_agent = user_agent
        #: Number of requests actually sent (introspection for tests/tools).
        self.requests = 0

    # ------------------------------------------------------------------ #
    def _guard(self, url: str) -> None:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not host:
            return
        if not any(host == h or host.endswith("." + h) for h in self.allowed_hosts):
            return
        if not live_google_allowed():
            raise LiveGoogleNotAllowedError(
                "refusing to send a request to a Google host without "
                "GBP_ALLOW_LIVE_GOOGLE=1"
            )

    def _send(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> Response:
        self._guard(url)
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
            url = f"{url}{'&' if '?' in url else '?'}{query}"

        hdrs = {"User-Agent": self.user_agent}
        hdrs.update({k: str(v) for k, v in (headers or {}).items()})
        body = None
        if data is not None:
            # A JSON API (the GBP v4 reply write, Issue #126) is told apart by
            # its Content-Type: encode JSON and keep that header. Anything else
            # (the OAuth token exchange) keeps the form encoding.
            if (hdrs.get("Content-Type") or "").lower().startswith("application/json"):
                body = _json.dumps(data).encode("utf-8")
            else:
                body = urllib.parse.urlencode(data).encode("utf-8")
                hdrs.setdefault("Content-Type",
                                "application/x-www-form-urlencoded")

        request = urllib.request.Request(url, data=body, headers=hdrs,
                                         method=method.upper())
        self.requests += 1
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read()
                status = int(response.getcode() or 200)
                resp_headers = {k: v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            # Error statuses are RETURNED so HttpClient owns retry policy.
            raw = exc.read() or b""
            status = int(exc.code)
            resp_headers = {k: v for k, v in (exc.headers or {}).items()}
        except (socket.timeout, TimeoutError) as exc:
            raise ApiTimeout(f"request timed out after {timeout}s") from exc
        except urllib.error.URLError as exc:
            raise ApiTimeout(f"network error: {exc.reason}") from exc

        try:
            payload: Any = _json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        return Response(status=status, payload=payload, headers=resp_headers)

    # -- Transport protocol --------------------------------------------- #
    def send(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> Response:
        """Dispatch ANY method (incl. ``PUT``) with the same guard/contract.

        ``HttpClient`` prefers this so a write keeps its real verb (the reply
        ``PUT``, Issue #126) instead of being folded into a ``POST``.
        """
        return self._send(method, url, params=params, headers=headers,
                          data=data, timeout=timeout)

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
    ) -> Response:
        return self._send("GET", url, params=params, headers=headers,
                          timeout=timeout)

    def post(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
    ) -> Response:
        return self._send("POST", url, data=data, headers=headers,
                          timeout=timeout)
