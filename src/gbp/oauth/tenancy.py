"""Tenant scoping for the live GBP connection (Issue #110).

The platform serves many nail salons from one account, so every read and every
write names exactly ONE tenant. This module is the single place that decides
what "belongs to a tenant" means for the OAuth layer (pending flows, encrypted
tokens and synced location rows), and it reuses the guarantees already
documented for the reply engine's tenancy module (Issue #71):
``src/gbp/replies/tenancy.py`` on the multi-tenant branch).

The three rules, all fail-closed:

1. an unknown, empty or non-string tenant yields NOTHING -- never "everything";
2. a record/row that belongs to another tenant is never returned;
3. two tenants are never conflated, not even when their display names differ
   only in case or surrounding whitespace (the tenant key is exact).

Standard library only, no network.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict, Iterable, List

__all__ = [
    "TenantIsolationError",
    "normalize_tenant",
    "tenant_key",
    "same_tenant",
    "assert_owner",
    "scope_rows",
]


class TenantIsolationError(RuntimeError):
    """Raised when a value belonging to another tenant is about to be used."""


def normalize_tenant(tenant: Any) -> str:
    """Validate a tenant id and return it unchanged (``str``).

    Deliberately NO case folding or stripping of the interior: two tenants that
    differ by one character are two tenants. Only a surrounding whitespace
    trim is applied, because that is an artefact of form/env input.
    """
    if tenant is None:
        raise TenantIsolationError("tenant id is required")
    value = tenant if isinstance(tenant, str) else str(tenant)
    value = value.strip()
    if not value:
        raise TenantIsolationError("tenant id must not be empty")
    return value


def tenant_key(tenant: Any) -> str:
    """A filesystem-safe, non-reversible key for a tenant.

    Used for the on-disk vault file name so a tenant id can never traverse
    paths or collide through a crafted value.
    """
    return hashlib.sha256(normalize_tenant(tenant).encode("utf-8")).hexdigest()


def same_tenant(a: Any, b: Any) -> bool:
    """Constant-time comparison of two tenant ids (``False`` on either error)."""
    try:
        return hmac.compare_digest(normalize_tenant(a), normalize_tenant(b))
    except TenantIsolationError:
        return False


def assert_owner(record_tenant: Any, tenant: Any) -> None:
    """Raise unless ``record_tenant`` is exactly ``tenant``."""
    if not same_tenant(record_tenant, tenant):
        raise TenantIsolationError(
            "refusing to use a record that belongs to another tenant")


def scope_rows(rows: Iterable[Dict[str, Any]], tenant: Any,
               key: str = "tenant_id") -> List[Dict[str, Any]]:
    """Keep only the rows tagged with ``tenant``.

    A row without the tag is dropped (an untagged row must never be attributed
    to whoever happens to be asking).
    """
    wanted = normalize_tenant(tenant)
    kept: List[Dict[str, Any]] = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        tagged = row.get(key)
        if tagged is None:
            continue
        if same_tenant(tagged, wanted):
            kept.append(row)
    return kept
