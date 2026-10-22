"""Offline GMap SEO audit package.

Deterministic, dependency-free auditing of **synthetic** Google Business Profile
fixtures for nail salons. Nothing here contacts Google, opens a socket, or edits
a live profile: every finding is derived from the fixture fields handed in.

Re-uses the multi-location identity convention of the shared dashboard
(``name + ZIP``), see ``src/gbp/repo/sqlite_repo.py`` and ``docs/DOMAIN-RULES.md``.
"""

from __future__ import annotations

from .audit import (
    ALLOWED_PRIMARY_CATEGORIES,
    DISCLAIMER,
    PREFERRED_PRIMARY_CATEGORY,
    AuditReport,
    Finding,
    LocationAudit,
    audit_location,
    audit_profiles,
    load_profiles,
    severity_rank,
)

__all__ = [
    "ALLOWED_PRIMARY_CATEGORIES",
    "DISCLAIMER",
    "PREFERRED_PRIMARY_CATEGORY",
    "AuditReport",
    "Finding",
    "LocationAudit",
    "audit_location",
    "audit_profiles",
    "load_profiles",
    "severity_rank",
]
