"""Google OAuth web-flow glue for the GBP platform (connect / callback / disconnect).

Lives in ``scripts/`` on purpose: ``src/gbp/auth/**`` is a protected path, so the auth
package is left untouched and the owner decides when (and whether) this moves there.

Security rules enforced here (all fail-closed):

* nothing calls Google unless :func:`live_ready` says the operator opted in
  (``GBP_ALLOW_LIVE_GOOGLE=1``), a client id/secret exist and a *safe* redirect URI is set;
* the redirect URI must be HTTPS, or the loopback form used for local development;
* ``state`` is mandatory, single-use, re-entrancy guarded, and **burned before the
  exchange** so a failed or concurrent callback can never reuse it;
* PKCE is never silently dropped: the verifier is handed to the exchanger when it accepts
  one, and the flow refuses to exchange when it does not;
* no token, client secret, authorization code or verifier is ever returned, logged, or
  allowed to escape inside an exception message.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import time
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

from gbp.auth.oauth import SCOPES, build_authorization_url, live_google_allowed

__all__ = [
    "GoogleSetupRequired", "FlowError", "live_ready", "check_redirect_uri", "start_flow",
    "finish_flow", "list_locations_read_only", "locations_fetcher", "disconnect",
    "token_path", "redact", "REDIRECT_ENV", "CLIENT_ID_ENV", "CLIENT_SECRET_ENV",
    "MAX_TEST_LOCATIONS",
]

CLIENT_ID_ENV = "GBP_GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "GBP_GOOGLE_CLIENT_SECRET"
REDIRECT_ENV = "GBP_OAUTH_REDIRECT_URI"
TOKEN_PATH_ENV = "GBP_TOKEN_PATH"

#: Hard cap for the connection test: list at most five locations.
MAX_TEST_LOCATIONS = 5

#: How long a started flow stays usable (seconds).
FLOW_TTL_SECONDS = 600

#: Shapes that must never appear in a message this module raises.
_SECRET_SHAPES = (
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{10,}\b"),
)


class GoogleSetupRequired(RuntimeError):
    """Raised when the live connection is not allowed/configured yet.

    ``reason`` is a stable code; the message is safe to show to an operator and contains
    no secret.
    """

    def __init__(self, reason: str, remedy: str = "") -> None:
        super().__init__("google_setup_required: %s%s" % (reason, (" — " + remedy) if remedy else ""))
        self.reason = reason
        self.remedy = remedy


class FlowError(RuntimeError):
    """Raised for a bad callback (bad state, replay, in-flight, PKCE or exchange error)."""

    def __init__(self, reason: str) -> None:
        super().__init__("google_flow_error: %s" % reason)
        self.reason = reason


def redact(text: Any, secrets_: Sequence[str] = ()) -> str:
    """Return ``text`` with any secret-shaped run and any known secret removed.

    Used on every message that could carry an exchanger's own error text, so a provider
    that echoes the request body cannot leak the code, the secret or a token.
    """
    out = str(text)
    for value in secrets_:
        if value:
            out = out.replace(str(value), "[redacted]")
    for pattern in _SECRET_SHAPES:
        out = pattern.sub("[redacted]", out)
    return out


def _conf(environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = os.environ if environ is None else environ
    return {
        "client_id": str(env.get(CLIENT_ID_ENV) or "").strip(),
        "client_secret": str(env.get(CLIENT_SECRET_ENV) or "").strip(),
        "redirect_uri": str(env.get(REDIRECT_ENV) or "").strip(),
    }


def check_redirect_uri(uri: str) -> Dict[str, Any]:
    """Say whether a redirect URI is acceptable, and why not when it is not.

    HTTPS is required; the loopback forms used for local development are allowed because
    they never leave the machine. A fragment is refused (the code would land in the URL
    bar and in history), and so is a relative or scheme-less value.
    """
    value = str(uri or "").strip()
    if not value:
        return {"ok": False, "reason": "redirect_uri_missing"}
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return {"ok": False, "reason": "redirect_uri_not_absolute"}
    if parts.fragment:
        return {"ok": False, "reason": "redirect_uri_has_fragment"}
    if parts.scheme == "https":
        return {"ok": True, "reason": None, "loopback": False}
    host = (parts.hostname or "").lower()
    if parts.scheme == "http" and host in ("127.0.0.1", "localhost", "::1"):
        return {"ok": True, "reason": None, "loopback": True}
    return {"ok": False, "reason": "redirect_uri_not_https"}


def live_ready(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Say whether a live call is permitted, without calling anything."""
    env = os.environ if environ is None else environ
    conf = _conf(env)
    opted_in = (str(env.get("GBP_ALLOW_LIVE_GOOGLE") or "").strip() == "1") if environ is not None else live_google_allowed()
    have = {"client_id": bool(conf["client_id"]), "client_secret": bool(conf["client_secret"]),
            "redirect_uri": bool(conf["redirect_uri"]), "opt_in": bool(opted_in)}
    if not opted_in:
        return {"ready": False, "reason": "live_opt_in_missing",
                "remedy": "set GBP_ALLOW_LIVE_GOOGLE=1 on purpose for this run", "have": have}
    for key, reason in (("client_id", "client_id_missing"), ("client_secret", "client_secret_missing")):
        if not conf[key]:
            return {"ready": False, "reason": reason,
                    "remedy": "set %s in the operator environment (never share it in chat)"
                              % (CLIENT_ID_ENV if key == "client_id" else CLIENT_SECRET_ENV),
                    "have": have}
    uri = check_redirect_uri(conf["redirect_uri"])
    if not uri["ok"]:
        return {"ready": False, "reason": uri["reason"],
                "remedy": "set %s to https://gbp.adzdigital.com/api/google/oauth/callback" % REDIRECT_ENV,
                "have": have}
    return {"ready": True, "reason": None, "remedy": "", "have": have}


