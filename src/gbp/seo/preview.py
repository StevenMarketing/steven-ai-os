"""Read-only, offline presentation layer for the GMap SEO audit (issue #42).

This module is a thin *view model* on top of the issue #39 audit engine
(:mod:`gbp.seo.audit`). It re-implements **no** audit logic: it calls
``audit_profiles`` and reshapes the deterministic :class:`~gbp.seo.audit.AuditReport`
into the structure the web dashboard preview renders.

Why a separate module
---------------------
The audit engine answers "what is wrong with this profile?"; the dashboard needs
a *presentable* answer: a per-salon card with an explicit score **label**, the
findings split into categories vs services, the missing required fields pulled
out, and a visible demo/offline banner. Keeping that shaping here means the
engine stays untouched and the UI never has to re-derive anything.

Guarantees (mirrors the engine's contract)
------------------------------------------
* **Offline.** No socket, no Google call, no credential, no write. Synthetic
  fixtures only.
* **Deterministic.** Same dataset in -> byte-identical structure out (the engine
  sorts; this module only re-buckets and never iterates a set).
* **Honest labels.** The score is a heuristic **completeness** score -- never a
  Google ranking score -- and the informational ``SEO-IDENTITY-REUSED-NAME``
  finding does not lower it (the engine handles the penalty; the label makes the
  meaning explicit in the UI).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .audit import (
    DISCLAIMER,
    AuditReport,
    LocationAudit,
    audit_profiles,
    load_profiles,
    severity_rank,
)

__all__ = [
    "BANNER",
    "CATEGORY_FIELDS",
    "DATASETS",
    "DATASET_NAMES",
    "DEFAULT_DATASET",
    "DemoError",
    "PreviewError",
    "SERVICE_FIELDS",
    "SCORE_LABEL",
    "build_preview",
    "hostile_fixture",
]

#: The exact wording the UI must show next to the score. It is a heuristic
#: COMPLETENESS score: it measures how complete the profile fields are, and it
#: says nothing about Google rankings, visibility or competitor position.
SCORE_LABEL = "completeness score (heuristic)"

#: Shown at the top of the preview. The dashboard has no live Google connection.
BANNER = (
    "DEMO / OFFLINE PREVIEW — no live Google connectivity. Synthetic fixture "
    "data only: nothing is read from Google, nothing is written to Google, and "
    "no profile is changed automatically."
)

#: Findings about categories (kept visually distinct from services).
CATEGORY_FIELDS = ("primary_category", "secondary_categories")

#: Findings about the service list.
SERVICE_FIELDS = ("services",)

DEFAULT_DATASET = "default"


class PreviewError(ValueError):
    """The requested fixture dataset does not exist (a caller error -> 400)."""


class DemoError(RuntimeError):
    """A *deliberate*, documented failure used to exercise the error state.

    ``build_preview(dataset="error")`` raises this so the dashboard can show a
    real error state deterministically, without any network fault being faked.
    """


# --------------------------------------------------------------------------
# fixture datasets (synthetic, deterministic, in-repo)
# --------------------------------------------------------------------------
def _incomplete_fixture() -> Dict[str, Any]:
    """A deliberately sparse synthetic shop: several required fields missing.

    Used to demonstrate the 'missing required fields' display.
    """
    return {
        "location_name": "accounts/4242/locations/fx-incomplete",
        "title": "Fixture Nails — Incomplete",
        "primary_category": "Nail Salon",
        # No city/state/postal_code/phone/website/hours/description/services:
        # every one of those is a required field -> high-severity findings.
        "_fixture": True,
    }


def hostile_fixture() -> List[Dict[str, Any]]:
    """A synthetic profile whose text contains markup, to prove escaping.

    Every value is inert fixture text. The dashboard must display it as *text*:
    the ``<`` / ``>`` characters have to survive as characters, never become
    markup. This fixture exists so a test can assert the renderer escapes. The
    markup is placed in the fields the preview actually surfaces (name, ZIP,
    city, resource id), so it reaches the client verbatim.
    """
    return [{
        "location_name": "accounts/4242/locations/fx-<script>alert(3)</script>",
        "title": "Fixture <script>alert(1)</script> Nails",
        "city": "<img src=x onerror=alert(2)> Grove",
        "state": "CA", "postal_code": "92841",
        "primary_category": "Nail Salon",
        "secondary_categories": ["Manicure", "Pedicure"],
        "services": ["Manicure", "Pedicure", "Gel Nails"],
        "description": ("Fixture text with markup: <b>bold</b> and entities that "
                        "must render literally. " * 6),
        "hours": {day: {"open": "09:00", "close": "19:00"}
                  for day in ("monday", "tuesday", "wednesday", "thursday",
                              "friday", "saturday", "sunday")},
        "phone": "+1 714-555-0000",
        "website": "https://example.invalid/hostile",
        "_fixture": True,
    }]


#: The deterministic datasets the preview can render, with a human label. Names
#: are stable strings used by ``GET /api/seo/audit?dataset=<name>`` and the UI's
#: dataset picker.
DATASETS: Dict[str, str] = {
    # The engine's own embedded synthetic demo account (one fairly complete
    # shop plus one incomplete shop that reuses the first shop's name under a
    # different ZIP -> exercises the reused-name identity note).
    "default": "Default synthetic demo account",
    # A single sparse shop -> exercises the missing-fields display.
    "incomplete": "Sparse shop (missing required fields)",
    # Markup in every text field -> exercises escaping.
    "hostile": "Markup-in-text shop (escaping check)",
    # No locations at all -> exercises the empty state.
    "empty": "No salons (empty state)",
    # A deliberate, documented failure -> exercises the error state.
    "error": "Deliberate demo failure (error state)",
}

#: Sorted dataset names (stable order for the UI picker and tests).
DATASET_NAMES: List[str] = sorted(DATASETS)


def _profiles_for(dataset: str) -> List[Dict[str, Any]]:
    """Resolve a dataset name to a list of synthetic profiles (no I/O)."""
    if dataset not in DATASETS:
        raise PreviewError(
            f"unknown dataset {dataset!r}; expected one of {DATASET_NAMES}")
    if dataset == "error":
        raise DemoError(
            "deliberate demo failure (dataset='error'): the offline preview "
            "error state is being shown on purpose.")
    if dataset == "default":
        # The engine's embedded synthetic fixtures (deep-copied by load_profiles).
        return list(load_profiles())
    if dataset == "empty":
        return []
    if dataset == "incomplete":
        return [_incomplete_fixture()]
    if dataset == "hostile":
        return hostile_fixture()
    raise PreviewError(f"dataset {dataset!r} has no fixture wired up")


# --------------------------------------------------------------------------
# view model
# --------------------------------------------------------------------------
def identity_label(location_name: str, display_name: str,
                   postal_code: str) -> str:
    """``name · ZIP · resource id`` — the shared dashboard disambiguation key.

    Two salons can share a name; the ZIP (and the resource id) is what tells
    them apart, exactly as ``docs/DOMAIN-RULES.md`` §2.3 requires.
    """
    return "{name} · {zip} · {resource}".format(
        name=display_name or "(untitled fixture)",
        zip=postal_code or "(no ZIP)",
        resource=location_name or "(no resource id)",
    )


def _salon_view(loc: LocationAudit) -> Dict[str, Any]:
    """One salon card: score + labelled findings, categories split from services."""
    findings = [f.as_dict() for f in loc.actions]          # already prioritised
    categories = [f for f in findings if f["field"] in CATEGORY_FIELDS]
    services = [f for f in findings if f["field"] in SERVICE_FIELDS]
    other = [f for f in findings
             if f["field"] not in CATEGORY_FIELDS
             and f["field"] not in SERVICE_FIELDS]
    missing = sorted(f["field"] for f in findings
                     if f["code"] == "SEO-MISSING-FIELD")
    return {
        "location_name": loc.location_name,
        "display_name": loc.title,
        "city": loc.city,
        "state": loc.state,
        "postal_code": loc.postal_code,
        "identity_label": identity_label(loc.location_name, loc.title,
                                         loc.postal_code),
        "score": loc.score,
        "score_label": SCORE_LABEL,
        "highest_severity": loc.highest_severity,
        "finding_count": len(findings),
        "missing_fields": missing,
        "category_findings": categories,
        "service_findings": services,
        "other_findings": other,
        "findings": findings,
    }


def _report_view(report: AuditReport, dataset: str) -> Dict[str, Any]:
    salons = [_salon_view(loc) for loc in report.locations]
    # Deterministic order: by (name, ZIP), then resource id. Same rule the
    # engine uses in ``AuditReport.as_dict``.
    salons.sort(key=lambda s: (s["display_name"].strip().lower(),
                               s["postal_code"].strip(),
                               s["location_name"]))
    base = report.as_dict()                                # engine truth
    return {
        "fixture_only": base["fixture_only"],
        "source": base["source"],
        "mode": "offline-demo",
        "dataset": dataset,
        "banner": BANNER,
        "score_label": SCORE_LABEL,
        "disclaimer": base["disclaimer"],
        "location_count": base["location_count"],
        "total_findings": base["total_findings"],
        "duplicate_names": base["duplicate_names"],
        "skipped": base["skipped"],
        "category_fields": list(CATEGORY_FIELDS),
        "service_fields": list(SERVICE_FIELDS),
        "salons": salons,
    }


def build_preview(dataset: str = DEFAULT_DATASET,
                  profiles: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """Build the deterministic preview view model for one fixture dataset.

    ``profiles`` (tests only) bypasses the dataset lookup and audits exactly the
    profiles handed in. No network, no writes, no clock, no randomness.
    """
    if profiles is None:
        resolved = _profiles_for(dataset)
    else:
        resolved = list(profiles)
    report = audit_profiles(resolved)
    return _report_view(report, dataset if profiles is None else "custom")


def severity_sort_key(finding: Dict[str, Any]) -> int:
    """Expose the engine's severity ordering so callers can re-check priority."""
    return severity_rank(finding.get("severity", ""))
