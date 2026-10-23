"""Read-only live accounts/locations sync for the dashboard (Issue #110).

The FIRST live read the platform performs: list the Google accounts the tenant
granted access to, then the locations under each account, and map both into the
dashboard data model (business name, ZIP, sync status, last sync).

Read-only by construction:

* only ``GET`` endpoints are reachable (:class:`~gbp.client.gbp_client.GbpClient`
  is used for ``list_accounts`` / ``list_locations`` only);
* the access token comes from the tenant's encrypted vault slot and is injected
  as an ``Authorization`` header, never stored on the client, never logged;
* pacing is a :class:`~gbp.sync.pacing.TokenBucket` well under 300 QPM, and the
  underlying ``HttpClient`` already retries 429/5xx with exponential backoff and
  jitter, honouring ``Retry-After``;
* every mapped row carries its ``tenant_id`` so a caller can only ever consume
  its own tenant's rows (see :func:`gbp.oauth.tenancy.scope_rows`).

The transport is injected, so the whole module is exercised offline.

Standard library only, no network of its own.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from gbp.client.gbp_client import GbpClient
from gbp.oauth.tenancy import normalize_tenant
from gbp.oauth.vault import TokenVault, TokenExpiredError
from gbp.sync.pacing import DEFAULT_QPM, TokenBucket

__all__ = ["AuthorizedHttp", "LiveLocationSync", "map_account", "map_location"]


class AuthorizedHttp:
    """Wrap an ``HttpClient`` so every request carries the tenant's bearer token.

    The token is fetched per call (so an expired one is refreshed transparently)
    and is never copied out of the vault.
    """

    def __init__(self, http: Any, token_provider: Callable[[], Any]) -> None:
        self._http = http
        self._token_provider = token_provider
        #: Set after the first call; exposes the tenant id the token belonged to.
        self.last_tenant: Optional[str] = None

    def request(self, method: str, url: str, params: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None, data: Optional[Any] = None) -> Any:
        token = self._token_provider()
        merged = dict(headers or {})
        merged["Authorization"] = "Bearer %s" % (token.access_token,)
        return self._http.request(method, url, params=params, headers=merged, data=data)


def map_account(account: Dict[str, Any], *, tenant: str) -> Dict[str, Any]:
    """Normalise one GBP account into the dashboard shape."""
    name = str((account or {}).get("name") or "")
    return {
        "tenant_id": tenant,
        "account_id": name,
        "account_name": str((account or {}).get("accountName")
                            or (account or {}).get("name") or ""),
        "type": (account or {}).get("type"),
    }


def map_location(location: Dict[str, Any], *, tenant: str, account: str,
                 sync_status: str = "ok", last_sync: Optional[str] = None) -> Dict[str, Any]:
    """Normalise one GBP location into the dashboard rows model.

    Fields are the ones the existing dashboard already renders: business name
    (title), ZIP (postal code), sync status, last sync.
    """
    address = (location or {}).get("storefrontAddress") or {}
    title = (location or {}).get("title") or (location or {}).get("locationName") or ""
    return {
        "tenant_id": tenant,
        "location_id": str((location or {}).get("name") or ""),
        "account_id": account,
        "business_name": str(title),
        "zip": str(address.get("postalCode") or (location or {}).get("postalCode") or ""),
        "city": str(address.get("locality") or (location or {}).get("city") or ""),
        "region": str(address.get("administrativeArea") or ""),
        "sync_status": str(sync_status),
        "last_sync": last_sync,
    }


class LiveLocationSync:
    """Read-only accounts/locations sync for exactly one tenant."""

    def __init__(self, vault: TokenVault, tenant: Any, *, transport: Any = None,
                 http: Any = None, quota: Any = None, base: Optional[str] = None,
                 qpm: int = DEFAULT_QPM, client_id: Optional[str] = None,
                 client_secret: Optional[str] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.vault = vault
        self.tenant = normalize_tenant(tenant)
        self._transport = transport
        self._clock: Callable[[], float] = clock or time.time
        self.quota = quota if quota is not None else TokenBucket(qpm)
        # A bare ``http`` (already an HttpClient or a MockTransport) wins; else a
        # live UrllibTransport wrapped in an HttpClient with retry/backoff.
        if http is None:
            from gbp.client.http_client import HttpClient
            from gbp.client.urllib_transport import UrllibTransport
            http = HttpClient(transport if transport is not None else UrllibTransport())
        self._authorized = AuthorizedHttp(
            http, lambda: self.vault.get_valid(self.tenant, self._transport))
        kwargs: Dict[str, Any] = {"http": self._authorized, "quota": self.quota}
        if base:
            kwargs["base"] = base
        self.client = GbpClient(**kwargs)

    # -- reads ------------------------------------------------------------- #
    def list_accounts(self) -> List[Dict[str, Any]]:
        """Every Google account the tenant granted access to (read-only)."""
        payload = self.client.list_accounts() or {}
        rows = payload.get("accounts") if isinstance(payload, dict) else None
        return [map_account(a, tenant=self.tenant) for a in (rows or [])]

    def list_locations(self, account: Any) -> List[Dict[str, Any]]:
        """Every location under ``account`` (read-only, paginated)."""
        name = account.get("account_id") if isinstance(account, dict) else account
        now_iso = _iso(self._clock())
        return [map_location(loc, tenant=self.tenant, account=str(name),
                             last_sync=now_iso)
                for loc in (self.client.list_locations(name) or [])]

    # -- orchestration ----------------------------------------------------- #
    def sync(self, *, accounts: Optional[List[Dict[str, Any]]] = None,
             record: bool = True) -> Dict[str, Any]:
        """Read accounts + their locations and return the dashboard bundle.

        ``record`` writes the sync result (time + status) into the tenant's
        vault record. A token that cannot be refreshed surfaces as
        ``token_expired`` instead of raising into the caller's request handler.
        """
        started = self._clock()
        try:
            account_rows = accounts if accounts is not None else self.list_accounts()
            locations: List[Dict[str, Any]] = []
            for account in account_rows:
                locations.extend(self.list_locations(account))
        except TokenExpiredError:
            if record:
                self.vault.mark_error(self.tenant, "token_expired")
            return {
                "tenant_id": self.tenant,
                "accounts": [],
                "locations": [],
                "sync_status": "token_expired",
                "last_sync": _iso(self._clock()),
            }
        finished = _iso(self._clock())
        bundle = {
            "tenant_id": self.tenant,
            "accounts": account_rows,
            "locations": locations,
            "sync_status": "ok",
            "last_sync": finished,
            "duration_ms": int((self._clock() - started) * 1000),
        }
        if record:
            self.vault.note_sync(self.tenant, "ok", finished)
        return bundle


def _iso(stamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
