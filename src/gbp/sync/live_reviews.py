"""Read-only live REVIEW sync for the dashboard (Issue #123).

The second live read the platform performs: for each of the tenant's LIVE
locations it walks ``GET /v4/{full_name}/reviews`` through the existing
:class:`~gbp.sync.reviews_sync.ReviewSync` engine and upserts the real reviews
into the same dashboard model the offline path uses.

Naming (the gotcha this module exists to handle)
------------------------------------------------
Inc 2 persists live locations with ``business_locations.location_name`` in the
SHORT v1 form ``locations/{locationId}`` (that is what the Business Information
API returns as ``name``). The v4 reviews API instead needs the FULL resource
name ``accounts/{accountId}/locations/{locationId}``. :func:`compose_v4_name`
composes it from the location's owning ``google_accounts.name`` -- a row already
stored in the full form passes through unchanged.

No duplicate location rows
--------------------------
:class:`~gbp.sync.reviews_sync.ReviewSync`'s by-name upsert is only used when no
row id is known. Every live caller here passes the EXISTING row id, so the full
v4 name is never inserted as a second, bare location row and the reviews attach
to the live row the dashboard already shows.

Read-only by construction (mirrors :mod:`gbp.sync.live_sync`):

* only ``GET`` endpoints are reachable (``GbpClient.list_reviews``);
* the access token comes from the tenant's encrypted vault slot and is injected
  as an ``Authorization`` header, never stored on the client, never logged;
* pacing is the shared :class:`~gbp.sync.pacing.TokenBucket` (``<= 240`` QPM);
* a single location whose reviews call fails is isolated and reported as a safe
  reason -- it never aborts the other locations.

Standard library only, no network of its own.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from gbp.client.gbp_client import GbpClient
from gbp.oauth.tenancy import normalize_tenant
from gbp.oauth.vault import TokenVault
from gbp.sync.live_sync import AuthorizedHttp
from gbp.sync.pacing import DEFAULT_QPM, TokenBucket
from gbp.sync.persist import safe_reason
from gbp.sync.reviews_sync import ReviewSync

__all__ = ["LiveReviewSync", "compose_v4_name"]


def compose_v4_name(account_name: Any, location_name: Any) -> str:
    """Compose the full v4 resource name for a live location row.

    ``location_name`` is normally the SHORT v1 form (``locations/{id}``) that
    Inc 2 persisted; the full name is then ``{google_accounts.name}/{that}``.
    A name already in the full form (``accounts/.../locations/...``) passes
    through unchanged, so a re-composition is idempotent. A name that is
    neither (unexpected) is returned as-is rather than guessed at.
    """
    loc = str(location_name or "").strip()
    if loc.startswith("accounts/"):
        return loc  # already full form -- pass through
    if loc.startswith("locations/"):
        account = str(account_name or "").strip().rstrip("/")
        return f"{account}/{loc}" if account else loc
    return loc


class LiveReviewSync:
    """Read-only reviews sync for exactly one tenant's live locations."""

    def __init__(
        self,
        vault: TokenVault,
        tenant: Any,
        repo: Any,
        *,
        transport: Any = None,
        http: Any = None,
        quota: Any = None,
        base: Optional[str] = None,
        qpm: int = DEFAULT_QPM,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
        review_sync_factory: Optional[Callable[[Any, Any], Any]] = None,
    ) -> None:
        self.vault = vault
        self.tenant = normalize_tenant(tenant)
        self.repo = repo
        self._transport = transport
        self._clock: Callable[[], float] = clock or time.time
        self.quota = quota if quota is not None else TokenBucket(qpm)
        # A bare ``http`` (an HttpClient or a MockTransport) wins; else a live
        # UrllibTransport wrapped in an HttpClient with retry/backoff -- the same
        # construction ``LiveLocationSync`` uses.
        if http is None:
            from gbp.client.http_client import HttpClient
            from gbp.client.urllib_transport import UrllibTransport
            http = HttpClient(transport if transport is not None else UrllibTransport())
        self._authorized = AuthorizedHttp(
            http, lambda: self.vault.get_valid(self.tenant, self._transport)
        )
        kwargs: Dict[str, Any] = {"http": self._authorized, "quota": self.quota}
        if base:
            kwargs["base"] = base
        self.client = GbpClient(**kwargs)
        # The engine is injectable so tests can drive it without a socket.
        factory = review_sync_factory or (lambda repo, client: ReviewSync(repo, client))
        self.reviews = factory(repo, self.client)

    # -- orchestration ----------------------------------------------------- #
    def sync_all(self, targets: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Sync reviews for every live location in ``targets``.

        Each target is a mapping with ``location_row_id``, ``business_name``,
        ``location_name`` (short or full form) and ``account_name`` (the owning
        ``google_accounts.name``). A location whose reviews call fails is caught,
        reduced to a safe reason and reported -- the remaining locations still
        sync. Returns the summary the endpoint renders; never a token or a raw
        Google body.
        """
        per_location: List[Dict[str, Any]] = []
        reviews_upserted = 0
        errors = 0
        synced = 0
        for target in targets or []:
            row_id = target.get("location_row_id")
            business_name = target.get("business_name") or ""
            full_name = compose_v4_name(target.get("account_name"),
                                        target.get("location_name"))
            entry: Dict[str, Any] = {
                "location_id": row_id,
                "business_name": business_name,
                "upserted": 0,
                "errors": 0,
            }
            try:
                result = self.reviews.sync_location(
                    full_name, location_row_id=row_id)
                up = int(result.get("upserted") or 0)
                errs = int(result.get("errors") or 0)
                entry["upserted"] = up
                entry["errors"] = errs
                reviews_upserted += up
                errors += errs
                synced += 1
            except Exception as exc:  # noqa: BLE001 - isolate per location
                entry["errors"] = 1
                entry["reason"] = safe_reason(exc)
                errors += 1
            per_location.append(entry)

        if errors == 0:
            status = "ok"
        elif synced:
            status = "partial"
        else:
            status = "failed"
        summary: Dict[str, Any] = {
            "tenant_id": self.tenant,
            "locations": len(per_location),
            "reviews_upserted": reviews_upserted,
            "errors": errors,
            "per_location": per_location,
            "sync_status": status,
            "last_sync": _iso(self._clock()),
        }
        if status != "ok":
            # First failure reason, already safe, for the tenant-level outcome.
            reason = next((e["reason"] for e in per_location if e.get("reason")),
                          "sync_failed")
            summary["reason"] = reason
        return summary


def _iso(stamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
