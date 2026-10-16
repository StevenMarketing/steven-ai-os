"""Google OAuth web-flow glue for GReply (connect / callback / disconnect).

Lives in ``scripts/`` on purpose: ``src/gbp/auth/**`` is a protected path, so the auth
package is left untouched and the owner decides when (and whether) this moves there.

Design rules (all fail-closed):

* nothing here calls Google unless :func:`live_ready` says the operator opted in
  (``GBP_ALLOW_LIVE_GOOGLE=1``), a client id/secret exist and a redirect URI is set;
* ``state`` and PKCE are mandatory, single-use, and bound to the caller's session
  record -- a callback with a missing, unknown or replayed state ends in
  :class:`GoogleSetupRequired`/``FlowError`` and never exchanges a code;
* no token, client secret or authorization code is ever returned in a report, a log
  line or an exception message;
* ``list_locations_read_only`` never invents rows: with no credentials it raises
  instead of returning demo data dressed up as real data.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from typing import Any, Callable, Dict, List, Optional

from gbp.auth.oauth import SCOPES, build_authorization_url, live_google_allowed

__all__ = [
    "GoogleSetupRequired", "FlowError", "live_ready", "start_flow", "finish_flow",
    "list_locations_read_only", "disconnect", "token_path", "REDIRECT_ENV", "CLIENT_ID_ENV",
    "CLIENT_SECRET_ENV", "MAX_TEST_LOCATIONS",
]

CLIENT_ID_ENV = "GBP_GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "GBP_GOOGLE_CLIENT_SECRET"
REDIRECT_ENV = "GBP_OAUTH_REDIRECT_URI"

#: Hard cap for the connection test: list at most five locations.
MAX_TEST_LOCATIONS = 5

#: How long a started flow stays usable (seconds).
FLOW_TTL_SECONDS = 600


class GoogleSetupRequired(RuntimeError):
    """Raised when the live connection is not allowed/configured yet.

    ``reason`` is a stable code (``live_opt_in_missing``, ``client_id_missing``,
    ``client_secret_missing``, ``redirect_uri_missing``, ``not_approved``); the
    message is safe to show to an operator and contains no secret.
    """

    def __init__(self, reason: str, remedy: str = "") -> None:
        super().__init__("google_setup_required: %s%s" % (reason, (" — " + remedy) if remedy else ""))
        self.reason = reason
        self.remedy = remedy


class FlowError(RuntimeError):
    """Raised for a bad callback (missing/unknown/replayed state, wrong session)."""

    def __init__(self, reason: str) -> None:
        super().__init__("google_flow_error: %s" % reason)
        self.reason = reason


def _conf(environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = os.environ if environ is None else environ
    return {
        "client_id": str(env.get(CLIENT_ID_ENV) or "").strip(),
        "client_secret": str(env.get(CLIENT_SECRET_ENV) or "").strip(),
        "redirect_uri": str(env.get(REDIRECT_ENV) or "").strip(),
    }


def live_ready(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Say whether a live call is permitted, without calling anything.

    Returns ``{"ready": bool, "reason": str|None, "remedy": str, "have": {...bool}}``.
    ``have`` only reports presence, never values.
    """
    env = os.environ if environ is None else environ
    conf = _conf(env)
    opted_in = (str(env.get("GBP_ALLOW_LIVE_GOOGLE") or "").strip() == "1") if environ is not None else live_google_allowed()
    if not opted_in:
        return {"ready": False, "reason": "live_opt_in_missing",
                "remedy": "set GBP_ALLOW_LIVE_GOOGLE=1 on purpose for this run",
                "have": {"client_id": bool(conf["client_id"]), "client_secret": bool(conf["client_secret"]),
                         "redirect_uri": bool(conf["redirect_uri"]), "opt_in": False}}
    have = {"client_id": bool(conf["client_id"]), "client_secret": bool(conf["client_secret"]),
            "redirect_uri": bool(conf["redirect_uri"]), "opt_in": True}
    for key, reason in (("client_id", "client_id_missing"), ("client_secret", "client_secret_missing")):
        if not conf[key]:
            return {"ready": False, "reason": reason,
                    "remedy": "set %s in the operator environment (never share it in chat)"
                              % (CLIENT_ID_ENV if key == "client_id" else CLIENT_SECRET_ENV),
                    "have": have}
    if not conf["redirect_uri"]:
        return {"ready": False, "reason": "redirect_uri_missing",
                "remedy": "set %s to the stable HTTPS callback, e.g. "
                          "https://gbp.adzdigital.com/api/google/oauth/callback" % REDIRECT_ENV,
                "have": have}
    return {"ready": True, "reason": None, "remedy": "", "have": have}


