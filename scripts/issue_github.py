"""Minimal GitHub REST client shared by the issue worker.

One place for the things that are easy to get wrong:

* one authentication header, built once -- a token can never leak into a URL,
  a log line or an exception message;
* **full pagination** over ``Link: rel="next"`` for issues, events, comments;
* **fail-closed** error handling: rate limits, 5xx and malformed bodies all
  raise, and nothing is ever interpreted as "no work to do";
* a **pluggable transport**, so the entire pull-request flow can be exercised
  in tests with a recording fake and no network at all.

Only ``api.github.com`` is ever contacted. No other host appears in this module.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

API_ROOT = "https://api.github.com"
API_HOST = "api.github.com"
USER_AGENT = "steven-ai-os-issue-worker/1.0"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
TIMEOUT = 30
PER_PAGE = 100
#: hard stop so a pathological repo cannot paginate forever
MAX_PAGES = 20

#: transport(method, url, headers, body) -> (status, headers, body_bytes)
Transport = Callable[[str, str, Dict[str, str], Optional[bytes]],
                     Tuple[int, Dict[str, str], bytes]]


class GitHubError(RuntimeError):
    """An API call did not succeed. Callers must never treat this as empty."""

    def __init__(self, message: str, status: Optional[int] = None,
                 url: Optional[str] = None, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.detail = detail


class RateLimited(GitHubError):
    """GitHub refused the call because of a rate limit."""

    def __init__(self, message: str, reset_at: Optional[str] = None,
                 status: int = 403, url: Optional[str] = None) -> None:
        super().__init__(message, status=status, url=url)
        self.reset_at = reset_at


class TransportError(GitHubError):
    """The request never reached GitHub (DNS, TLS, timeout, ...)."""


# --------------------------------------------------------------------------
def build_headers(token: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "Accept": ACCEPT,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    if extra:
        headers.update(extra)
    return headers


def default_transport(method: str, url: str, headers: Dict[str, str],
                      body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
    if urllib.parse.urlsplit(url).hostname != API_HOST:
        raise GitHubError(f"refusing to call a non-GitHub host: {url}")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:               # 4xx/5xx are responses
        return exc.code, dict(exc.headers or {}), exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise TransportError(f"cannot reach GitHub: {exc}") from exc


def parse_link_next(headers: Dict[str, str]) -> Optional[str]:
    """Return the ``rel="next"`` URL from a Link header, if any."""
    for key, value in (headers or {}).items():
        if key.lower() != "link" or not value:
            continue
        for part in str(value).split(","):
            segments = part.split(";")
            if len(segments) < 2:
                continue
            url = segments[0].strip().strip("<>")
            if any('rel="next"' in s or "rel=next" in s for s in segments[1:]):
                return url
    return None


def _rate_limit_headers(headers: Dict[str, str]) -> Dict[str, Any]:
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    return {
        "remaining": lowered.get("x-ratelimit-remaining"),
        "reset": lowered.get("x-ratelimit-reset"),
        "retry_after": lowered.get("retry-after"),
    }


def _raise_for_status(status: int, headers: Dict[str, str], raw: bytes,
                      url: str) -> None:
    limits = _rate_limit_headers(headers)
    detail = raw[:400].decode("utf-8", "replace") if raw else ""
    if status in (403, 429):
        remaining = str(limits.get("remaining") or "")
        message = detail.lower()
        if remaining == "0" or "rate limit" in message or status == 429:
            reset = str(limits.get("reset") or limits.get("retry_after") or "")
            raise RateLimited(
                f"GitHub rate limit reached (status {status}); "
                f"reset={reset or 'unknown'}", reset_at=reset or None,
                status=status, url=url)
    raise GitHubError(f"GitHub API error {status} for {url}", status=status,
                      url=url, detail=detail)


def request(method: str, path: str, token: str, *,
            transport: Optional[Transport] = None,
            body: Optional[Dict[str, Any]] = None,
            params: Optional[Dict[str, Any]] = None,
            expect: Sequence[int] = (200, 201, 204),
            timeout_unused: Optional[float] = None) -> Tuple[Any, Dict[str, str]]:
    """One API call. Any unexpected outcome raises -- never a silent empty."""
    send = transport or default_transport
    url = path if path.startswith("http") else API_ROOT + path
    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}{'&' if '?' in url else '?'}{query}"
    payload = None
    headers = build_headers(token)
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    status, response_headers, raw = send(method, url, headers, payload)
    if status not in tuple(expect):
        _raise_for_status(int(status), response_headers, raw, url)
    if status == 204 or not raw:
        return None, response_headers
    try:
        return json.loads(raw.decode("utf-8")), response_headers
    except (ValueError, UnicodeDecodeError) as exc:
        raise GitHubError(f"GitHub returned a malformed body for {url}",
                          status=status, url=url, detail=str(exc)) from exc


def paginate(path: str, token: str, *, transport: Optional[Transport] = None,
             params: Optional[Dict[str, Any]] = None,
             max_pages: int = MAX_PAGES,
             per_page: int = PER_PAGE) -> List[Any]:
    """Every item of a paginated collection, following ``Link`` headers.

    Stops at ``max_pages``; if a page is full but the response carries no Link
    header (some fake transports), the page counter is advanced instead, so a
    truncated page can never be mistaken for the end of the collection.
    """
    items: List[Any] = []
    query = dict(params or {})
    query.setdefault("per_page", per_page)
    query.setdefault("page", 1)
    url: Optional[str] = path
    current: Optional[Dict[str, Any]] = query
    pages = 0
    while url and pages < max_pages:
        payload, headers = request("GET", url, token, transport=transport,
                                   params=current or None)
        current = None                  # a Link URL carries its own query string
        if isinstance(payload, list):
            items.extend(payload)
            size = len(payload)
        else:                                   # single-object endpoints
            items.append(payload)
            size = 0
        pages += 1
        nxt = parse_link_next(headers)
        if nxt:
            url = nxt
        elif size >= per_page:
            # No Link header but the page was full: keep going by page number so
            # a truncated collection can never be mistaken for the whole one.
            current = {"per_page": per_page,
                       "page": int(query.get("page", 1)) + pages}
            url = path
        else:
            url = None
    return items


# --------------------------------------------------------------------------
# High level helpers used by the worker (all transport-injectable)
# --------------------------------------------------------------------------
def list_issues(repo: str, token: str, label: str, *,
                transport: Optional[Transport] = None,
                states: str = "open") -> List[Dict[str, Any]]:
    """Open issues carrying ``label``; pull requests are filtered out."""
    items = paginate(f"/repos/{repo}/issues", token, transport=transport,
                     params={"state": states, "labels": label})
    return [item for item in items if isinstance(item, dict)
            and "pull_request" not in item]


def list_issue_events(repo: str, number: Any, token: str, *,
                      transport: Optional[Transport] = None) -> List[Dict[str, Any]]:
    """The full event timeline of one issue (paginated)."""
    items = paginate(f"/repos/{repo}/issues/{number}/events", token,
                     transport=transport)
    return [item for item in items if isinstance(item, dict)]