def _pkce_pair() -> Dict[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return {"code_verifier": verifier, "code_challenge": challenge, "code_challenge_method": "S256"}


def _accepts_verifier(exchanger: Callable[..., Any]) -> bool:
    """True when the injected exchanger can take the PKCE verifier."""
    import inspect
    try:
        params = inspect.signature(exchanger).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return False
    if "code_verifier" in params:
        return True
    return any(p.kind == p.VAR_KEYWORD for p in params.values())


def start_flow(*, environ: Optional[Dict[str, str]] = None, now: Optional[float] = None,
               scopes: Optional[List[str]] = None) -> Dict[str, Any]:
    """Build the consent URL plus the session record the caller must store."""
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


def finish_flow(code: str, state: str, flow_session: Optional[Dict[str, Any]], *,
                environ: Optional[Dict[str, str]] = None, now: Optional[float] = None,
                exchanger: Optional[Callable[..., Any]] = None,
                store: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Verify the callback and persist the token. Returns safe metadata only.

    The state is burned **before** the exchange: a failed, replayed or concurrent
    callback can never reuse it, and a failure never leaves the state available for a
    second attempt.
    """
    state_info = live_ready(environ)
    if not state_info["ready"]:
        raise GoogleSetupRequired(state_info["reason"], state_info["remedy"])
    session = _check_state(flow_session, state, now)
    if not code:
        raise FlowError("missing_code")
    conf = _conf(environ)
    if exchanger is None:
        raise GoogleSetupRequired("exchanger_not_wired",
                                  "pass exchanger=gbp.auth.oauth.exchange_code-bound callable")
    if not _accepts_verifier(exchanger):
        # Never fall back to a verifier-less exchange: that would silently downgrade PKCE.
        raise FlowError("pkce_not_threaded")
    if not session.get("code_verifier"):
        # The flow was started with PKCE, so the exchange must carry the verifier.
        raise FlowError("pkce_verifier_missing")
    # Burn first: claim the state, then exchange. A crash or a concurrent callback now
    # finds the state unusable instead of racing for a second token.
    session["used"] = True
    session["used_at"] = float(now if now is not None else time.time())
    session["in_flight"] = True
    secrets_ = (conf["client_secret"], code, str(session.get("code_verifier") or ""))
    try:
        payload = exchanger(client_id=conf["client_id"], client_secret=conf["client_secret"],
                            code=code, redirect_uri=session.get("redirect_uri") or conf["redirect_uri"],
                            code_verifier=session.get("code_verifier"))
    except Exception as exc:  # noqa: BLE001 - any provider failure is reported safely
        raise FlowError("exchange_error: %s" % redact(exc, secrets_)) from None
    finally:
        session["in_flight"] = False
    token = payload.get("token") if isinstance(payload, dict) else payload
    if token is None:
        raise FlowError("exchange_failed")
    try:
        saved = (store or _default_store())(token)
    except Exception as exc:  # noqa: BLE001 - storage failure must not leak either
        raise FlowError("store_error: %s" % redact(exc, secrets_)) from None
    return {"connected": True, "saved": bool(saved), "scopes": list(session.get("scopes") or SCOPES),
            "token_redacted": True}


def _check_state(flow_session: Optional[Dict[str, Any]], state: str,
                 now: Optional[float] = None) -> Dict[str, Any]:
    if not flow_session or not flow_session.get("state"):
        raise FlowError("no_flow_in_session")
    if not state or not secrets.compare_digest(str(state), str(flow_session["state"])):
        raise FlowError("state_mismatch")
    age = float(now if now is not None else time.time()) - float(flow_session.get("created_at") or 0.0)
    if age > FLOW_TTL_SECONDS:
        raise FlowError("state_expired")
    if flow_session.get("in_flight"):
        raise FlowError("state_in_flight")
    if flow_session.get("used"):
        raise FlowError("state_replayed")
    return flow_session


def token_path(environ: Optional[Dict[str, str]] = None) -> str:
    """Where a stored token lives. Default is outside the repository, never inside it."""
    env = os.environ if environ is None else environ
    configured = str(env.get(TOKEN_PATH_ENV) or "").strip()
    if configured:
        return configured
    return os.path.join(os.path.expanduser("~"), ".gbp_token.json")


def _default_store() -> Callable[..., Any]:
    """Persist through the existing token manager (mode 0600, redacted repr)."""

    def _store(token: Any, path: Optional[str] = None) -> bool:
        from gbp.auth.token_manager import TokenManager
        manager = TokenManager(path or token_path())
        manager.token = token
        manager.save()
        return True

    return _store


def locations_fetcher(client: Any, *, page_size: int = MAX_TEST_LOCATIONS) -> Callable[..., Any]:
    """Wrap an injected read-only client into the fetcher this module expects.

    Nothing here builds a client or reaches the network by itself: the caller injects an
    already-authenticated client, which the live ladder must have allowed first. The
    adapter only reads and only keeps the four fields the preview needs.
    """
    base_url = "https://mybusinessbusinessinformation.googleapis.com/v1"
    limit = min(int(page_size or MAX_TEST_LOCATIONS), MAX_TEST_LOCATIONS)

    def _fetch(*, account: Optional[str] = None, limit: int = limit) -> List[Dict[str, Any]]:
        scope = str(account or "accounts/-")
        response = client.get(
            "%s/%s/locations" % (base_url, scope),
            params={"readMask": "name,title,storefrontAddress", "pageSize": min(int(limit), MAX_TEST_LOCATIONS)},
        )
        payload = response.json() if hasattr(response, "json") else response
        rows = (payload or {}).get("locations") or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            address = row.get("storefrontAddress") or {}
            lines = address.get("addressLines") or []
            out.append({
                "location_id": str(row.get("name") or ""),
                "business_name": str(row.get("title") or ""),
                "address": ", ".join(str(x) for x in lines),
                "zip": str(address.get("postalCode") or ""),
            })
        return out

    return _fetch


def list_locations_read_only(*, limit: int = MAX_TEST_LOCATIONS, environ: Optional[Dict[str, str]] = None,
                             fetcher: Optional[Callable[..., Any]] = None,
                             account: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read at most ``limit`` locations (default 5) -- read-only, never fabricated."""
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