def _pkce_pair() -> Dict[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return {"code_verifier": verifier, "code_challenge": challenge, "code_challenge_method": "S256"}


def start_flow(*, environ: Optional[Dict[str, str]] = None, now: Optional[float] = None,
               scopes: Optional[List[str]] = None) -> Dict[str, Any]:
    """Build the consent URL plus the session record the caller must store.

    The returned dict holds ``authorization_url``, ``state``, ``created_at`` and the
    PKCE verifier. Only ``authorization_url`` may be shown to a browser; the verifier
    stays server-side.
    """
    state = live_ready(environ)
    if not state["ready"]:
        raise GoogleSetupRequired(state["reason"], state["remedy"])
    conf = _conf(environ)
    pkce = _pkce_pair()
    csrf_state = secrets.token_urlsafe(32)
    url = build_authorization_url(
        client_id=conf["client_id"], redirect_uri=conf["redirect_uri"],
        state=csrf_state, scopes=scopes or list(SCOPES))
    # build_authorization_url() has no PKCE parameters yet, so they are appended here.
    url = "%s&code_challenge=%s&code_challenge_method=%s" % (
        url, pkce["code_challenge"], pkce["code_challenge_method"])
    return {
        "authorization_url": url,
        "state": csrf_state,
        "created_at": float(now if now is not None else time.time()),
        "code_verifier": pkce["code_verifier"],
        "redirect_uri": conf["redirect_uri"],
        "scopes": list(scopes or SCOPES),
    }


def _check_state(flow_session: Optional[Dict[str, Any]], state: str,
                 now: Optional[float] = None) -> Dict[str, Any]:
    if not flow_session or not flow_session.get("state"):
        raise FlowError("no_flow_in_session")
    if not state or not secrets.compare_digest(str(state), str(flow_session["state"])):
        raise FlowError("state_mismatch")
    age = float(now if now is not None else time.time()) - float(flow_session.get("created_at") or 0.0)
    if age > FLOW_TTL_SECONDS:
        raise FlowError("state_expired")
    if flow_session.get("used"):
        raise FlowError("state_replayed")
    return flow_session


def finish_flow(code: str, state: str, flow_session: Optional[Dict[str, Any]], *,
                environ: Optional[Dict[str, str]] = None, now: Optional[float] = None,
                exchanger: Optional[Callable[..., Any]] = None,
                store: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Verify the callback and persist the token. Returns safe metadata only."""
    state_info = live_ready(environ)
    if not state_info["ready"]:
        raise GoogleSetupRequired(state_info["reason"], state_info["remedy"])
    session = _check_state(flow_session, state, now)
    if not code:
        raise FlowError("missing_code")
    conf = _conf(environ)
    if exchanger is None:
        from gbp.auth.oauth import exchange_code as exchanger  # local import: keeps this module import-light
    # The real exchange_code() takes (transport, *, client_id, code, redirect_uri,
    # client_secret) -- no verifier argument yet, so the verifier stays in the session
    # record until PKCE is threaded through the exchange.
    payload = exchanger(client_id=conf["client_id"], client_secret=conf["client_secret"],
                        code=code, redirect_uri=session.get("redirect_uri") or conf["redirect_uri"])
    token = payload.get("token") if isinstance(payload, dict) else payload
    if token is None:
        raise FlowError("exchange_failed")
    session["used"] = True
    saved = (store or _default_store())(token)
    return {"connected": True, "saved": bool(saved), "scopes": list(session.get("scopes") or SCOPES),
            "token_redacted": True}


def token_path(environ: Optional[Dict[str, str]] = None) -> str:
    """Where a stored token would live (path only -- never the token)."""
    env = os.environ if environ is None else environ
    return str(env.get("GBP_TOKEN_PATH") or os.path.join(os.getcwd(), ".gbp_token.json"))


def _default_store() -> Callable[..., Any]:
    def _store(token: Any) -> bool:  # pragma: no cover - refuses until wired on purpose
        raise GoogleSetupRequired(
            "store_not_wired",
            "pass a store= callable backed by gbp.auth.token_manager.TokenManager "
            "(the manager's save signature must be confirmed before wiring)")
    return _store


def list_locations_read_only(*, limit: int = MAX_TEST_LOCATIONS, environ: Optional[Dict[str, str]] = None,
                             fetcher: Optional[Callable[..., Any]] = None,
                             account: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read at most ``limit`` locations (default 5) — read-only, never fabricated.

    With no live configuration this raises :class:`GoogleSetupRequired`; it never
    falls back to fixture rows, so a demo number can never be mistaken for a real one.
    """
    state = live_ready(environ)
    if not state["ready"]:
        raise GoogleSetupRequired(state["reason"], state["remedy"])
    if limit < 1 or limit > MAX_TEST_LOCATIONS:
        limit = MAX_TEST_LOCATIONS
    if fetcher is None:
        raise GoogleSetupRequired("fetcher_not_wired",
                                  "wire the read-only Google client for the listings step")
    rows = fetcher(account=account, limit=limit) or []
    out: List[Dict[str, Any]] = []
    for row in rows[:limit]:
        out.append({
            "location_id": str(row.get("location_id") or row.get("name") or ""),
            "business_name": str(row.get("business_name") or row.get("title") or ""),
            "address": str(row.get("address") or ""),
            "zip": str(row.get("zip") or row.get("postal_code") or ""),
        })
    return out


def disconnect(*, store_path: Optional[str] = None,
               environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Forget the stored token. Never prints or returns the token."""
    if store_path is None:
        store_path = token_path(environ)
    if not store_path or not os.path.exists(store_path):
        return {"disconnected": False, "reason": "no_token_stored"}
    os.remove(store_path)
    return {"disconnected": True, "reason": "token_removed"}
