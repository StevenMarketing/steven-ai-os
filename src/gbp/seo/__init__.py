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
from .preview import (
    BANNER,
    DATASET_NAMES,
    SCORE_LABEL,
    DemoError,
    PreviewError,
    build_preview,
)
from .gap import (
    GAP_DISCLAIMER,
    SMALL_N_MINIMUM,
    GapReport,
    analyze_gap,
    load_gap_fixture,
)
from .ranking import (
    DEFAULT_KEYWORDS,
    DEFAULT_LOCATIONS,
    DEFAULT_SEED,
    DEFAULT_WEEKS,
    SIMULATED,
    SIMULATED_LABEL,
    VISIBILITY_INDEX_LABEL,
    LocationPeriodSummary,
    RankObservation,
    RankSeries,
    RankingReport,
    simulate_dataset,
    simulate_series,
    summarize_location,
    summarize_series,
)

__all__ = [
    "ALLOWED_PRIMARY_CATEGORIES",
    "BANNER",
    "DATASET_NAMES",
    "DISCLAIMER",
    "DemoError",
    "GAP_DISCLAIMER",
    "DEFAULT_KEYWORDS",
    "DEFAULT_LOCATIONS",
    "DEFAULT_SEED",
    "DEFAULT_WEEKS",
    "PREFERRED_PRIMARY_CATEGORY",
    "PreviewError",
    "SCORE_LABEL",
    "SIMULATED",
    "SIMULATED_LABEL",
    "SMALL_N_MINIMUM",
    "VISIBILITY_INDEX_LABEL",
    "AuditReport",
    "Finding",
    "GapReport",
    "LocationAudit",
    "LocationPeriodSummary",
    "RankObservation",
    "RankSeries",
    "RankingReport",
    "analyze_gap",
    "audit_location",
    "audit_profiles",
    "build_preview",
    "load_gap_fixture",
    "load_profiles",
    "severity_rank",
    "simulate_dataset",
    "simulate_series",
    "summarize_location",
    "summarize_series",
]
