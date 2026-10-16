"""Mock Google Business Profile client backed entirely by local JSON fixtures.

This is the OFFLINE stand-in for the real GBP API client. It makes no network
calls, needs no credentials, and uses the standard library only. It exists so
the rest of the pipeline (normalisation, storage, reply drafting) can be built
and tested end to end without touching Google.

Behaviour that mirrors the real API:

* ``list_locations(account)``      -> list of location dicts.
* ``fetch_page(location, token)``  -> one raw page dict, with ``nextPageToken``
  when more pages exist. ``token=None`` means "first page".
* ``list_reviews(location)``       -> walks every page and returns all reviews,
  de-duplicated by review ``name`` (the real API can repeat a review across
  pages when it is edited mid-crawl).
* ``RateLimitedError`` / ``AuthError`` -- typed exceptions mirroring HTTP 429
  and 401 so callers can branch without parsing strings.
* ``with_backoff(fn)``             -> retries ``RateLimitedError`` only.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Set, TypeVar

from gbp import fixtures

T = TypeVar("T")

#: First page token sentinel; ``None`` means "start at page 1".
FIRST_PAGE: Optional[str] = None


class GBPError(Exception):
    """Base class for typed GBP client errors."""

    code: int = 0

    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code if code is not None else type(self).code
        self.payload: Dict[str, Any] = payload or {}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


class RateLimitedError(GBPError):
    """HTTP 429 / RESOURCE_EXHAUSTED -- safe to retry."""

    code = 429


class AuthError(GBPError):
    """HTTP 401 / UNAUTHENTICATED -- retrying will not help."""

    code = 401


_ERROR_CLASSES: Dict[str, type] = {
    "error_429": RateLimitedError,
    "429": RateLimitedError,
    "error_401": AuthError,
    "401": AuthError,
}


def _error_message(payload: Dict[str, Any], fallback: str) -> str:
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or fallback)
    return fallback


def with_backoff(
    fn: Callable[[], T],
    attempts: int = 3,
    base_delay: float = 0.0,
    sleep: Optional[Callable[[float], None]] = None,
) -> T:
    """Call ``fn`` and retry on ``RateLimitedError`` with exponential backoff.

    ``AuthError`` is never retried -- it propagates immediately. The delay is
    measured from ``base_delay`` (default 0.0, so tests run instantly); tests
    patch :func:`time.sleep` to assert on retry behaviour.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    _sleep = sleep if sleep is not None else time.sleep
    last_error: Optional[RateLimitedError] = None
    for attempt in range(attempts):
        try:
            return fn()
        except RateLimitedError as exc:  # retryable
            last_error = exc
            if attempt == attempts - 1:
                raise
            _sleep(base_delay * (2 ** attempt))
    # Unreachable: the loop either returns or raises.
    raise last_error if last_error is not None else RuntimeError("with_backoff failed")


def _review_name(review: Dict[str, Any]) -> str:
    return str(review.get("name", ""))


def _belongs_to(review: Dict[str, Any], location: Optional[str]) -> bool:
    """True when ``review`` belongs to ``location`` (shop A/B isolation).

    ``location`` may be ``None``/empty to accept every review.
    """
    if not location:
        return True
    return _review_name(review).startswith(f"{location}/")


def _is_newer_or_equal(candidate: Dict[str, Any], current: Dict[str, Any]) -> bool:
    """Compare two snapshots of the same review by ``updateTime``.

    ISO-8601 timestamps sort lexicographically, so a plain string compare is
    enough (the real API always returns ``Z``-suffixed UTC). On a tie the later
    occurrence wins, mirroring "last one seen is the current state".
    """
    return str(candidate.get("updateTime") or "") >= str(current.get("updateTime") or "")


