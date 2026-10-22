"""Deterministic, offline competitor-gap analysis (issue #88).

Why this module exists
----------------------
The #39/#86 audit engine (:mod:`gbp.seo.audit`) answers "what is wrong with
*this* profile?". This module answers the neighbouring question: "where does the
target profile's **coverage** lag the profiles it is actually compared with?" --
services, secondary categories, key-field presence and the distribution of the
heuristic completeness score across the comparison set.

It is deliberately built *on top of* the audit engine: every completeness score
comes from ``audit_location`` and no scoring rule is re-implemented here. What
this module adds is set arithmetic (union across the direct set, competitor count
and share per item), a deterministic gap list, benchmarks and evidence-quoting
recommendations.

Offline and honest by construction
----------------------------------
* **No network.** Synthetic fixtures only. No socket, no scraping, no Google
  call, no credential, no write. There is no live competitor data source: the
  "competitors" are fixture profiles in the same schema as the audited profiles.
* **Never ranks competitors.** Nothing here reports a competitor *position* or
  claims a ranking/visibility outcome. The score it reuses is a heuristic
  **completeness** score and is labelled as such everywhere it appears.
* **Never invents.** A malformed entry is reported in ``skipped`` (or ``notes``
  when only one field is malformed) with a reason; it is never coerced into a
  plausible-looking profile, and no service or category is invented.
* **Deterministic.** Everything is sorted; no clock, no randomness, no set
  iteration order. Same input in -> byte-identical report out (``to_json``).

Definitions this module commits to
----------------------------------
* **Direct set**: competitors whose *normalized* primary category equals the
  target's normalized primary category. Anyone else is excluded, with a reason.
* **Self**: a competitor carrying the target's own resource id
  (``location_name``) is excluded as self. Two *blank* resource ids are not an
  identity, so blank ids never trigger the self rule.
* **Share**: ``competitor_count / len(direct set)`` as a percentage rounded to
  the nearest integer (Python ``round``, half to even). The raw count is always
  reported next to it, so no precision is lost.
* **Small-N guard**: fewer than :data:`SMALL_N_MINIMUM` (3) direct competitors
  yields an explicit ``insufficient-competitor-data`` result carrying **no**
  percentages, **no** coverage tables, **no** benchmarks, **no** field-presence
  rates and **no** recommendations -- only the resolved set and the reason.
"""

from __future__ import annotations

import copy
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# Reused from the audit engine on purpose: completeness scores MUST come from
# ``audit_location`` (never re-implemented), and field presence is judged with
# the very same normalization/time helpers the score uses, so the two can never
# drift apart.
from .audit import (
    _DESCRIPTION_MIN,
    _WEEKDAYS,
    _is_blank,
    _minutes,
    _norm,
    audit_location,
)

__all__ = [
    "BENCHMARK_LABEL",
    "DEFAULT_COMPETITORS",
    "DEFAULT_TARGET",
    "FIELD_PRESENCE_FIELDS",
    "GAP_DISCLAIMER",
    "MODE",
    "SCORE_LABEL",
    "SMALL_N_MINIMUM",
    "STATUS_INSUFFICIENT",
    "STATUS_OK",
    "GapReport",
    "analyze_gap",
    "load_gap_fixture",
]

#: Emitted on every gap report. Fixtures only -- no live data, no ranking.
GAP_DISCLAIMER = (
    "Offline competitor-gap analysis of synthetic Google Business Profile "
    "fixtures. No live Google connectivity, no scraping, no real competitor "
    "data, no ranking dataset, and no ranking or visibility improvement is "
    "claimed or implied. Every share is a count over a synthetic fixture set "
    "and no competitor is ranked."
)

#: The only label allowed next to any score this module reports.
SCORE_LABEL = "heuristic completeness score (not a ranking score)"
#: Label for the benchmark block.
BENCHMARK_LABEL = "heuristic completeness"

MODE = "offline-competitor-gap"

