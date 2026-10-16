"""OAuth 2.0 readiness for the Google Business Profile integration (Group 8B / T26).

Everything here can be exercised WITHOUT contacting Google:

* the documented scope set (:data:`SCOPES`),
* :func:`build_authorization_url` -- a pure string operation,
* :func:`exchange_code` -- the authorization-code exchange, which runs over an
  INJECTED transport (tests pass a ``MockTransport``; this module never opens a
  socket of its own),
* :func:`google_connection_status` -- reports ``not_connected`` unless a real
  access token is actually present on disk (and never returns the token),
* a hard opt-in gate (:func:`live_google_allowed`): a LIVE transport is refused
  unless ``GBP_ALLOW_LIVE_GOOGLE=1`` is set explicitly, so an accidental real
  call is impossible by default.

No credential is ever hardcoded here: the client id/secret are parameters or
environment values, and nothing is logged.

Standard library only.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any, Dict, List, Optional

from gbp.client.http_client import MockTransport
from gbp.auth.token_manager import Token

__all__ = [
    "SCOPES",
    "AUTHORIZE_URL",
    "TOKEN_URL",
    "LIVE_OPTIN_ENV",
    "LiveGoogleNotAllowedError",
    "live_google_allowed",
    "build_authorization_url",
    "exchange_code",
    "google_connection_status",
]

#: The scope the integration needs. ``business.manage`` is a RESTRICTED scope:
#: Google must approve the Cloud project before a real token will work.
SCOPES: tuple = ("https://www.googleapis.com/auth/business.manage",)

#: OAuth 2.0 endpoints (documented; only ever used over an injected transport).
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Set to ``1`` to allow a LIVE (socket) transport. Off by default.
LIVE_OPTIN_ENV = "GBP_ALLOW_LIVE_GOOGLE"


class LiveGoogleNotAllowedError(RuntimeError):
    """Raised when a live Google call is attempted without the explicit opt-in."""


def live_google_allowed() -> bool:
    """True only when ``GBP_ALLOW_LIVE_GOOGLE=1`` was set on purpose."""
    return str(os.environ.get(LIVE_OPTIN_ENV, "")).strip() in {"1", "true", "yes"}


def guard_transport(transport: Any) -> None:
    """Refuse a non-mock transport unless the operator opted in.

    A ``MockTransport`` is always allowed (it is offline by construction), so
    the whole flow stays testable while an accidental live call stays
    impossible.
    """
    if isinstance(transport, MockTransport) or live_google_allowed():
        return
    raise LiveGoogleNotAllowedError(
        "refusing to talk to Google: inject a MockTransport, or set "
        f"{LIVE_OPTIN_ENV}=1 explicitly for a real deployment"
    )


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    *,
    state: str,
    scopes: Optional[List[str]] = None,
    access_type: str = "offline",
    prompt: str = "consent",
    include_granted_scopes: bool = True,
) -> str:
    """Build the consent-screen URL.

    ``access_type=offline`` + ``prompt=consent`` are what make Google return a
    refresh token, which is required for unattended synchronisation.
    """
    if not client_id:
        raise ValueError("client_id is required")
    if not redirect_uri:
        raise ValueError("redirect_uri is required")
    if not state:
        raise ValueError("state is required (CSRF protection for the redirect)")
    params = {
        "client_id": str(client_id),
        "redirect_uri": str(redirect_uri),
        "response_type": "code",
        "scope": " ".join(scopes or SCOPES),
        "state": str(state),
        "access_type": str(access_type),
        "prompt": str(prompt),
        "include_granted_scopes": "true" if include_granted_scopes else "false",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(
    transport: Any,
    *,
    client_id: str,
    code: str,
    redirect_uri: str,
    client_secret: Optional[str] = None,
    code_verifier: Optional[str] = None,
    token_url: str = TOKEN_URL,
    timeout: float = 15.0,
) -> Token:
    """Exchange an authorization code for a token over the injected transport.

    Refuses a live transport unless the opt-in is set. The code, the secret and the
    PKCE verifier are put in the request body only -- never logged, never returned.

    ``code_verifier`` is the RFC 7636 value that pairs with the ``code_challenge`` sent
    in the authorization request. When it is omitted the request is the plain
    authorization-code flow, which a PKCE-protected authorization will reject.
    """
    if not client_id:
        raise ValueError("client_id is required")
    if not code:
        raise ValueError("code is required")
    guard_transport(transport)

    body: Dict[str, str] = {
        "client_id": str(client_id),
        "code": str(code),
        "grant_type": "authorization_code",
        "redirect_uri": str(redirect_uri),
    }
    if client_secret:
        body["client_secret"] = str(client_secret)
    if code_verifier:
        # RFC 7636 section 4.5: the verifier travels in the token request body.
        body["code_verifier"] = str(code_verifier)

    response = transport.post(token_url, data=body, timeout=timeout)
    status = getattr(response, "status", None)
    payload = response.json() if hasattr(response, "json") else response
    if status is not None and int(status) >= 400:
        raise RuntimeError(f"token exchange failed with HTTP {int(status)}")
    if not isinstance(payload, dict):
        raise RuntimeError("token exchange returned an unexpected payload")
    return Token.from_dict({
        "access_token": payload.get("access_token", ""),
        "refresh_token": payload.get("refresh_token"),
        "expires_at": payload.get("expires_at", 0.0),
        "scopes": (payload.get("scope") or "").split(),
        "token_type": payload.get("token_type", "Bearer"),
    })


def google_connection_status(token_path: Optional[str] = None) -> Dict[str, Any]:
    """Report whether a usable Google token exists. NEVER returns the token.

    ``connected`` is only true when a non-empty access token is actually
    present, so the UI can show "Google: not connected" honestly instead of
    implying a working integration.
    """
    path = token_path or os.environ.get("GBP_TOKEN_PATH") or os.path.expanduser(
        "~/.gbp/token.json")
    if not os.path.exists(path):
        return {"connected": False, "reason": "no_token", "token_path": path}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"connected": False, "reason": "unreadable_token",
                "token_path": path}
    token = Token.from_dict(data if isinstance(data, dict) else {})
    if not token.access_token:
        return {"connected": False, "reason": "empty_token", "token_path": path}
    return {
        "connected": True,
        "reason": "token_present",
        "token_path": path,
        "expires_at": token.expires_at,
        "scopes": list(token.scopes),
    }