class MockGBPClient:
    """Fixture-backed GBP client.

    Parameters
    ----------
    fixtures_dir:
        Optional override for the fixtures directory (defaults to the project
        ``fixtures/``). Useful in tests.
    error:
        Optional error fixture name (``"error_429"`` / ``"error_401"``) to make
        the client raise a typed error instead of returning data.
    fail_times:
        How many calls should raise before the client starts succeeding again.
        ``None`` (default) means "always raise" while ``error`` is set.
    error_on:
        Optional iterable of method names the error applies to. Default: every
        method.
    """

    def __init__(
        self,
        fixtures_dir: Any = None,
        error: Optional[str] = None,
        fail_times: Optional[int] = None,
        error_on: Optional[Set[str]] = None,
    ) -> None:
        self.fixtures_dir = fixtures_dir
        self.error = error
        self.fail_times = fail_times
        self.error_on = set(error_on) if error_on is not None else None
        self.calls: List[Dict[str, Any]] = []
        self._failures = 0

        self._accounts = fixtures.list_accounts(fixtures_dir)
        self._locations = fixtures.list_locations(fixtures_dir)
        self._pages = fixtures.list_reviews_pages(fixtures_dir)
        # Token -> raw page dict. FIRST_PAGE (None) maps to page 1.
        self._page_by_token: Dict[Optional[str], Dict[str, Any]] = {
            FIRST_PAGE: self._pages[0],
            self._pages[0].get("nextPageToken"): self._pages[1],
        }

    # -- error injection ---------------------------------------------------

    def _maybe_fail(self, method: str) -> None:
        if not self.error:
            return
        if self.error_on is not None and method not in self.error_on:
            return
        if self.fail_times is not None and self._failures >= self.fail_times:
            return
        self._failures += 1
        fixture_name = self.error if self.error.endswith(".json") else f"{self.error}.json"
        payload = fixtures.load_fixture(fixture_name, self.fixtures_dir)
        key = self.error[:-5] if self.error.endswith(".json") else self.error
        cls = _ERROR_CLASSES.get(key)
        if cls is None:
            raise ValueError(f"unknown mock error: {self.error}")
        raise cls(_error_message(payload, self.error), payload=payload)

    def _record(self, method: str, **kwargs: Any) -> None:
        entry: Dict[str, Any] = {"method": method}
        entry.update(kwargs)
        self.calls.append(entry)

    # -- public API --------------------------------------------------------

    def accounts(self) -> List[Dict[str, Any]]:
        """Return the configured accounts."""
        self._record("accounts")
        self._maybe_fail("accounts")
        return [dict(a) for a in self._accounts]

    def list_locations(self, account: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return the locations of ``account`` (e.g. ``"accounts/1001"``).

        ``account`` may also be an account dict, or omitted to return every
        fixture location.
        """
        self._record("list_locations", account=account)
        self._maybe_fail("list_locations")

        if isinstance(account, dict):
            account = account.get("name")
        if account is None and self._accounts:
            account = self._accounts[0].get("name")

        results = []
        for location in self._locations:
            if account is None or str(location.get("name", "")).startswith(f"{account}/"):
                results.append(dict(location))
        return results

    def fetch_page(self, location: str, page_token: Optional[str] = None) -> Dict[str, Any]:
        """Return one raw reviews page for ``location``.

        ``page_token=None`` fetches the first page. The returned dict contains
        ``reviews`` and, on every page but the last, ``nextPageToken``.
        """
        self._record("fetch_page", location=location, page_token=page_token)
        self._maybe_fail("fetch_page")

        page = self._page_by_token.get(page_token)
        if page is None:
            raise ValueError(f"unknown page token: {page_token!r}")
        return dict(page)

    def _merge_review(self, seen: Dict[str, Dict[str, Any]], review: Dict[str, Any]) -> None:
        """Insert/refresh ``review`` in ``seen`` keyed by review ``name``.

        When a review ``name`` repeats (across pages or within one page), the
        snapshot with the newer ``updateTime`` wins (D2 fix); the original
        first-seen position is preserved.
        """
        name = _review_name(review)
        current = seen.get(name)
        if current is None or _is_newer_or_equal(review, current):
            seen[name] = dict(review)

    def _collect(self, seen: Dict[str, Dict[str, Any]], reviews, location: Optional[str]) -> None:
        for review in reviews:
            if _belongs_to(review, location):
                self._merge_review(seen, review)

    def list_reviews(self, location: str) -> List[Dict[str, Any]]:
        """Walk every reviews page for ``location``, de-duplicated by name.

        Returns the reviews in first-seen order; when the same review ``name``
        appears more than once, the snapshot with the newer ``updateTime`` wins.
        Only reviews whose ``name`` belongs to ``location`` are returned (shop
        A/B isolation).
        """
        self._record("list_reviews", location=location)
        seen: Dict[str, Dict[str, Any]] = {}
        token: Optional[str] = FIRST_PAGE
        while True:
            page = self.fetch_page(location, token)
            self._collect(seen, page.get("reviews", []), location)
            token = page.get("nextPageToken")
            if not token:
                break
        return list(seen.values())

    def list_reviews_for(
        self,
        location: Optional[str] = None,
        page_files: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Serve a named fixture page set (e.g. ``["reviews_loc002_page1.json"]``).

        Pages are chained by their own ``nextPageToken`` (first file is page 1,
        the token of page *n* resolves to page *n+1*). Reviews are de-duplicated
        by ``name`` with the newer ``updateTime`` winning, and filtered to
        ``location`` when given. Passing ``page_files=None`` walks the default
        ``reviews_page1.json`` / ``reviews_page2.json`` pair. Raises nothing for
        an empty page set: it simply returns ``[]``.
        """
        self._record("list_reviews_for", location=location, page_files=list(page_files) if page_files else None)
        self._maybe_fail("list_reviews_for")

        if page_files is None:
            pages = list(self._pages)
        else:
            pages = [fixtures.load_fixture(f, self.fixtures_dir) for f in page_files]

        mapping: Dict[Optional[str], Dict[str, Any]] = {}
        token: Optional[str] = FIRST_PAGE
        for page in pages:
            mapping[token] = dict(page)
            token = page.get("nextPageToken")

        seen: Dict[str, Dict[str, Any]] = {}
        token = FIRST_PAGE
        while True:
            page = mapping.get(token)
            if page is None:
                break
            self._collect(seen, page.get("reviews", []), location)
            token = page.get("nextPageToken")
            if not token:
                break
        return list(seen.values())
