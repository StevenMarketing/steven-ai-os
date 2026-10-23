"""Single-use, TTL-bounded OAuth connect flows, one per tenant (Issue #110).

A "flow" is the server-side record created when a business clicks *Connect
Google*. It carries the CSRF ``state`` and a second ``nonce``; both must come
back on the callback, and the record is BURNED before the code exchange so a
replayed, expired or concurrent callback can never reuse it.

The store is in-process and injectable (``clock``), so tests drive time
directly and never sleep. Nothing here is persisted: a pending flow is
short-lived by design, and the *token* is what the vault persists.

Standard library only, no network.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Callable, Dict, List, Optional

from gbp.oauth.tenancy import normalize_tenant, tenant_key

__all__ = ["FlowStateError", "StateStore", "DEFAULT_FLOW_TTL_SECONDS"]

#: How long a started flow stays usable (seconds).
DEFAULT_FLOW_TTL_SECONDS = 600


class FlowStateError(RuntimeError):
    """Raised for a bad callback: missing/mismatched state, replay or expiry."""

    def __init__(self, reason: str) -> None:
        super().__init__("oauth_flow_error: %s" % reason)
        self.reason = reason


class StateStore:
    """Per-tenant pending-flow store with strict, fail-closed validation."""

    def __init__(self, *, ttl_seconds: int = DEFAULT_FLOW_TTL_SECONDS,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.ttl_seconds = int(ttl_seconds)
        self._clock: Callable[[], float] = clock or time.time
        self._flows: Dict[str, Dict[str, Any]] = {}

    # -- lifecycle --------------------------------------------------------- #
    def start(self, tenant: Any, *, redirect_uri: str, scopes: List[str],
              state: Optional[str] = None, nonce: Optional[str] = None) -> Dict[str, Any]:
        """Create (and store) the flow record for ``tenant``.

        ``state``/``nonce`` may be injected by a test; production generates two
        independent URL-safe random values.
        """
        key = tenant_key(tenant)
        now = self._clock()
        record: Dict[str, Any] = {
            "tenant_id": normalize_tenant(tenant),
            "state": state or secrets.token_urlsafe(32),
            "nonce": nonce or secrets.token_urlsafe(32),
            "redirect_uri": str(redirect_uri),
            "scopes": list(scopes),
            "created_at": now,
            "used": False,
            "used_at": None,
            "in_flight": False,
        }
        self._flows[key] = record
        return record

    def peek(self, tenant: Any) -> Optional[Dict[str, Any]]:
        """The pending flow for ``tenant`` (or ``None``) -- never another tenant's."""
        return self._flows.get(tenant_key(tenant))

    def clear(self, tenant: Any) -> bool:
        """Forget the pending flow for ``tenant``; returns whether one existed."""
        return self._flows.pop(tenant_key(tenant), None) is not None

    # -- validation -------------------------------------------------------- #
    def consume(self, tenant: Any, state: Any, nonce: Any) -> Dict[str, Any]:
        """Validate and BURN the flow, then return it.

        Order matters: existence, then state, then nonce, then freshness, then
        single-use. Every failure raises :class:`FlowStateError` with a stable
        reason, and no failure leaves the flow usable for a second attempt.
        """
        record = self._flows.get(tenant_key(tenant))
        if not record:
            raise FlowStateError("no_flow_in_session")
        if not state or not secrets.compare_digest(str(state), str(record["state"])):
            raise FlowStateError("state_mismatch")
        if not nonce or not secrets.compare_digest(str(nonce), str(record["nonce"])):
            raise FlowStateError("nonce_mismatch")
        age = self._clock() - float(record.get("created_at") or 0.0)
        if age > self.ttl_seconds:
            raise FlowStateError("state_expired")
        if record.get("in_flight"):
            raise FlowStateError("state_in_flight")
        if record.get("used"):
            raise FlowStateError("state_replayed")
        # Burn first: claim the flow before the exchange, so a crash or a
        # concurrent callback finds it unusable instead of racing for a token.
        record["used"] = True
        record["used_at"] = self._clock()
        record["in_flight"] = True
        return record

    def release(self, tenant: Any) -> None:
        """Clear the in-flight flag once the exchange settled (success or not)."""
        record = self._flows.get(tenant_key(tenant))
        if record is not None:
            record["in_flight"] = False
