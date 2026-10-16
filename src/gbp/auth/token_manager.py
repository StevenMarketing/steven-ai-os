"""OAuth2 token storage and refresh for the GBP integration.

Standard library only, no network. The manager persists a single token as a
JSON file created with mode ``0o600`` and never logs token values -- both the
:class:`Token` representation and the JSON dump are safe to print.

Refresh is a *single* attempt over an injected transport (``transport.post``),
so it can be driven entirely by a fake in tests. No real Google endpoint is
ever contacted from this module.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from gbp.client.mock_client import AuthError

__all__ = ["Token", "TokenManager"]

DEFAULT_REFRESH_URL = "https://oauth2.googleapis.com/token"


class Token:
    """An OAuth2 token.

    The ``__repr__`` is deliberately redacted: token values must never reach a
    log line, a traceback or a test failure message.
    """

    __slots__ = ("access_token", "refresh_token", "expires_at", "scopes", "token_type")

    def __init__(
        self,
        access_token: str = "",
        refresh_token: Optional[str] = None,
        expires_at: float = 0.0,
        scopes: Optional[List[str]] = None,
        token_type: str = "Bearer",
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        #: Absolute expiry as a UNIX epoch (seconds). 0.0 means "already expired".
        self.expires_at = float(expires_at or 0.0)
        self.scopes = list(scopes or [])
        self.token_type = token_type

    # -- expiry ----------------------------------------------------------- #
    def is_expired(self, now: float, skew_seconds: float = 60) -> bool:
        """True when the token expires within ``skew_seconds`` of ``now``."""
        if not self.access_token:
            return True
        return now >= (self.expires_at - float(skew_seconds))

    # -- serialisation ---------------------------------------------------- #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Token":
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token"),
            expires_at=data.get("expires_at", 0.0),
            scopes=data.get("scopes") or [],
            token_type=data.get("token_type", "Bearer"),
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial, but redacted
        return (
            "Token(access_token='***', refresh_token="
            f"{'***' if self.refresh_token else None}, "
            f"expires_at={self.expires_at}, scopes={self.scopes!r})"
        )


def _payload(response: Any) -> Dict[str, Any]:
    """Best-effort extraction of a JSON body from a transport response."""
    if response is None:
        return {}
    if hasattr(response, "json"):
        try:
            data = response.json()
        except Exception:  # pragma: no cover - defensive
            return {}
        return data if isinstance(data, dict) else {}
    return response if isinstance(response, dict) else {}


def _status(response: Any) -> Optional[int]:
    status = getattr(response, "status", None)
    if status is None and isinstance(response, dict):
        status = response.get("status")
    return int(status) if status is not None else None


class TokenManager:
    """Load/save/refresh a single OAuth2 token.

    Parameters
    ----------
    token_path:
        Where the token JSON lives (outside the repo, mode ``600``).
    refresh_url:
        Token endpoint used by :meth:`refresh`.
    client_id / client_secret:
        Sent with the refresh exchange when configured.
    clock:
        Injectable ``() -> float`` epoch source (defaults to :func:`time.time`).
    """

    def __init__(
        self,
        token_path: str,
        refresh_url: str = DEFAULT_REFRESH_URL,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.token_path = str(token_path)
        self.refresh_url = refresh_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._clock: Callable[[], float] = clock or time.time
        self.token: Optional[Token] = None

    # -- persistence ------------------------------------------------------ #
    def load(self) -> Optional[Token]:
        """Load the token from disk; returns ``None`` when the file is absent."""
        path = Path(self.token_path)
        if not path.exists():
            self.token = None
            return None
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.token = Token.from_dict(data if isinstance(data, dict) else {})
        return self.token

    def save(self, path: Optional[str] = None) -> None:
        """Write the token as JSON, creating the file with mode ``0o600``."""
        target = Path(path or self.token_path)
        if target.parent and str(target.parent):
            target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.token.to_dict() if self.token is not None else {}
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        # Defensive: an existing file keeps its old mode otherwise.
        try:
            os.chmod(str(target), 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass

    # -- expiry ----------------------------------------------------------- #
    def is_expired(self, skew_seconds: float = 60) -> bool:
        if self.token is None:
            return True
        return self.token.is_expired(self._clock(), skew_seconds)

    # -- refresh ---------------------------------------------------------- #
    def refresh(self, transport: Any) -> Token:
        """Exchange the refresh token for a new access token -- single attempt.

        Raises :class:`AuthError` on any failure (non-2xx status, missing
        access token, or a transport-level exception).
        """
        token = self.token if self.token is not None else Token()
        if not token.refresh_token:
            raise AuthError("cannot refresh: no refresh_token stored")
        data: Dict[str, Any] = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        }
        if self.client_id:
            data["client_id"] = self.client_id
        if self.client_secret:
            data["client_secret"] = self.client_secret
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            response = transport.post(self.refresh_url, data=data, headers=headers)
        except AuthError:
            raise
        except Exception as exc:  # transport failure -> typed auth error
            raise AuthError(f"token refresh transport failed: {exc}") from exc

        payload = _payload(response)
        status = _status(response)
        if status is not None and not (200 <= status < 300):
            message = payload.get("error_description") or payload.get("error") or "token refresh rejected"
            raise AuthError(str(message), code=status)

        access_token = payload.get("access_token")
        if not access_token:
            raise AuthError("token refresh response contained no access_token")

        expires_in = payload.get("expires_in")
        try:
            expires_in = float(expires_in)
        except (TypeError, ValueError):
            expires_in = 0.0
        refreshed = Token(
            access_token=str(access_token),
            refresh_token=payload.get("refresh_token") or token.refresh_token,
            expires_at=self._clock() + expires_in,
            scopes=payload.get("scopes") or token.scopes,
            token_type=payload.get("token_type") or token.token_type,
        )
        self.token = refreshed
        return refreshed

    def ensure_valid(self, transport: Any) -> Token:
        """Return a usable token, refreshing it first when expired."""
        if self.is_expired() or self.token is None:
            return self.refresh(transport)
        return self.token
