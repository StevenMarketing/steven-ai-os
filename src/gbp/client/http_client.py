"""Retrying HTTP client for the GBP integration (offline, stdlib only).

The module defines:

* :class:`Response`      -- the value a transport returns (status/headers/JSON).
* :class:`Transport`     -- the protocol a transport must satisfy.
* :class:`MockTransport` -- an offline transport serving registered responses.
* :class:`HttpClient`    -- exponential backoff with jitter, ``Retry-After``
  support and typed error classification.

Error handling
--------------
* ``401`` -> :class:`AuthError`, never retried.
* ``429`` -> retried with backoff, then :class:`RateLimitedError`.
* ``5xx`` -> retried with backoff, then :class:`ApiError`.
* other ``4xx`` -> :class:`ApiError`, never retried.

``AuthError`` / ``RateLimitedError`` are shared with
:mod:`gbp.client.mock_client` so a transport can raise exactly the type the
caller catches. Sleeping is injectable, so tests never really wait.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from gbp.client.mock_client import AuthError, GBPError, RateLimitedError

__all__ = [
    "ApiError",
    "ApiTimeout",
    "AuthError",
    "GBPError",
    "RateLimitedError",
    "Response",
    "Transport",
    "MockTransport",
    "HttpClient",
]


class ApiError(GBPError):
    """HTTP error that is not worth retrying (other 4xx, or 5xx after retries)."""

    code = 400


class ApiTimeout(GBPError):
    """A socket-level timeout (retryable; surfaced after retries are exhausted)."""

    code = 408


def _parse_retry_after(headers: Optional[Dict[str, Any]]) -> Optional[float]:
    """Return the ``Retry-After`` value in seconds, or ``None`` if unusable."""
    raw = None
    for key, value in (headers or {}).items():
        if str(key).lower() == "retry-after":
            raw = value
            break
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


class Response:
    """Minimal HTTP response value object."""

    def __init__(
        self,
        status: int = 200,
        payload: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.status = int(status)
        self.payload = payload if payload is not None else {}
        self.headers = dict(headers or {})

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        payload = self.payload
        return payload() if callable(payload) else payload

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Response(status={self.status}, headers={sorted(self.headers)})"


@runtime_checkable
class Transport(Protocol):
    """The two-method protocol every transport (real or mock) must satisfy."""

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
    ) -> Response:  # pragma: no cover - protocol
        ...

    def post(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
    ) -> Response:  # pragma: no cover - protocol
        ...


def _matches(required: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    """True when every ``required`` key/value is present in ``actual``."""
    return all(actual.get(key) == value for key, value in required.items())


class MockTransport:
    """Offline transport serving registered responses.

    Routes are matched by ``method`` + ``url`` (+ an optional params subset).
    The **first registered matching route wins**, so register param-specific
    routes (e.g. a ``pageToken`` page) before a generic one.

    Failure injection:

    * :meth:`fail_429` -- return ``429`` for a URL a given number of times,
      optionally with a ``Retry-After`` header.
    * :meth:`always_401` -- return ``401`` for a URL, unconditionally.

    Every request is appended to :attr:`calls` (method, url, params, data,
    headers) so tests can assert on the exact endpoint shape.
    """

    def __init__(self) -> None:
        self.routes: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []
        self._fail429: Dict[str, int] = {}
        self._retry_after: Dict[str, Optional[float]] = {}
        self._always401: set = set()

    # -- registration ------------------------------------------------------ #
    def register(
        self,
        method: str,
        url: str,
        payload: Optional[Any] = None,
        status: int = 200,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> "MockTransport":
        self.routes.append(
            {
                "method": method.upper(),
                "url": url,
                "payload": payload if payload is not None else {},
                "status": int(status),
                "headers": dict(headers or {}),
                "params": dict(params or {}),
            }
        )
        return self

    # Convenience alias kept close to the HTTP vocabulary.
    add_response = register

    def fail_429(self, url: str, times: int = 1, retry_after: Optional[float] = None) -> "MockTransport":
        self._fail429[url] = int(times)
        self._retry_after[url] = retry_after
        return self

    def always_401(self, url: str) -> "MockTransport":
        self._always401.add(url)
        return self

    # -- Transport protocol ------------------------------------------------ #
    def send(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Any] = None,
        timeout: float = 15.0,
    ) -> Response:
        """Dispatch ANY method (incl. ``PUT``) and return the raw ``Response``.

        ``HttpClient`` uses this so a write is sent with its real verb instead of
        being folded into ``post`` (Issue #126's reply ``PUT``). Same
        single-attempt contract as :meth:`get` / :meth:`post`.
        """
        return self._handle(method, url, params=params, headers=headers,
                            data=data, timeout=timeout)

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
    ) -> Response:
        return self._handle("GET", url, params=params, headers=headers, data=None, timeout=timeout)

    def post(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
    ) -> Response:
        return self._handle("POST", url, params=None, headers=headers, data=data, timeout=timeout)

    # -- HttpClient-compatible convenience --------------------------------- #
    def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Any] = None,
    ) -> Any:
        """Dispatch and return the parsed JSON body -- single attempt, no retry.

        Mirrors :meth:`HttpClient.request` so a bare ``MockTransport`` can be
        handed straight to :class:`~gbp.client.gbp_client.GbpClient` in tests;
        wrap it in an ``HttpClient`` when retry behaviour is under test.
        """
        response = self._handle(
            method.upper(), url, params=params, headers=headers, data=data
        )
        payload = response.json()
        if 200 <= response.status < 300:
            return payload
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else "request failed"
        body = payload if isinstance(payload, dict) else {}
        if response.status == 401:
            raise AuthError(str(message), code=401, payload=body)
        if response.status == 429:
            raise RateLimitedError(str(message), code=429, payload=body)
        raise ApiError(str(message), code=response.status, payload=body)

    # -- dispatch ---------------------------------------------------------- #
    def _handle(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Any] = None,
        timeout: float = 15.0,
    ) -> Response:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "data": data,
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )

        if url in self._always401:
            return Response(401, {"error": {"code": 401, "message": "Unauthorized"}})

        remaining = self._fail429.get(url, 0)
        if remaining > 0:
            self._fail429[url] = remaining - 1
            retry_after = self._retry_after.get(url)
            response_headers = {}
            if retry_after is not None:
                response_headers["Retry-After"] = str(retry_after)
            return Response(
                429,
                {"error": {"code": 429, "message": "Rate limited"}},
                response_headers,
            )

        for route in self.routes:
            if route["method"] != method or route["url"] != url:
                continue
            if not _matches(route["params"], dict(params or {})):
                continue
            return Response(route["status"], route["payload"], route["headers"])

        return Response(404, {"error": {"code": 404, "message": f"no route for {method} {url}"}})


class HttpClient:
    """Thin retrying wrapper around a :class:`Transport`.

    ``request`` returns the parsed JSON body of a successful response and raises
    a typed error otherwise. Backoff is ``base_delay * 2**attempt`` with jitter
    in ``[1, 2)`` so parallel clients do not retry in lockstep. Both ``sleep``
    and the jitter source are injectable, so tests run instantly.
    """

    def __init__(
        self,
        transport: Any,
        max_attempts: int = 3,
        base_delay: float = 0.01,
        sleep: Optional[Callable[[float], None]] = None,
        jitter: Optional[Callable[[], float]] = None,
        timeout: float = 15.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.transport = transport
        self.max_attempts = int(max_attempts)
        self.base_delay = float(base_delay)
        self._sleep: Callable[[float], None] = sleep or time.sleep
        self._random: Callable[[], float] = jitter or random.random
        #: Socket-level timeout (seconds) passed to the transport on every call.
        self.timeout = float(timeout)
        self.calls = 0

    # -- internals --------------------------------------------------------- #
    def _backoff(self, attempt: int) -> float:
        """Exponential delay with jitter for a zero-based ``attempt`` index."""
        return self.base_delay * (2 ** attempt) * (1.0 + self._random())

    def _send(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]],
        headers: Optional[Dict[str, str]],
        data: Optional[Any],
    ) -> Response:
        # Prefer a method-aware ``send`` (so a PUT stays a PUT -- the reply
        # write, Issue #126); fall back to the GET/POST protocol for a transport
        # that only implements get/post.
        send = getattr(self.transport, "send", None)
        if callable(send):
            return send(method, url, params=params, headers=headers, data=data,
                        timeout=self.timeout)
        if method == "GET":
            return self.transport.get(url, params=params, headers=headers, timeout=self.timeout)
        return self.transport.post(url, data=data, headers=headers, timeout=self.timeout)

    @staticmethod
    def _message(response: Response, fallback: str) -> str:
        payload = response.json() if hasattr(response, "json") else {}
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or fallback)
            if isinstance(error, str):
                return error
        return fallback

    # -- public API -------------------------------------------------------- #
    def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Any] = None,
    ) -> Any:
        method = method.upper()
        for attempt in range(self.max_attempts):
            self.calls += 1
            last = attempt == self.max_attempts - 1
            try:
                response = self._send(method, url, params, headers, data)
            except ApiTimeout:
                if last:
                    raise
                self._sleep(self._backoff(attempt))
                continue
            except (TimeoutError, OSError) as exc:
                if last:
                    raise ApiTimeout(f"request timed out after {self.timeout}s") from exc
                self._sleep(self._backoff(attempt))
                continue
            status = response.status

            if 200 <= status < 300:
                return response.json()

            retry_after = _parse_retry_after(response.headers)
            retryable = status == 429 or 500 <= status < 600
            if retryable:
                last = attempt == self.max_attempts - 1
                if status == 429:
                    if last:
                        raise RateLimitedError(
                            self._message(response, "rate limited"),
                            code=429,
                            payload=response.json() if isinstance(response.json(), dict) else {},
                        )
                else:
                    if last:
                        raise ApiError(
                            self._message(response, "server error"),
                            code=status,
                            payload=response.json() if isinstance(response.json(), dict) else {},
                        )
                delay = retry_after if retry_after is not None else self._backoff(attempt)
                self._sleep(delay)
                continue

            if status == 401:
                raise AuthError(self._message(response, "unauthorized"), code=401)

            raise ApiError(
                self._message(response, "request failed"),
                code=status,
                payload=response.json() if isinstance(response.json(), dict) else {},
            )

        # Unreachable: every branch above returns or raises.
        raise ApiError("request failed", code=0)
