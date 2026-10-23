"""Persist a live accounts/locations sync bundle (Issue #117).

Issue #110's :class:`~gbp.sync.live_sync.LiveLocationSync` returns a bundle of
accounts + locations; before this module the HTTP handler threw that bundle
away, so a tenant could connect Google successfully and still see the demo
fixtures. This module is the ONE writer that turns a bundle into dashboard rows:

* accounts land in ``google_accounts`` (keyed on the Google account resource
  name) with their display name + type, which is what the business-group
  projection (:mod:`gbp.groups`) reads back;
* locations land in ``business_locations`` keyed on the Google location resource
  name (``accounts/{id}/locations/{id}``), tagged with ``tenant_id`` and
  ``source = 'live'``;
* a location that Google no longer returns is MARKED (``sync_status =
  'removed'``), never deleted and never left looking freshly synced;
* every attempt also writes the sync outcome into the tenant's vault meta
  (``sync_status`` / ``last_sync_at`` / a safe error reason).

Guarantees
----------
* **Idempotent**: the upserts are keyed on the Google resource name, so running
  the same bundle twice adds no rows and changes no ids.
* **Tenant-scoped**: a row already owned by a DIFFERENT tenant is never
  overwritten (the write is refused and reported as ``conflicts``); an untagged
  row (the demo seed) is claimed by the tenant that syncs it, because the
  resource name identifies the same Google location.
* **Secret-free**: only the fields the mapping layer produced are stored, and an
  error reason is reduced to an exception CLASS name or an API error CODE --
  never a token, never a raw provider body.

Standard library only; no network of its own.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from gbp.oauth.tenancy import normalize_tenant

__all__ = [
    "LIVE_SOURCE",
    "safe_reason",
    "persist_bundle",
    "record_outcome",
]

#: ``business_locations.source`` value written by a real Google sync.
LIVE_SOURCE = "live"

#: Status recorded for a location that a later sync no longer returned.
REMOVED_STATUS = "removed"

#: Status recorded on a successful sync attempt.
OK_STATUS = "ok"

#: Error codes the vault treats as "the token is unusable" are already handled
#: by :class:`~gbp.oauth.vault.TokenVault`; everything else is a plain failure.
_ERROR_ALREADY_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:+-]{0,63}$")


def safe_reason(value: Any) -> str:
    """Reduce a failure to a class name / error code safe to persist and show.

    Accepts an exception (its class name is used), a short code-like string, or
    a dict with an ``error``/``code``/``status`` key (the provider's own error
    CODE only). Anything else -- a long message, a URL, a response body -- is
    replaced by ``"unknown_error"``, so a provider body or a token can never be
    stored through this path.
    """
    if isinstance(value, BaseException):
        return type(value).__name__
    if isinstance(value, dict):
        for key in ("error", "code", "status", "reason"):
            candidate = value.get(key)
            if isinstance(candidate, str) and _ERROR_ALREADY_SAFE.match(
                    candidate.strip()):
                return candidate.strip()
        return "unknown_error"
    text = "" if value is None else str(value).strip()
    if _ERROR_ALREADY_SAFE.match(text):
        return text
    return "unknown_error"


def _iso(stamp: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() if stamp is None else stamp))


def persist_bundle(repo: Any, tenant: Any, bundle: Optional[Dict[str, Any]],
                   *, now_iso: Optional[str] = None) -> Dict[str, Any]:
    """Upsert one sync bundle for ``tenant`` and report what changed.

    Returns ``{"accounts", "locations", "removed", "conflicts", "sync_status",
    "last_sync_at"}`` -- the counts the "Sync now" endpoint reports back. The
    counts are of PERSISTED rows (the whole bundle, re-sync included), which is
    what the dashboard shows, not of newly-inserted rows.
    """
    tid = normalize_tenant(tenant)
    data = bundle if isinstance(bundle, dict) else {}
    stamp = now_iso or data.get("last_sync") or _iso()
    status = str(data.get("sync_status") or OK_STATUS)

    account_rows: List[Dict[str, Any]] = [
        row for row in (data.get("accounts") or []) if isinstance(row, dict)]
    location_rows: List[Dict[str, Any]] = [
        row for row in (data.get("locations") or []) if isinstance(row, dict)]

    account_ids: Dict[str, int] = {}
    conflicts = 0
    for row in account_rows:
        name = str(row.get("account_id") or row.get("name") or "").strip()
        if not name:
            continue
        owner = repo.account_tenant(name)
        if owner is not None and owner != tid:
            conflicts += 1
            continue  # another tenant's account: never touched
        account_ids[name] = repo.upsert_account(
            name, row.get("account_name") or name, row.get("type"),
            tenant_id=tid)

    kept: List[str] = []
    for row in location_rows:
        name = str(row.get("location_id") or row.get("name") or "").strip()
        if not name:
            continue
        owner = repo.location_tenant(name)
        if owner is not None and owner != tid:
            conflicts += 1
            continue  # another tenant's location: never touched
        account_name = str(row.get("account_id") or "").strip()
        account_id = account_ids.get(account_name)
        if account_id is None:
            account_id = repo.upsert_account(
                account_name, row.get("group_name"), None, tenant_id=tid)
        repo.upsert_location(
            account_id, name,
            title=row.get("business_name") or row.get("title"),
            city=row.get("city"), state=row.get("region") or row.get("state"),
            language=row.get("language"),
            postal_code=row.get("zip") or row.get("postal_code"),
            tenant_id=tid, source=LIVE_SOURCE,
            sync_status=str(row.get("sync_status") or status),
            last_sync_at=str(row.get("last_sync") or stamp))
        kept.append(name)

    removed = repo.mark_locations_absent(tid, kept, status=REMOVED_STATUS,
                                         at=stamp)
    return {
        "tenant_id": tid,
        "accounts": len(account_ids),
        "locations": len(kept),
        "removed": int(removed),
        "conflicts": int(conflicts),
        "sync_status": status,
        "last_sync_at": stamp,
    }


def record_outcome(vault: Any, tenant: Any, bundle: Optional[Dict[str, Any]],
                   *, now_iso: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Record a sync attempt in the tenant's vault meta (never a raw body).

    A success writes ``sync_status`` + ``last_sync_at`` and clears any previous
    error; a failure writes the same two fields PLUS a safe error reason
    (exception class name / API error code only). Returns the non-secret meta,
    or ``None`` when nothing is stored for the tenant.
    """
    tid = normalize_tenant(tenant)
    data = bundle if isinstance(bundle, dict) else {}
    status = str(data.get("sync_status") or "error")
    at = data.get("last_sync") or now_iso or _iso()
    if status == OK_STATUS:
        return vault.note_outcome(tid, status, at=at, clear_error=True)
    reason = data.get("reason")
    if reason is None and status not in ("token_expired",):
        reason = "sync_failed"
    if status == "token_expired":
        # The vault already knows this means "the stored token is unusable";
        # record the same code so the row stays consistent with
        # ``connection_state``.
        reason = status
    return vault.note_outcome(
        tid, status, at=at,
        error=safe_reason(reason) if reason is not None else None)
