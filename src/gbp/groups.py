"""GBP business-group projection (Issue #112).

A GBP *business group* is an account of type ``LOCATION_GROUP`` in the Google
Account Management API. Locations always live under an account, so every
location row already knows its account -- this module turns that account into
the three dashboard-facing group fields carried on every location row:

    group_id    the account resource name (``accounts/123``) -- the group identity
    group_name  the group's display name, or ``"Ungrouped"`` for locations that
                sit on a primary / personal account
    group_type  the raw account type (``LOCATION_GROUP`` / ``PERSONAL`` / ...)

The module is deliberately tiny, standard-library only and network-free so the
same projection is used by the live sync bundle, the fixtures/demo seeder and
the read-only API endpoints -- one naming rule, no drift.

Read-only by construction: nothing here can create, rename or move a group.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "UNGROUPED",
    "LOCATION_GROUP",
    "is_group_account",
    "group_fields",
    "group_summary",
]

#: Display name used for locations that sit on a primary/personal account.
UNGROUPED = "Ungrouped"

#: The account type that IS a business group in the Account Management API.
LOCATION_GROUP = "LOCATION_GROUP"


def is_group_account(account_type: Any) -> bool:
    """True when ``account_type`` denotes a real business group."""
    return str(account_type or "").strip().upper() == LOCATION_GROUP


def group_fields(group_id: Any, account_name: Any = None,
                 account_type: Any = None) -> Dict[str, Any]:
    """Return the three group fields for one account.

    ``group_id`` is the account id (its resource name). A ``LOCATION_GROUP``
    account keeps its own display name; anything else -- a primary/personal
    account, or an account with no type at all -- is reported as
    :data:`UNGROUPED`, so the naming rule lives in exactly one place.
    """
    gid = "" if group_id is None else str(group_id)
    name = str(account_name).strip() if account_name else ""
    if is_group_account(account_type):
        return {
            "group_id": gid,
            "group_name": name or UNGROUPED,
            "group_type": str(account_type),
        }
    return {
        "group_id": gid,
        "group_name": UNGROUPED,
        "group_type": (str(account_type) if account_type else None),
    }


def group_summary(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-group location counts over ``rows``, always fully populated.

    Every distinct group present in ``rows`` becomes one entry
    ``{"group_id", "group_name", "group_type", "count"}``. Rows without group
    fields are counted as :data:`UNGROUPED` rather than dropped, so a count is
    never silently hidden. Real groups are ordered by name and ``Ungrouped``
    always sorts last, so the order is stable across requests.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        gid = row.get("group_id")
        gid = "" if gid is None else str(gid)
        name = row.get("group_name") or UNGROUPED
        entry = by_id.get(gid)
        if entry is None:
            entry = {
                "group_id": gid,
                "group_name": str(name),
                "group_type": row.get("group_type"),
                "count": 0,
            }
            by_id[gid] = entry
        entry["count"] += 1
    return sorted(by_id.values(),
                  key=lambda g: (g["group_name"] == UNGROUPED,
                                 str(g["group_name"]).lower(), g["group_id"]))
