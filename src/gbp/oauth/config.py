"""Live-mode configuration for the per-tenant GBP OAuth connect (Issue #110).

Credentials are read from the ENVIRONMENT only -- never from a file in the
repository, never hardcoded, never logged:

* ``GBP_OAUTH_CLIENT_ID`` / ``GBP_OAUTH_CLIENT_SECRET`` / ``GBP_OAUTH_REDIRECT_URI``
  -- the OAuth client (as mandated by the issue; the earlier single-connection
  flow used ``GBP_GOOGLE_*`` names, which are left alone);
* ``GBP_LIVE=1`` -- the master switch. Live behaviour happens ONLY when this is
  set AND all three credentials exist, so with ``GBP_LIVE`` unset every code
  path behaves exactly like today's offline/fixture build;
* ``GBP_TOKEN_ENCRYPTION_KEY`` -- the key material for the encrypted vault;
* ``GBP_TOKEN_VAULT_DIR`` -- where the encrypted per-tenant records live
  (defaults outside the repository).

Standard library only, no network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

__all__ = [
    "LIVE_ENV",
    "CLIENT_ID_ENV",
    "CLIENT_SECRET_ENV",
    "REDIRECT_ENV",
    "VAULT_DIR_ENV",
    "ENCRYPTION_KEY_ENV",
    "CIPHER_ENV",
    "LiveNotConfigured",
    "OAuthConfig",
    "load_config",
    "live_enabled",
    "require_config",
    "vault_dir",
    "encryption_key",
]

LIVE_ENV = "GBP_LIVE"
CLIENT_ID_ENV = "GBP_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV = "GBP_OAUTH_CLIENT_SECRET"
REDIRECT_ENV = "GBP_OAUTH_REDIRECT_URI"
VAULT_DIR_ENV = "GBP_TOKEN_VAULT_DIR"
ENCRYPTION_KEY_ENV = "GBP_TOKEN_ENCRYPTION_KEY"
#: ``stdlib`` (default) or ``fernet`` (used only when ``cryptography`` exists).
CIPHER_ENV = "GBP_VAULT_CIPHER"

_TRUTHY = {"1", "true", "yes", "on"}


class LiveNotConfigured(RuntimeError):
    """Raised when the live path is asked for but not fully configured.

    ``reason`` is a stable code and the message holds no secret value, so it is
    safe to surface to an operator.
    """

    def __init__(self, reason: str, remedy: str = "") -> None:
        super().__init__("gbp_live_not_configured: %s%s"
                         % (reason, (" -- " + remedy) if remedy else ""))
        self.reason = reason
        self.remedy = remedy


@dataclass(frozen=True)
class OAuthConfig:
    """The three OAuth client values, kept together so they can be injected."""

    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""

    @property
    def complete(self) -> bool:
        """True only when all three values are present."""
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def missing(self) -> Optional[str]:
        """Name the first missing value (``None`` when complete)."""
        for value, env in ((self.client_id, CLIENT_ID_ENV),
                           (self.client_secret, CLIENT_SECRET_ENV),
                           (self.redirect_uri, REDIRECT_ENV)):
            if not value:
                return env
        return None


def _env(environ: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return dict(os.environ) if environ is None else dict(environ)


def load_config(environ: Optional[Dict[str, Any]] = None) -> OAuthConfig:
    """Read the OAuth client values from the environment (whitespace trimmed)."""
    env = _env(environ)
    return OAuthConfig(
        client_id=str(env.get(CLIENT_ID_ENV) or "").strip(),
        client_secret=str(env.get(CLIENT_SECRET_ENV) or "").strip(),
        redirect_uri=str(env.get(REDIRECT_ENV) or "").strip(),
    )


def live_enabled(environ: Optional[Dict[str, Any]] = None) -> bool:
    """True only when ``GBP_LIVE`` is set AND the client is fully configured.

    Everything else (unset switch, partial credentials) is the offline/fixture
    path, which must stay byte-for-byte identical to today's behaviour.
    """
    env = _env(environ)
    if str(env.get(LIVE_ENV) or "").strip().lower() not in _TRUTHY:
        return False
    return load_config(env).complete


def require_config(environ: Optional[Dict[str, Any]] = None) -> OAuthConfig:
    """Return the config or raise :class:`LiveNotConfigured`, named in order."""
    env = _env(environ)
    if str(env.get(LIVE_ENV) or "").strip().lower() not in _TRUTHY:
        raise LiveNotConfigured(
            "live_mode_off",
            "set %s=1 on purpose for this run" % LIVE_ENV)
    config = load_config(env)
    missing = config.missing()
    if missing:
        raise LiveNotConfigured(
            "credential_missing",
            "set %s in the operator environment (never share it in chat)" % missing)
    return config


def vault_dir(environ: Optional[Dict[str, Any]] = None) -> str:
    """Where encrypted per-tenant records live; defaults OUTSIDE the repo."""
    env = _env(environ)
    configured = str(env.get(VAULT_DIR_ENV) or "").strip()
    if configured:
        return configured
    return os.path.join(os.path.expanduser("~"), ".gbp", "token_vault")


def encryption_key(environ: Optional[Dict[str, Any]] = None) -> str:
    """Return the vault key material, or raise when it is absent.

    The value itself is never returned to a caller for display and never
    logged; only the derived cipher is handed around.
    """
    env = _env(environ)
    key = str(env.get(ENCRYPTION_KEY_ENV) or "").strip()
    if not key:
        raise LiveNotConfigured(
            "encryption_key_missing",
            "set %s to a long random value (never commit it)" % ENCRYPTION_KEY_ENV)
    return key
