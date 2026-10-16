"""Central redaction helper -- mask secrets before they reach any log/print.

Group 8A / T24 (+ T24a fix: bounded cookie masking)
---------------------------------------------------
Every log/print path that could carry request headers, cookies, bodies or user
records must route through :func:`redact` / :func:`redact_text` /
:func:`redact_headers` so that tokens, passwords, session cookies, Authorization
headers and CSRF tokens are replaced with ``[REDACTED]`` before they are emitted.

This is a *masking* helper: it is safe to print its output. It never returns the
original secret material in the masked result.

T24a fix: the Cookie/Set-Cookie scrubber previously captured ``[^\\r\\n]+``,
i.e. the whole rest of the line. Any legitimate text after a cookie value was
destroyed (``'prefix cookie=abc123 suffix KEEPME'`` -> ``'prefix cookie=[REDACTED]'``),
which also mangled the fail-closed help message that mentions
``GBP_SESSION_SECRET=...``. Masking is now bounded to the token/pair being
masked: names, attributes and trailing prose survive.

Standard library only.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

#: Placeholder used in place of any masked value.
REDACTED = "[REDACTED]"

#: Mapping keys whose value is always masked, regardless of content.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "pass",
        "secret",
        "client_secret",
        "session_secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "api_key",
        "apikey",
        "x-api-key",
        "session",
        "session_id",
        "session_token",
        "sessionid",
        "sid",
        "cookie",
        "set_cookie",
        "authorization",
        "proxy_authorization",
        "csrf",
        "csrf_token",
        "xsrf",
        "xsrf_token",
        "x_csrf_token",
        "x_api_key",
        "private_key",
    }
)


def _norm_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def is_sensitive_key(key: Any) -> bool:
    """Return True when ``key`` names a value that must be masked."""
    return _norm_key(key) in SENSITIVE_KEYS


# --- bounded Cookie / Set-Cookie masking (T24a) ------------------------------

#: A Cookie / Set-Cookie header marker plus the start of its value section.
_COOKIE_HEADER = re.compile(r"(?i)(\b(?:set-)?cookie\s*[:=]\s*)(\S[^\r\n]*)")

#: Splits a cookie value section on its separators (``;`` and whitespace) while
#: keeping the separators, so only the tokens themselves are ever replaced.
_COOKIE_PART_SPLIT = re.compile(r"([;\s]+)")


def _mask_cookie_tail(raw: str) -> str:
    """Mask cookie VALUES only, keeping names, attributes and trailing prose.

    ``name=value`` pairs have only the value masked (``name=[REDACTED]``); a bare
    value directly after the header marker is masked as a whole. Attribute words
    (``HttpOnly``, ``SameSite=Lax``) and any unrelated trailing text are kept.
    """
    parts = _COOKIE_PART_SPLIT.split(raw)
    seen_value = False
    out: list[str] = []
    for part in parts:
        if not part or _COOKIE_PART_SPLIT.fullmatch(part):
            out.append(part)
            continue
        if "=" in part:
            name, _, _value = part.partition("=")
            out.append(f"{name}={REDACTED}")
            seen_value = True
        elif not seen_value:
            out.append(REDACTED)
            seen_value = True
        else:
            out.append(part)
    return "".join(out)


def _mask_cookie_header(match: re.Match[str]) -> str:
    return match.group(1) + _mask_cookie_tail(match.group(2))


# Ordered (pattern, replacement) scrubbers for free-form text.
# Every value group is bounded to the token it masks: nothing after a masked
# value may be consumed.
_TEXT_SCRUBBERS: tuple[tuple[re.Pattern[str], Any], ...] = (
    # JSON/quoted key: "password": "..." / 'csrf_token': '...'
    (
        re.compile(
            r"(?i)([\"']?(?:password|passwd|pwd|secret|client_secret|"
            r"csrf[-_]?token|xsrf[-_]?token|csrf|xsrf|api[-_]?key|access[-_]?token|"
            r"refresh[-_]?token|id[-_]?token|auth[-_]?token|token)[\"']?\s*[:=]\s*)"
            r"([\"'][^\"']*[\"']|[^\s,;&}]+)"
        ),
        r"\1" + REDACTED,
    ),
    # Authorization header (any scheme, with or without "Bearer").
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+"),
        r"\1" + REDACTED,
    ),
    # Bare "Bearer <token>" anywhere.
    (
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]+=*"),
        "Bearer " + REDACTED,
    ),
    # Cookie / Set-Cookie headers -- bounded: masks values, keeps the rest.
    (_COOKIE_HEADER, _mask_cookie_header),
    # A session cookie embedded in a cookie header string.
    (
        re.compile(
            r"(?i)\b(gbp_session|sessionid|session|sid|jsessionid|phpsessid)=([^;\s\"',]+)"
        ),
        r"\1=" + REDACTED,
    ),
    # Provider-style API tokens (OpenRouter sk-or-v1..., generic sk-...).
    (re.compile(r"\bsk-or-v1[A-Za-z0-9\-_]+\b"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{6,}\b"), REDACTED),
)


def redact_text(text: str) -> str:
    """Return ``text`` with any recognisable secret material masked.

    Non-sensitive text before and after every masked value is preserved.
    """
    if not isinstance(text, str):
        return text
    out = text
    for pattern, repl in _TEXT_SCRUBBERS:
        out = pattern.sub(repl, out)
    return out


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return redact(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact(value: Any) -> Any:
    """Recursively mask sensitive keys/values in a mapping, list or string.

    A mapping key listed in :data:`SENSITIVE_KEYS` has its whole value replaced
    with :data:`REDACTED`; any remaining string is passed through
    :func:`redact_text`.
    """
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, val in value.items():
            if is_sensitive_key(key):
                out[key] = REDACTED
            else:
                out[key] = _redact_value(val)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_headers(headers: Any) -> dict[str, Any]:
    """Mask an HTTP header mapping/iterable of pairs for safe printing."""
    items: Iterable[Any]
    if isinstance(headers, Mapping):
        items = headers.items()
    else:
        items = headers
    out: dict[str, Any] = {}
    for pair in items:
        try:
            key, val = pair
        except (TypeError, ValueError):
            continue
        out[key] = REDACTED if is_sensitive_key(key) else redact_text(str(val))
    return out
