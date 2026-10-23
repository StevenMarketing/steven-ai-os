"""HTTP wiring for the per-tenant Google connect flow (Issue #114).

The Inc 1 OAuth engine (``gbp.oauth.flow`` / ``state`` / ``vault``) already
implements the connect/callback/disconnect orchestration, but an HTTP request
handler needs two things the engine deliberately does not own:

* a ``state -> tenant`` INDEX, so the callback derives the tenant from the
  server-side flow record. The ``state`` query parameter is only a LOOKUP key
  here -- it is never a tenant, and a callback for an unknown state is
  rejected. This is what makes a cross-tenant callback impossible.
* INJECTABLE transports: the token-exchange transport and the read-only
  accounts/locations sync transport, so the whole flow runs offline in tests
  and only ever opens a socket when an operator injects a live one.

Nothing here opens a socket by itself; the default transports are created
lazily and are gated by the existing opt-in
(:func:`gbp.auth.oauth.guard_transport`). No token, code, secret or verifier is
ever returned, logged or put in an exception message.

Standard library only.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from gbp.oauth.config import require_config
from gbp.oauth.flow import (
    disconnect as flow_disconnect,
    handle_callback,
    start_connect,
)
from gbp.oauth.state import FlowStateError, StateStore

__all__ = ["GoogleConnectWiring"]


class GoogleConnectWiring:
    """Per-server bridge between the OAuth engine and the HTTP routes.

    One instance is created per HTTP server (it holds the in-process pending
    flows and the ``state -> tenant`` index), so a connect started on one
    request is finished by the callback on a later request.

    Parameters
    ----------
    store:
        The pending-flow store (a :class:`~gbp.oauth.state.StateStore`). A
        fresh one is created when omitted; tests inject one to drive the clock.
    token_transport:
        The authorization-code exchange transport. Defaults (lazily) to a
        live ``UrllibTransport``, which ``guard_transport`` refuses unless the
        operator opted in -- so an accidental live call stays impossible.
    api_transport:
        The read-only sync transport. Defaults (lazily) to a live transport.
    sync_factory:
        ``(tenant, vault) -> object with .sync()``; overrides how the
        post-callback read-only sync is built (tests inject a fake).
    vault_factory:
        ``(environ, config) -> TokenVault``; overrides where the encrypted
        per-tenant records live (tests point it at a temp dir).
    clock:
        Injectable ``() -> float`` epoch clock (tests drive time directly).
    """

    def __init__(
        self,
        *,
        store: Optional[StateStore] = None,
        token_transport: Any = None,
        api_transport: Any = None,
        sync_factory: Optional[Callable[[Any, Any], Any]] = None,
        vault_factory: Optional[Callable[[Any, Any], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.store = store if store is not None else StateStore()
        self._token_transport = token_transport
        self._api_transport = api_transport
        self._sync_factory = sync_factory
        self._vault_factory = vault_factory
        self._clock = clock
        #: state -> tenant id (the tenant is NEVER taken from the callback URL).
        self._tenant_by_state: Dict[str, str] = {}

    # -- collaborators --------------------------------------------------- #
    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.time()

    def _transport(self) -> Any:
        if self._token_transport is None:
            from gbp.client.urllib_transport import UrllibTransport

            self._token_transport = UrllibTransport()
        return self._token_transport

    def _vault(self, environ: Optional[Dict[str, Any]], config: Any) -> Any:
        if self._vault_factory is not None:
            return self._vault_factory(environ, config)
        from gbp.oauth.config import encryption_key, vault_dir
        from gbp.oauth.vault import TokenVault

        return TokenVault(
            vault_dir(environ), encryption_key(environ),
            client_id=config.client_id, client_secret=config.client_secret)

    def _sync(self, tenant: str, vault: Any, config: Any) -> Any:
        if self._sync_factory is not None:
            return self._sync_factory(tenant, vault)
        from gbp.client.http_client import HttpClient
        from gbp.sync.live_sync import LiveLocationSync

        http = None
        if self._api_transport is not None:
            http = HttpClient(self._api_transport)
        return LiveLocationSync(
            vault, tenant, http=http,
            client_id=config.client_id, client_secret=config.client_secret)

    # -- routes ---------------------------------------------------------- #
    def start(self, tenant: Any, *, environ: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Begin a connect flow and return the consent URL + state/nonce.

        Raises :class:`~gbp.oauth.config.LiveNotConfigured` when the live path
        is not fully configured -- the caller turns that into an honest
        refusal, never a fake redirect.
        """
        result = start_connect(tenant, store=self.store, environ=environ)
        # Index state -> tenant so the callback derives the tenant from the
        # server-side flow record (the state param is only a lookup key).
        self._tenant_by_state[str(result["state"])] = result["tenant_id"]
        return result

    def tenant_for_state(self, state: Any) -> Optional[str]:
        """The tenant that started the flow behind ``state`` (or ``None``)."""
        if not state:
            return None
        return self._tenant_by_state.get(str(state))

    def callback(self, *, code: str, state: str,
                 environ: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Finish a connect flow and run the read-only sync. Returns safe meta.

        Raises :class:`~gbp.oauth.state.FlowStateError` for an unknown, bad,
        expired or replayed state, and :class:`~gbp.oauth.flow.OAuthExchangeError`
        when the code exchange fails -- both WITHOUT a token ever surfacing.
        """
        tenant = self.tenant_for_state(state)
        if tenant is None:
            raise FlowStateError("unknown_state")
        record = self.store.peek(tenant) or {}
        nonce = str(record.get("nonce") or "")
        config = require_config(environ)
        vault = self._vault(environ, config)
        result = handle_callback(
            code, state, nonce, tenant=tenant, store=self.store, vault=vault,
            transport=self._transport(), environ=environ, now=self._now())
        sync_bundle = self._run_sync(tenant, vault, config)
        return {"tenant_id": tenant, "connection": result, "sync": sync_bundle}

    def sync(self, tenant: Any, *,
             environ: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run the read-only sync for ``tenant`` on demand (Issue #117).

        Backs the session-scoped "Sync now" route: it resolves the vault the
        same way the callback does and returns the SAME bundle shape, so the
        caller can persist it through :func:`gbp.sync.persist.persist_bundle`.
        GET-only against Google (see :mod:`gbp.sync.live_sync`).
        """
        config = require_config(environ)
        vault = self._vault(environ, config)
        return self._run_sync(tenant, vault, config)

    def _run_sync(self, tenant: str, vault: Any, config: Any) -> Dict[str, Any]:
        try:
            bundle = self._sync(tenant, vault, config).sync()
        except Exception as exc:  # noqa: BLE001 - a sync failure must not
            # invalidate a token the tenant just granted; report it honestly
            # and never leak the underlying message (it could echo a URL).
            bundle = {"tenant_id": tenant, "sync_status": "error",
                      "reason": type(exc).__name__, "accounts": [],
                      "locations": []}
        # Issue #117: EVERY attempt leaves a trace (status + timestamp, or a
        # safe reason) -- the connect-time sync used to be silently swallowed.
        try:
            from gbp.sync.persist import record_outcome

            record_outcome(vault, tenant, bundle)
        except Exception:  # noqa: BLE001 - bookkeeping must not break the flow
            pass
        return bundle

    def disconnect(self, tenant: Any, *,
                   environ: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Forget one tenant's stored token (Google-side revoke is best-effort)."""
        config = require_config(environ)
        vault = self._vault(environ, config)
        return flow_disconnect(tenant, vault)
