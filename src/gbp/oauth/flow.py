"""Per-tenant OAuth connect / callback orchestration (Issue #110, increment 1).

Wires the pieces together:

* :func:`start_connect` -- builds the consent URL for ONE business and stores
  the pending flow (state + nonce) in a :class:`~gbp.oauth.state.StateStore`;
* :func:`handle_callback` -- validates the callback STRICTLY (state AND nonce,
  fresh and single-use), exchanges the code over an injected transport and
  writes the token into the tenant's encrypted vault slot;
* :func:`connection_rows` -- the dashboard projection: per-business connection
  state plus the last-sync timestamp, never a token;
* :func:`disconnect` -- forget one tenant's token.

Everything is fail-closed: without ``GBP_LIVE=1`` and the three credentials,
``start_connect``/``handle_callback`` refuse instead of pretending. No token,
code, secret or verifier is ever returned, logged or put in an exception
message.

Standard library only; the only network hop is the injected transport.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from gbp.auth.oauth import SCOPES, build_authorization_url, guard_transport
from gbp.auth.token_manager import Token
from gbp.oauth.config import OAuthConfig, require_config
from gbp.oauth.state import StateStore
from gbp.oauth.tenancy import normalize_tenant
from gbp.oauth.vault import (
    STATE_CONNECTED,
    STATE_NOT_CONNECTED,
    STATE_TOKEN_EXPIRED,
    TOKEN_URL,
    TokenVault,
)

__all__ = [
    "OAuthExchangeError",
    "start_connect",
    "handle_callback",
    "connection_rows",
    "disconnect",
    "STATE_LABELS",
]


class OAuthExchangeError(RuntimeError):
    """The authorization-code exchange failed (reason only, never the body)."""

    def __init__(self, reason: str) -> None:
        super().__init__("oauth_exchange_error: %s" % reason)
        self.reason = reason


#: Human labels for the three connection states the dashboard renders.
STATE_LABELS: Dict[str, str] = {
    STATE_NOT_CONNECTED: "Not connected",
    STATE_CONNECTED: "Connected",
    STATE_TOKEN_EXPIRED: "Token expired or revoked",
}


def _exchange(transport: Any, config: OAuthConfig, *, code: str, redirect_uri: str,
              code_verifier: Optional[str] = None, now: float,
              timeout: float = 15.0) -> Token:
    """POST the authorization code to Google's token endpoint, offline-driven.

    ``guard_transport`` refuses a live transport unless the operator opted in,
    so a test's ``MockTransport`` is the only way this runs by default.
    """
    guard_transport(transport)
    body: Dict[str, str] = {
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "code": str(code),
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        body["code_verifier"] = str(code_verifier)
    response = transport.post(TOKEN_URL, data=body, timeout=timeout)
    payload = response.json() if hasattr(response, "json") else (response or {})
    status = int(getattr(response, "status", 200))
    if not isinstance(payload, dict) or status >= 400:
        raise OAuthExchangeError("token_endpoint_rejected")
    access_token = payload.get("access_token")
    if not access_token:
        raise OAuthExchangeError("no_access_token_in_response")
    try:
        expires_in = float(payload.get("expires_in") or 0.0)
    except (TypeError, ValueError):
        expires_in = 0.0
    scope_text = payload.get("scope") or ""
    return Token(
        access_token=str(access_token),
        refresh_token=payload.get("refresh_token"),
        expires_at=now + expires_in,
        scopes=scope_text.split() if scope_text else list(SCOPES),
        token_type=payload.get("token_type") or "Bearer",
    )


def start_connect(tenant: Any, *, store: StateStore, environ: Optional[Dict[str, Any]] = None,
                  scopes: Optional[List[str]] = None,
                  state: Optional[str] = None, nonce: Optional[str] = None,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """Start a per-tenant connect flow and return the consent URL + the values.

    ``state``/``nonce`` are returned so the caller can keep the nonce on the
    tenant's session (it must come back on the callback); they are not secrets
    in the OAuth sense, but they ARE single-use and are burned on the callback.
    """
    config = require_config(environ)
    tid = normalize_tenant(tenant)
    scope_list = list(scopes or SCOPES)
    record = store.start(tid, redirect_uri=config.redirect_uri, scopes=scope_list,
                         state=state, nonce=nonce)
    url = build_authorization_url(
        client_id=config.client_id, redirect_uri=config.redirect_uri,
        state=record["state"], scopes=scope_list)
    return {
        "tenant_id": tid,
        "authorization_url": url,
        "state": record["state"],
        "nonce": record["nonce"],
        "scopes": scope_list,
        "expires_in": store.ttl_seconds,
    }


def handle_callback(code: str, state: str, nonce: str, *, tenant: Any,
                    store: StateStore, vault: TokenVault, transport: Any,
                    environ: Optional[Dict[str, Any]] = None,
                    exchanger: Optional[Callable[..., Token]] = None,
                    now: Optional[float] = None) -> Dict[str, Any]:
    """Validate a callback and persist the tenant's token. Returns safe metadata.

    The flow is burned before the exchange (see :meth:`StateStore.consume`), so
    a replayed or concurrent callback cannot mint a second token.
    """
    config = require_config(environ)
    tid = normalize_tenant(tenant)
    # Raises FlowStateError on mismatch/expiry/replay; burns the flow on success.
    store.consume(tid, state, nonce)
    stamp = now if now is not None else time.time()
    exchange = exchanger or _exchange
    try:
        if not code:
            raise OAuthExchangeError("missing_code")
        try:
            token = exchange(
                transport, config, code=code,
                redirect_uri=config.redirect_uri, now=stamp)
        except OAuthExchangeError:
            raise
        except Exception:  # noqa: BLE001 - provider failure, reported safely
            raise OAuthExchangeError("exchange_failed") from None
    finally:
        store.release(tid)
    vault.save(tid, token)
    return {
        "tenant_id": tid,
        "connected": True,
        "connection_state": vault.connection_state(tid),
        "scopes": list(token.scopes),
        "has_refresh_token": bool(token.refresh_token),
        "token_redacted": True,
    }


def connection_rows(businesses: List[Dict[str, Any]], vault: TokenVault, *,
                    now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Per-business dashboard rows: connection state + last sync, no token.

    ``businesses`` are the dashboard rows (``location_name`` is the tenant id,
    matching :data:`gbp.oauth.tenancy`'s exact-match rule). Every row gets a
    ``connection_state`` and a human ``connection_label``; the sync timestamp
    comes from the vault record when the live sync has run, else from the row.
    """
    out: List[Dict[str, Any]] = []
    for row in businesses or ():
        if not isinstance(row, dict):
            continue
        tenant = row.get("location_name") or row.get("tenant_id") or row.get("location_id")
        entry = dict(row)
        try:
            state = vault.connection_state(tenant, now=now)
            meta = vault.meta(tenant)
        except Exception:  # noqa: BLE001 - a bad record must not break the page
            state, meta = STATE_TOKEN_EXPIRED, None
        entry["connection_state"] = state
        entry["connection_label"] = STATE_LABELS.get(state, STATE_LABELS[STATE_NOT_CONNECTED])
        entry["google_last_sync_at"] = (meta or {}).get("last_sync_at")
        entry["last_sync_at"] = (meta or {}).get("last_sync_at") or row.get("last_sync_at")
        entry["google_sync_status"] = (meta or {}).get("sync_status")
        # Issue #117: the recorded failure reason (exception class / API error
        # code only -- the vault never stores anything else here) so a failed
        # sync is visible on the Google Connection row instead of a silent no-op.
        entry["google_last_error"] = (meta or {}).get("last_error")
        out.append(entry)
    return out


def disconnect(tenant: Any, vault: TokenVault) -> Dict[str, Any]:
    """Remove one tenant's stored token; never prints or returns it."""
    tid = normalize_tenant(tenant)
    removed = vault.delete(tid)
    return {
        "tenant_id": tid,
        "disconnected": removed,
        "reason": "token_removed" if removed else "no_token_stored",
        "connection_state": vault.connection_state(tid),
    }