#: Minimum number of valid *direct* competitors before any statistic is emitted.
SMALL_N_MINIMUM = 3

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient-competitor-data"

#: The key fields whose presence rate is benchmarked against the direct set.
#: ``(report key, human label)``. ``full_week_hours`` and
#: ``description_over_250`` mirror the audit engine's own notion of a usable
#: hours mapping and of a non-truncated description.
FIELD_PRESENCE_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("website", "a website field"),
    ("phone", "a phone number"),
    ("full_week_hours", "opening hours for all seven days"),
    ("description_over_250",
     f"a description of at least {_DESCRIPTION_MIN} characters"),
)


# --------------------------------------------------------------------------
# small deterministic helpers
# --------------------------------------------------------------------------
def _lower(value: Any) -> str:
    return _norm(value).lower()


def _list_field(profile: Dict[str, Any], name: str) -> Tuple[List[str], bool]:
    """Read a list-valued fixture field tolerantly.

    Returns ``(items, malformed)``: ``malformed`` is True when the field holds
    something that is not a list/tuple/string, in which case it contributes no
    coverage (and the caller records a reason -- nothing is invented).
    """
    value = profile.get(name)
    if value is None:
        return [], False
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return [], True
    return [text for text in (_norm(item) for item in value) if text], False


def _display_label(spellings: Dict[str, int]) -> str:
    """Pick the display spelling for a normalized key, deterministically.

    Most frequent raw spelling wins; ties fall back to the lexicographically
    smallest spelling, so the label never depends on iteration order.
    """
    return sorted(spellings.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _percent(count: int, total: int) -> int:
    """Rounded percentage (integer) of ``count`` out of ``total``."""
    if total <= 0:
        return 0
    return int(round(100 * count / total))


def _median(values: Sequence[int]) -> Optional[Union[int, float]]:
    """Deterministic median; returns an int when the median is whole."""
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    value = (ordered[middle - 1] + ordered[middle]) / 2
    return int(value) if float(value).is_integer() else value


def _has_full_week_hours(profile: Dict[str, Any]) -> bool:
    """True when hours cover all seven days with valid, ordered ``HH:MM``."""
    hours = profile.get("hours")
    if not isinstance(hours, dict) or not hours:
        return False
    for day in _WEEKDAYS:
        value = hours.get(day)
        if not isinstance(value, dict):
            return False
        start = _minutes(str(value.get("open") or ""))
        end = _minutes(str(value.get("close") or ""))
        if start is None or end is None or start >= end:
            return False
    return True


def _has_long_description(profile: Dict[str, Any]) -> bool:
    raw = profile.get("description")
    return raw is not None and len(str(raw).strip()) >= _DESCRIPTION_MIN


_PRESENCE: Dict[str, Callable[[Dict[str, Any]], bool]] = {
    "website": lambda p: not _is_blank(p.get("website")),
    "phone": lambda p: not _is_blank(p.get("phone")),
    "full_week_hours": _has_full_week_hours,
    "description_over_250": _has_long_description,
}


def _coverage(profiles: Sequence[Dict[str, Any]], field_name: str,
              target_items: Sequence[str]) -> Tuple[List[Dict[str, Any]],
                                                      List[Dict[str, Any]]]:
    """Coverage table for one list field across the direct set.

    The table is the union of the direct set with the target's own items, so an
    item only the target lists is still visible (competitor count 0) without
    ever becoming a gap. Returns ``(items, gaps)``. Each item is
    ``{name, key, competitor_count, competitor_share, target_lists}``; ``gaps``
    is the subset the target does not list. Both are sorted by competitor share
    (equivalently: count) descending, then name ascending -- deterministic.
    """
    counts: Dict[str, int] = {}
    spellings: Dict[str, Dict[str, int]] = {}
    for profile in profiles:
        items, _malformed = _list_field(profile, field_name)
        seen: List[str] = []
        for raw in items:
            key = _lower(raw)
            if key not in seen:
                seen.append(key)
            bucket = spellings.setdefault(key, {})
            bucket[raw] = bucket.get(raw, 0) + 1
        for key in seen:  # one competitor counts once per item
            counts[key] = counts.get(key, 0) + 1

    target_keys: Dict[str, Dict[str, int]] = {}
    for raw in target_items:
        key = _lower(raw)
        bucket = target_keys.setdefault(key, {})
        bucket[raw] = bucket.get(raw, 0) + 1

    total = len(profiles)
    table: List[Dict[str, Any]] = []
    for key in sorted(counts):
        table.append({
            "name": _display_label(spellings[key]),
            "key": key,
            "competitor_count": counts[key],
            "competitor_share": _percent(counts[key], total),
            "target_lists": key in target_keys,
        })
    for key in sorted(target_keys):
        if key in counts:
            continue
        table.append({
            "name": _display_label(target_keys[key]),
            "key": key,
            "competitor_count": 0,
            "competitor_share": 0,
            "target_lists": True,
        })
    table.sort(key=lambda item: (-item["competitor_count"], item["name"]))
    gaps = [item for item in table if not item["target_lists"]]
    return table, gaps


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------
@dataclass
class GapReport:
    """The deterministic result of one target-vs-competitors gap analysis."""

    target_location_name: str = ""
    target_title: str = ""
    target_primary_category: str = ""
    target_score: int = 0
    direct_set: List[Dict[str, Any]] = field(default_factory=list)
    excluded: List[Dict[str, str]] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    notes: List[Dict[str, str]] = field(default_factory=list)
    insufficient: bool = False
    reason: str = ""
    services: Optional[Dict[str, Any]] = None
    secondary_categories: Optional[Dict[str, Any]] = None
    benchmarks: Optional[Dict[str, Any]] = None
    field_presence: List[Dict[str, Any]] = field(default_factory=list)
    recommendations: List[Dict[str, str]] = field(default_factory=list)

    @property
    def direct_competitor_count(self) -> int:
        return len(self.direct_set)

    @property
    def status(self) -> str:
        return STATUS_INSUFFICIENT if self.insufficient else STATUS_OK

    def as_dict(self) -> Dict[str, Any]:
        return {
            "fixture_only": True,
            "source": "synthetic-fixture",
            "mode": MODE,
            "disclaimer": GAP_DISCLAIMER,
            "score_label": SCORE_LABEL,
            "benchmark_label": BENCHMARK_LABEL,
            "status": self.status,
            "insufficient_data": self.insufficient,
            "reason": self.reason,
            "small_n_minimum": SMALL_N_MINIMUM,
            "target": {
                "location_name": self.target_location_name,
                "display_name": self.target_title,
                "primary_category": self.target_primary_category,
                "score": self.target_score,
                "score_label": SCORE_LABEL,
            },
            "direct_competitor_count": self.direct_competitor_count,
            "direct_set": sorted(
                self.direct_set,
                key=lambda d: (d["display_name"].strip().lower(),
                               d["location_name"])),
            "excluded": _sorted_entries(self.excluded),
            "skipped": _sorted_entries(self.skipped),
            "notes": _sorted_entries(self.notes),
            "services": self.services,
            "secondary_categories": self.secondary_categories,
            "benchmarks": self.benchmarks,
            "field_presence": list(self.field_presence),
            "recommendations": list(self.recommendations),
        }

    def to_json(self) -> str:
        """Canonical JSON (sorted keys) so two runs are byte-identical."""
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


def _sorted_entries(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort reason-carrying entries deterministically (reason, then index)."""

    def key(entry: Dict[str, Any]) -> Tuple[str, int]:
        try:
            index = int(entry.get("index", "0"))
        except (TypeError, ValueError):
            index = 0
        return (str(entry.get("reason", "")), index)

    return sorted(entries, key=key)


def _competitor_view(profile: Dict[str, Any]) -> Dict[str, Any]:
    """A direct-set entry: only what the analysis reports, no invented fields."""
    return {
        "location_name": _norm(profile.get("location_name")),
        "display_name": _norm(profile.get("title")),
        "city": _norm(profile.get("city")),
        "postal_code": _norm(profile.get("postal_code")),
        "primary_category": _norm(profile.get("primary_category")),
        "score": audit_location(profile).score,
        "score_label": SCORE_LABEL,
    }


# --------------------------------------------------------------------------
# recommendations (only from the tables above)
# --------------------------------------------------------------------------
def _service_recommendations(gaps: Sequence[Dict[str, Any]], total: int
                             ) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for gap in gaps:
        count, share = gap["competitor_count"], gap["competitor_share"]
        out.append({
            "kind": "service",
            "subject": gap["name"],
            "evidence": (f"{count} of {total} direct competitors list "
                         f"{gap['name']!r} ({share}%)"),
            "text": (f"If the salon offers {gap['name']}, consider adding it to "
                     f"the services list: {count} of {total} direct competitors "
                     f"list it ({share}%)."),
        })
    return out


def _category_recommendations(gaps: Sequence[Dict[str, Any]], total: int
                              ) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for gap in gaps:
        count, share = gap["competitor_count"], gap["competitor_share"]
        out.append({
            "kind": "secondary_category",
            "subject": gap["name"],
            "evidence": (f"{count} of {total} direct competitors carry "
                         f"{gap['name']!r} as a secondary category ({share}%)"),
            "text": (f"If the salon offers {gap['name']}, consider adding it as "
                     f"a secondary category: {count} of {total} direct "
                     f"competitors carry it ({share}%)."),
        })
    return out


def _field_recommendations(presence: Sequence[Dict[str, Any]], total: int
                           ) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for row in presence:
        if row["target_present"]:
            continue
        out.append({
            "kind": "field",
            "subject": row["field"],
            "evidence": (f"{row['present_count']} of {total} direct competitors "
                         f"have {row['label']} "
                         f"({row['competitor_share']}%); the target does not"),
            "text": (f"If the salon can add {row['label']}, consider doing so: "
                     f"{row['present_count']} of {total} direct competitors "
                     f"have it ({row['competitor_share']}%)."),
        })
    return out


def _completeness_recommendation(benchmarks: Dict[str, Any]
                                 ) -> List[Dict[str, str]]:
    """Only emitted when the target scores below the direct-set median."""
    if not benchmarks.get("target_below_median"):
        return []
    return [{
        "kind": "completeness",
        "subject": "completeness",
        "evidence": (f"target {benchmarks['target']} vs direct-set median "
                     f"{benchmarks['median']} across "
                     f"{benchmarks['direct_competitors']} direct competitors"),
        "text": ("The target's heuristic completeness score "
                 f"({benchmarks['target']}) is below the direct-set median "
                 f"({benchmarks['median']}) across "
                 f"{benchmarks['direct_competitors']} direct competitors; "
                 "consider working through this profile's audit findings. "
                 "Completeness only -- this is not a position or outcome claim."),
    }]


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def analyze_gap(target: Dict[str, Any],
                competitors: Sequence[Any]) -> GapReport:
    """Compare one target profile against competitor profiles (fixtures only).

    ``target`` and every competitor use the existing audit fixture schema. The
    function never mutates its inputs (the audit engine is pure) and never
    raises on a malformed *competitor*: non-mappings land in ``skipped`` and
    category mismatches in ``excluded``, each with a reason. A non-mapping
    ``target`` is a caller error and raises ``ValueError``.
    """
    if not isinstance(target, dict):
        raise ValueError(
            f"target profile must be a mapping, got {type(target).__name__}")

    target_audit = audit_location(target)
    target_primary = _lower(target.get("primary_category"))
    target_id = _norm(target.get("location_name"))

    direct_profiles: List[Dict[str, Any]] = []
    direct_set: List[Dict[str, Any]] = []
    excluded: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    notes: List[Dict[str, str]] = []

    for index, competitor in enumerate(competitors):
        if not isinstance(competitor, dict):
            skipped.append({
                "index": str(index),
                "reason": f"not a mapping ({type(competitor).__name__})",
            })
            continue
        competitor_id = _norm(competitor.get("location_name"))
        if target_id and competitor_id and competitor_id == target_id:
            skipped.append({
                "index": str(index),
                "location_name": competitor_id,
                "reason": "same resource id as the target profile (self)",
            })
            continue

        competitor_primary = _lower(competitor.get("primary_category"))
        entry = {
            "index": str(index),
            "location_name": competitor_id,
            "primary_category": _norm(competitor.get("primary_category")),
        }
        if not target_primary:
            excluded.append(dict(entry, reason=(
                "the target profile has no primary category, so no direct "
                "comparison set can be built")))
            continue
        if not competitor_primary:
            excluded.append(dict(entry, reason="competitor has no primary category"))
            continue
        if competitor_primary != target_primary:
            excluded.append(dict(entry, reason=(
                f"primary category {_norm(competitor.get('primary_category'))!r} "
                f"differs from the target primary category "
                f"{_norm(target.get('primary_category'))!r}")))
            continue

        for field_name in ("services", "secondary_categories"):
            _items, malformed = _list_field(competitor, field_name)
            if malformed:
                notes.append({
                    "index": str(index),
                    "location_name": competitor_id,
                    "field": field_name,
                    "reason": (f"{field_name} is not a list, so this competitor "
                               "contributes no coverage for it"),
                })
        direct_profiles.append(competitor)
        direct_set.append(_competitor_view(competitor))

    direct_count = len(direct_profiles)
    report = GapReport(
        target_location_name=target_id,
        target_title=_norm(target.get("title")),
        target_primary_category=_norm(target.get("primary_category")),
        target_score=target_audit.score,
        direct_set=direct_set,
        excluded=excluded,
        skipped=skipped,
        notes=notes,
    )

    # ---- small-N guard: no percentages, no tables, no recommendations ----
    if not target_primary:
        report.insufficient = True
        report.reason = (
            "the target profile has no primary category, so no direct "
            "comparison set exists; no statistics are reported")
        return report
    if direct_count < SMALL_N_MINIMUM:
        report.insufficient = True
        report.reason = (
            f"only {direct_count} direct competitor(s) share the target "
            f"primary category {_norm(target.get('primary_category'))!r}; at "
            f"least {SMALL_N_MINIMUM} are required before competitor shares "
            "are reported")
        return report

    target_services, target_services_malformed = _list_field(target, "services")
    target_secondary, target_secondary_malformed = _list_field(
        target, "secondary_categories")
    for field_name, malformed in (("services", target_services_malformed),
                                  ("secondary_categories",
                                   target_secondary_malformed)):
        if malformed:
            notes.append({
                "index": "target",
                "location_name": target_id,
                "field": field_name,
                "reason": (f"target {field_name} is not a list, so the gap list "
                           "treats the target as listing nothing for it"),
            })

    service_items, service_gaps = _coverage(
        direct_profiles, "services", target_services)
    category_items, category_gaps = _coverage(
        direct_profiles, "secondary_categories", target_secondary)

    report.services = {
        "field": "services",
        "total_direct": direct_count,
        "target_count": len({_lower(s) for s in target_services}),
        "items": service_items,
        "gaps": service_gaps,
    }
    report.secondary_categories = {
        "field": "secondary_categories",
        "total_direct": direct_count,
        "target_count": len({_lower(s) for s in target_secondary}),
        "items": category_items,
        "gaps": category_gaps,
    }

    scores = [entry["score"] for entry in direct_set]
    median = _median(scores)
    benchmarks: Dict[str, Any] = {
        "label": BENCHMARK_LABEL,
        "target": report.target_score,
        "median": median,
        "min": min(scores),
        "max": max(scores),
        "direct_competitors": direct_count,
        "target_below_median": (median is not None
                                and report.target_score < median),
        "note": ("heuristic completeness only: this measures profile field "
                 "completeness, not a ranking position, and it ranks nobody"),
    }
    report.benchmarks = benchmarks

    presence: List[Dict[str, Any]] = []
    for key, label in FIELD_PRESENCE_FIELDS:
        check = _PRESENCE[key]
        present = sum(1 for profile in direct_profiles if check(profile))
        presence.append({
            "field": key,
            "label": label,
            "present_count": present,
            "competitor_share": _percent(present, direct_count),
            "target_present": bool(check(target)),
        })
    report.field_presence = presence

    report.recommendations = (
        _service_recommendations(service_gaps, direct_count)
        + _category_recommendations(category_gaps, direct_count)
        + _field_recommendations(presence, direct_count)
        + _completeness_recommendation(benchmarks)
    )
    return report


# --------------------------------------------------------------------------
# synthetic fixtures (deterministic, offline)
# --------------------------------------------------------------------------
def _full_hours() -> Dict[str, Dict[str, str]]:
    """9am-7pm every day, as a fresh mapping (deterministic fixture helper)."""
    return {day: {"open": "09:00", "close": "19:00"} for day in _WEEKDAYS}


_TARGET_DESCRIPTION = (
    "Demo Nails in Garden Grove is a warm, hygienic nail studio for everyday "
    "care and special occasions. Our technicians cover manicures, pedicures, "
    "gel nails and careful nail health, working with walk-ins and appointments "
    "all week. We shape, buff and finish every set by hand and we keep our "
    "tools sterilised between clients. Stop by for a quick refresh or settle "
    "in for a full afternoon of care with the team."
)

_COMPETITOR_DESCRIPTION = (
    "Sunny Nails is a neighbourhood studio offering manicures, pedicures, gel "
    "and acrylic nails, dip powder and simple nail art. We take walk-ins most "
    "days, keep prices posted in store and use single-use files for every "
    "client. Our team is happy to talk through shape, colour and aftercare "
    "before you sit down, and we work carefully around sensitive skin."
)

_SHORT_COMPETITOR_DESCRIPTION = (
    "Luxe Nails: manicures, dip powder, shellac and nail art in a calm studio."
)

#: A tiny, fully synthetic target profile (same schema as the audit fixtures).
DEFAULT_TARGET: Dict[str, Any] = {
    "location_name": "accounts/1001/locations/loc-001",
    "title": "Demo Nails Garden Grove",
    "city": "Garden Grove", "state": "CA", "postal_code": "92841",
    "primary_category": "Nail Salon",
    "secondary_categories": ["Manicure", "Pedicure"],
    "services": ["Manicure", "Pedicure", "Gel Nails"],
    "description": _TARGET_DESCRIPTION,
    "hours": _full_hours(),
    "phone": "+1 714-555-0142",
    "website": "https://example.invalid/demo-nails",
    "_fixture": True,
}

#: Synthetic competitor profiles: three direct competitors (same primary
#: category), one competitor in another category (excluded), one entry that
#: reuses the target's resource id (excluded as self) and one non-mapping entry
#: (skipped). Every value is invented fixture text.
DEFAULT_COMPETITORS: List[Any] = [
    {
        "location_name": "accounts/2002/locations/cmp-101",
        "title": "Sunny Nails",
        "city": "Garden Grove", "state": "CA", "postal_code": "92843",
        "primary_category": "Nail Salon",
        "secondary_categories": ["Manicure", "Pedicure", "Nail Art"],
        "services": ["Manicure", "Pedicure", "Gel Nails", "Dip Powder",
                     "Acrylic Nails"],
        "description": _COMPETITOR_DESCRIPTION,
        "hours": _full_hours(),
        "phone": "+1 714-555-0201",
        "website": "https://example.invalid/sunny-nails",
        "_fixture": True,
    },
    {
        "location_name": "accounts/2002/locations/cmp-102",
        "title": "Luxe Nails",
        "city": "Westminster", "state": "CA", "postal_code": "92683",
        "primary_category": "Nail Salon",
        "secondary_categories": ["Manicure", "Pedicure", "Waxing"],
        "services": ["Manicure", "Dip Powder", "Nail Art", "Shellac"],
        "description": _SHORT_COMPETITOR_DESCRIPTION,
        "hours": _full_hours(),
        "phone": "+1 714-555-0202",
        # no website on purpose: exercises the field-presence benchmark
        "_fixture": True,
    },
    {
        "location_name": "accounts/2002/locations/cmp-103",
        "title": "Bloom Nails",
        "city": "Fountain Valley", "state": "CA", "postal_code": "92708",
        "primary_category": "Nail Salon",
        "secondary_categories": ["Manicure", "Pedicure", "Nail Art", "Waxing"],
        "services": ["Manicure", "Pedicure", "Gel Nails", "Dip Powder",
                     "Spa Pedicure"],
        "description": _COMPETITOR_DESCRIPTION,
        # weekday-only hours on purpose: not a full week
        "hours": {day: {"open": "10:00", "close": "18:00"}
                  for day in _WEEKDAYS[:5]},
        "phone": "+1 714-555-0203",
        "website": "https://example.invalid/bloom-nails",
        "_fixture": True,
    },
    {
        # different primary category -> excluded from the direct set
        "location_name": "accounts/2002/locations/cmp-104",
        "title": "Corner Barbers",
        "city": "Garden Grove", "state": "CA", "postal_code": "92841",
        "primary_category": "Barber Shop",
        "secondary_categories": ["Haircut"],
        "services": ["Haircut", "Beard Trim"],
        "description": "Corner Barbers is a synthetic fixture barber shop.",
        "hours": _full_hours(),
        "phone": "+1 714-555-0204",
        "website": "https://example.invalid/corner-barbers",
        "_fixture": True,
    },
    {
        # same resource id as the target -> excluded as self
        "location_name": "accounts/1001/locations/loc-001",
        "title": "Demo Nails Garden Grove",
        "city": "Garden Grove", "state": "CA", "postal_code": "92841",
        "primary_category": "Nail Salon",
        "secondary_categories": ["Manicure", "Pedicure"],
        "services": ["Manicure", "Pedicure", "Gel Nails"],
        "description": _TARGET_DESCRIPTION,
        "hours": _full_hours(),
        "phone": "+1 714-555-0142",
        "website": "https://example.invalid/demo-nails",
        "_fixture": True,
    },
    # a malformed entry: reported in ``skipped``, never invented
    None,
]


def load_gap_fixture(path: Optional[Union[str, pathlib.Path]] = None
                     ) -> Dict[str, Any]:
    """Return ``{"target": {...}, "competitors": [...]}`` synthetic fixtures.

    With no ``path`` the embedded :data:`DEFAULT_TARGET` /
    :data:`DEFAULT_COMPETITORS` are returned as a deep copy (so callers cannot
    mutate the module constants). With a ``path`` the JSON file is read from
    disk -- still offline, still no network. The file must hold an object with a
    ``target`` mapping and a ``competitors`` list.
    """
    if path is None:
        return {"target": copy.deepcopy(DEFAULT_TARGET),
                "competitors": copy.deepcopy(DEFAULT_COMPETITORS)}
    with pathlib.Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("gap fixture file must hold an object with 'target' "
                         "and 'competitors'")
    target = data.get("target")
    competitors = data.get("competitors")
    if not isinstance(target, dict):
        raise ValueError("gap fixture 'target' must be a mapping")
    if not isinstance(competitors, list):
        raise ValueError("gap fixture 'competitors' must be a list")
    return {"target": target, "competitors": list(competitors)}
