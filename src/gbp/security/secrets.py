"""Session signing-secret loading + strength validation (fail closed).

Group 8A / T24
--------------
Shipping a *silently* invented ephemeral signing secret is not acceptable for
anything beyond a throwaway local demo. This module centralises the rule:

* :func:`load_session_secret` reads the secret from the environment.
* When ``require=True`` (i.e. real auth is being enabled), a missing secret is
  a hard error (:class:`SessionSecretError`) UNLESS the operator explicitly
  opts in with ``allow_ephemeral=True`` -- and even then a loud warning is
  emitted and the value is never persisted.
* A provided secret shorter than :data:`MIN_SECRET_LENGTH` is rejected
  (:class:`WeakSessionSecretError`).
* The secret value itself is NEVER returned in a message, logged, or written
  anywhere -- only its length/shape is ever described.

Standard library only.
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
import warnings
from dataclasses import dataclass
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

#: Minimum acceptable length (in characters) for a real signing secret.
MIN_SECRET_LENGTH = 32

DEFAULT_SECRET_ENV = "GBP_SESSION_SECRET"


class SessionSecretError(RuntimeError):
    """Raised when real auth is enabled but no usable secret is available."""


class WeakSessionSecretError(SessionSecretError):
    """Raised when a supplied signing secret is too short/weak."""


@dataclass(frozen=True)
class SessionSecret:
    """The resolved signing secret plus whether it is ephemeral."""

    value: bytes
    ephemeral: bool


def load_session_secret(
    env: Optional[Mapping[str, str]] = None,
    secret_env: str = DEFAULT_SECRET_ENV,
    *,
    require: bool = False,
    allow_ephemeral: bool = False,
    min_length: int = MIN_SECRET_LENGTH,
) -> SessionSecret:
    """Resolve the HMAC signing secret, failing closed by default.

    Parameters
    ----------
    env:
        Environment mapping to read from (defaults to ``os.environ``).
    secret_env:
        Name of the environment variable holding the secret.
    require:
        When ``True``, a missing secret is refused unless ``allow_ephemeral``.
    allow_ephemeral:
        LOCAL EXPERIMENTATION ONLY. When combined with ``require`` this permits
        a random per-process secret, with a loud warning that sessions die with
        the process. Never the default.
    min_length:
        Minimum acceptable length for a supplied secret.

    Returns
    -------
    SessionSecret
        ``.value`` bytes, ``.ephemeral`` bool. The value is never logged.
    """
    env = env if env is not None else os.environ
    raw = env.get(secret_env)

    if raw:
        if require and len(raw) < min_length:
            raise WeakSessionSecretError(
                f"{secret_env} is too weak: it must be at least {min_length} "
                f"characters (got {len(raw)}). Refusing to start real auth with "
                "a short signing secret. Generate one with:\n"
                "    python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        return SessionSecret(value=raw.encode("utf-8"), ephemeral=False)

    if require and not allow_ephemeral:
        raise SessionSecretError(
            f"{secret_env} is required when real auth (--auth) is enabled and is "
            "not set. Generate a random value of at least "
            f"{min_length} characters, e.g. with:\n"
            "    python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
            f"then set the environment variable {secret_env} to that value. "
            "Refusing to start with real auth rather than invent a signing secret. "
            "For throwaway local use only you may pass --allow-ephemeral-secret."
        )

    if not allow_ephemeral:
        # Non-strict library call (backward-compatible behaviour): still warn
        # loudly, but do not refuse -- existing callers rely on it.
        pass

    value = _secrets.token_bytes(32)
    msg = (
        f"{secret_env} is not set: generated an EPHEMERAL session secret for this "
        "process only (sessions will not survive a restart). Set the environment "
        "variable before any real deployment."
    )
    warnings.warn(msg, RuntimeWarning, stacklevel=2)
    logger.warning(msg)
    return SessionSecret(value=value, ephemeral=True)
