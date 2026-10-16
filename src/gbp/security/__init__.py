"""Security helpers: central secret handling + log redaction.

Group 8A / T24 -- one place to (a) load/validate the session signing secret
and (b) mask tokens, passwords, cookies, Authorization headers and CSRF
tokens before anything is printed or logged.

Standard library only. Nothing here writes a secret anywhere.
"""

from __future__ import annotations

from .redact import (
    REDACTED,
    SENSITIVE_KEYS,
    is_sensitive_key,
    redact,
    redact_headers,
    redact_text,
)
from .secrets import (
    MIN_SECRET_LENGTH,
    SessionSecret,
    SessionSecretError,
    WeakSessionSecretError,
    load_session_secret,
)

__all__ = [
    "REDACTED",
    "SENSITIVE_KEYS",
    "is_sensitive_key",
    "redact",
    "redact_headers",
    "redact_text",
    "MIN_SECRET_LENGTH",
    "SessionSecret",
    "SessionSecretError",
    "WeakSessionSecretError",
    "load_session_secret",
]
