"""Offline, simulated-only ranking *measurement framework* (issue #90).

Why this module exists
----------------------
The GMap SEO stream can talk about profile completeness (#39/#86) and competitor
coverage (#88) but has no vocabulary for **position over time**. This module
supplies that vocabulary and nothing else: a data model for one rank
observation, a seeded deterministic **simulated** series generator, the metric
definitions the stream will use, and a per-location per-period summary.

Simulated only -- enforced, not merely documented
-------------------------------------------------
* **There is no live data source here.** No Google API, no Local Rank Grid in
  any form, no scraping, no network call, no credential, no persistence, no
  dashboard. The module imports neither ``socket`` nor ``urllib``.
* **Non-simulated data is refused.** :class:`RankObservation` raises
  ``ValueError`` for any ``source`` other than :data:`SIMULATED`, so an upstream
  scraper (or a grid) cannot smuggle a real observation into these metrics. The
  refusal is the module's contract, not a comment.
* **The simulated marker travels end-to-end.** Every observation serializes
  ``simulated: True`` / ``source: "simulated"``; every summary and report
  serializes the :data:`SIMULATED_LABEL` next to every number, and a summary
  cannot be constructed at all without an explicit ``simulated=True``.
* **Deterministic.** The generator derives its random stream from
  ``sha256(seed, location_id, keyword)`` and a fixed integer mixer, so a seed
  reproduces a byte-identical series on any interpreter (it does not depend on
  ``random``'s implementation) and the produced series does not depend on the
  order of the inputs. Everything that can be sorted is sorted.

Metric definitions this module commits to
-----------------------------------------
See :data:`METRIC_DEFINITIONS` for the machine-readable copy of this table.

* ``coverage``: share of observations with a non-null position
  (``non_null / total``, rounded to 3 decimals). ``None`` weeks are *known*
  "outside top 20" readings, so they count in the denominator.
* ``average_position``: arithmetic mean of the **non-null** positions only,
  rounded to 3 decimals; ``None`` when the period has no position at all. It
  never counts an "outside top 20" week as a number.
* ``best_position`` / ``worst_position``: smallest / largest non-null position;
  ``None`` when there is no position at all.
* ``top_3_share`` / ``top_10_share``: share of **all** observations in the
  period whose non-null position is ``<= 3`` / ``<= 10``, rounded to 3 decimals.
  Null weeks are in the denominator, so the shares cannot be inflated by
  dropping the weeks where the salon was invisible.
* ``week_over_week``: for each pair of **consecutive ISO weeks** present in the
  period, the delta ``average_position(later) - average_position(earlier)``.
  Negative means the average position number got smaller, i.e. the salon moved
  *up*. A week whose average is ``None`` (no position at all) yields a ``None``
  delta and is listed, never dropped silently.
* ``visibility_index``: an **internal** index, **not** a Google metric and not
  comparable to any Google-reported number. Definition: for one keyword, score
  each observation as ``max(0, 21 - position) / 20`` and score a null week as
  ``0``; average those scores over the keyword's observations in the period;
  the index is the mean of those per-keyword averages (so every keyword counts
  once regardless of how many weeks it has). Rounded to 4 decimals; ``None``
  when the period has no observations. ``21 - position`` is used so that
  position 1 scores ``1.0``, position 20 scores ``0.05`` and "outside top 20"
  scores ``0``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_KEYWORDS",
    "DEFAULT_LOCATIONS",
    "DEFAULT_SEED",
    "DEFAULT_WEEKS",
    "DISCLAIMER",
    "MAX_POSITION",
    "METRIC_DEFINITIONS",
    "MODE",
    "MULTI_LOCATION_NOTE",
    "SIMULATED",
    "SIMULATED_LABEL",
    "SOURCE_LABEL",
    "VISIBILITY_INDEX_LABEL",
    "LocationPeriodSummary",
    "RankObservation",
    "RankSeries",
    "RankingReport",
    "average_position",
    "best_position",
    "coverage",
    "iso_week_range",
    "next_week",
    "parse_week",
    "simulate_dataset",
    "simulate_keyword_series",
    "simulate_series",
    "summarize_location",
    "summarize_series",
    "top_share",
    "visibility_by_keyword",
    "visibility_index",
    "week_label",
    "week_over_week",
    "worst_position",
]

#: The only ``source`` this module accepts. Anything else is refused.
SIMULATED = "simulated"
#: The label that must appear next to any number this module reports.
SIMULATED_LABEL = "SIMULATED"
#: Human-readable name of the accepted data source.
SOURCE_LABEL = "simulated fixture (seeded generator, offline)"
#: The visibility index is ours, not Google's. Always reported with this label.
VISIBILITY_INDEX_LABEL = (
    "internal visibility index (simulated; not a Google metric, not a Google "
    "position and not comparable to any Google-reported value)"
)

MODE = "offline-simulated-ranking-measurement"

DISCLAIMER = (
    "Offline ranking MEASUREMENT FRAMEWORK. Every position in this report comes "
    "from the seeded, synthetic generator in this module: there is no live "
    "Google connectivity, no Local Rank Grid, no scraping, no rank checking and "
    "no real customer or competitor data. Positions and the internal visibility "
    "index are simulated measurements of a simulated dataset -- they are not "
    "Google metrics and they assert nothing about a real business."
)

MULTI_LOCATION_NOTE = (
    "Locations are identified by the existing multi-location convention "
    "(resource id; see docs/DOMAIN-RULES.md). One location is one series; the "
    "framework never merges two locations into one series."
)

#: A position is an integer in ``1..MAX_POSITION``; ``None`` means "outside the
#: top 20", never "unknown" and never a stand-in for a grid rank.
MAX_POSITION = 20

#: ``YYYY-Www`` (ISO week), e.g. ``2026-W10``. Validated but not checked against
#: the calendar (week 53 is accepted for any year; use :func:`iso_week_range`
#: when you need real consecutive ISO weeks).
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")

#: Machine-readable copy of the metric contract in the module docstring.
METRIC_DEFINITIONS: Dict[str, str] = {
    "coverage": (
        "share of observations in the period with a non-null position "
        "(non_null / total, 3 decimals); null weeks are known 'outside top 20' "
        "readings and stay in the denominator"
    ),
    "average_position": (
        "arithmetic mean of the non-null positions only (3 decimals); null when "
        "the period has no position at all -- an 'outside top 20' week is never "
        "counted as a number"
    ),
    "best_position": "smallest non-null position in the period (null if none)",
    "worst_position": "largest non-null position in the period (null if none)",
    "top_3_share": (
        "share of ALL observations in the period with a non-null position <= 3 "
        "(3 decimals); null weeks remain in the denominator"
    ),
    "top_10_share": (
        "share of ALL observations in the period with a non-null position <= 10 "
        "(3 decimals); null weeks remain in the denominator"
    ),
    "week_over_week": (
        "for each pair of consecutive ISO weeks present in the period: later "
        "average_position minus earlier average_position; negative means the "
        "average position number fell, i.e. the salon moved up; a week with no "
        "non-null position yields a null delta and is still listed"
    ),
    "visibility_index": (
        "INTERNAL index, not a Google metric: per keyword, score each "
        "observation max(0, 21 - position) / 20 and each null week 0, average "
        "over that keyword's observations, then average those keyword means "
        "(4 decimals); null when the period has no observations"
    ),
}


# --------------------------------------------------------------------------
# deterministic helpers
# --------------------------------------------------------------------------
def week_label(year: int, week: int) -> str:
    """Canonical ISO week label, e.g. ``week_label(2026, 10) == '2026-W10'``."""
    if not isinstance(year, int) or isinstance(year, bool):
        raise ValueError(f"week year must be an int, got {type(year).__name__}")
    if not isinstance(week, int) or isinstance(week, bool):
        raise ValueError(f"ISO week must be an int, got {type(week).__name__}")
    if not 1 <= week <= 53:
        raise ValueError(f"ISO week must be in 1..53, got {week!r}")
    return f"{year:04d}-W{week:02d}"


def parse_week(label: str) -> Tuple[int, int]:
    """Split a canonical ISO week label into ``(year, week)``.

    Raises ``ValueError`` on anything that is not ``YYYY-Www`` with a week in
    ``1..53``.
    """
    if not isinstance(label, str):
        raise ValueError(f"ISO week must be a string, got {type(label).__name__}")
    match = _WEEK_RE.match(label)
    if match is None:
        raise ValueError(
            f"ISO week must look like 'YYYY-Www' (e.g. '2026-W10'), got {label!r}")
    year, week = int(match.group(1)), int(match.group(2))
    if not 1 <= week <= 53:
        raise ValueError(f"ISO week number must be in 1..53, got {label!r}")
    return year, week


def next_week(label: str) -> str:
    """The ISO week after ``label``, correct across year boundaries."""
    year, week = parse_week(label)
    if week >= date(year, 12, 28).isocalendar()[1]:
        return week_label(year + 1, 1)
    return week_label(year, week + 1)


def iso_week_range(start: str, count: int) -> Tuple[str, ...]:
    """``count`` consecutive ISO weeks beginning at ``start`` (inclusive)."""
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError(f"week count must be a non-negative int, got {count!r}")
    weeks: List[str] = []
    current = parse_week(start)  # validate up front
    label = week_label(*current)
    for _ in range(count):
        weeks.append(label)
        label = next_week(label)
    return tuple(weeks)


def _seed_int(seed: Any, *parts: Any) -> int:
    """Derive a stable 64-bit integer from ``seed`` and ``parts``.

    Pure ``sha256`` of the joined material, so the stream is identical on every
    interpreter and independent of the order in which series are generated.
    """
    material = "\x1f".join([repr(seed)] + [str(part) for part in parts])
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


_MASK = (1 << 64) - 1


class _DeterministicRng:
    """A tiny SplitMix64 mixer: deterministic, version-independent, seedable.

    Used instead of the ``random`` module on purpose -- ``random``'s stream is
    an implementation detail of the interpreter, while this mixer is frozen by
    these constants, so "same seed -> byte-identical series" holds forever.
    """

    def __init__(self, seed_int: int) -> None:
        self._state = seed_int & _MASK

    def _next_u64(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & _MASK
        value = self._state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK
        return value ^ (value >> 31)

    def randbelow(self, stop: int) -> int:
        """Uniform integer in ``0..stop-1`` (``stop`` must be >= 1)."""
        if stop < 1:
            raise ValueError("stop must be >= 1")
        return self._next_u64() % stop


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RankObservation:
    """One simulated ranking observation.

    ``(location_id, keyword, week)`` is the key; ``position`` is the integer
    ``1..MAX_POSITION`` or ``None`` for "outside the top 20". ``source`` must be
    :data:`SIMULATED` -- this module actively refuses anything else.
    """

    location_id: str
    keyword: str
    week: str
    position: Optional[int]
    source: str = SIMULATED

    # -- validation ------------------------------------------------------
    def __post_init__(self) -> None:
        # Source is checked first and *without* normalization: an upstream
        # scraper must not be able to pass by spelling "scraped" with spaces or
        # capitals, and no other spelling is silently accepted.
        if self.source != SIMULATED:
            raise ValueError(
                f"ranking observations must be {SIMULATED!r}; refused source "
                f"{self.source!r}. This module measures simulated data only -- "
                "no scraping, no rank checking, no Local Rank Grid, no live "
                "data of any kind.")

        location_id = self.location_id
        keyword = self.keyword
        if not isinstance(location_id, str) or not location_id.strip():
            raise ValueError("location_id must be a non-empty string")
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError("keyword must be a non-empty string")
        object.__setattr__(self, "location_id", location_id.strip())
        object.__setattr__(self, "keyword", keyword.strip())

        parse_week(self.week)  # raises ValueError on a malformed label

        position = self.position
        if position is None:
            return
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValueError(
                "position must be an int in 1.."
                f"{MAX_POSITION} or None (outside top 20), got "
                f"{position!r} ({type(position).__name__})")
        if not 1 <= position <= MAX_POSITION:
            raise ValueError(
                f"position must be in 1..{MAX_POSITION} or None (outside top "
                f"20), got {position!r}")

    # -- derived ---------------------------------------------------------
    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.location_id, self.keyword, self.week)

    @property
    def in_top_20(self) -> bool:
        return self.position is not None

    def as_dict(self) -> Dict[str, Any]:
        """Deterministic mapping, carrying the simulated marker."""
        return {
            "simulated": True,
            "source": self.source,
            "source_label": SOURCE_LABEL,
            "location_id": self.location_id,
            "keyword": self.keyword,
            "week": self.week,
            "position": self.position,
            "in_top_20": self.in_top_20,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


class RankSeries:
    """An ordered set of :class:`RankObservation`, duplicate-key safe.

    Adding a second observation for an existing
    ``(location_id, keyword, week)`` raises ``ValueError``: two readings of the
    same cell in the same ISO week cannot both be true, so the framework refuses
    to hold them rather than silently overwrite.
    """

    def __init__(self, observations: Iterable[RankObservation] = ()) -> None:
        self._items: List[RankObservation] = []
        self._index: Dict[Tuple[str, str, str], RankObservation] = {}
        for observation in observations:
            self.add(observation)

    def add(self, observation: RankObservation) -> RankObservation:
        if not isinstance(observation, RankObservation):
            raise ValueError(
                "a rank series holds RankObservation values only, got "
                f"{type(observation).__name__}")
        if observation.key in self._index:
            raise ValueError(
                "duplicate (location_id, keyword, week): "
                f"{observation.key!r} is already in this series")
        self._index[observation.key] = observation
        self._items.append(observation)
        return observation

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, observation: object) -> bool:
        return isinstance(observation, RankObservation) and \
            observation.key in self._index

    @property
    def observations(self) -> Tuple[RankObservation, ...]:
        """The observations in deterministic order (location, keyword, week)."""
        return tuple(sorted(self._items, key=lambda obs: obs.key))

    def select(self, location_id: Optional[str] = None,
               keyword: Optional[str] = None,
               week: Optional[str] = None) -> Tuple[RankObservation, ...]:
        """Filter by any combination of the key parts, deterministic order."""
        return tuple(obs for obs in self.observations
                     if (location_id is None or obs.location_id == location_id)
                     and (keyword is None or obs.keyword == keyword)
                     and (week is None or obs.week == week))

    def locations(self) -> Tuple[str, ...]:
        return tuple(sorted({obs.location_id for obs in self._items}))

    def keywords(self) -> Tuple[str, ...]:
        return tuple(sorted({obs.keyword for obs in self._items}))

    def weeks(self) -> Tuple[str, ...]:
        return tuple(sorted({obs.week for obs in self._items}))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "simulated": True,
            "source": SIMULATED,
            "source_label": SOURCE_LABEL,
            "observation_count": len(self._items),
            "locations": list(self.locations()),
            "keywords": list(self.keywords()),
            "weeks": list(self.weeks()),
            "observations": [obs.as_dict() for obs in self.observations],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# seeded, deterministic simulated generator
# --------------------------------------------------------------------------
def simulate_keyword_series(location_id: str, keyword: str,
                            weeks: Sequence[str], seed: Any = 90
                            ) -> Tuple[RankObservation, ...]:
    """One plausible position series for ``location_id`` x ``keyword``.

    A bounded random walk on a latent integer: it starts somewhere in the top
    15, drifts by at most two places per week, and occasionally slips past the
    top 20 (returning ``None``) or climbs back. The walk is driven only by
    ``sha256(seed, location_id, keyword)``, so the series is identical for a
    given seed and does not depend on the order in which series are generated.

    Every observation carries ``source=SIMULATED``.
    """
    if not isinstance(location_id, str) or not location_id.strip():
        raise ValueError("location_id must be a non-empty string")
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("keyword must be a non-empty string")
    week_list: List[str] = []
    for week in weeks:
        if not isinstance(week, str):
            raise ValueError(f"ISO week must be a string, got {week!r}")
        parse_week(week)  # validate: raises ValueError when malformed
        week_list.append(week)
    if len(set(week_list)) != len(week_list):
        raise ValueError("weeks must not repeat")
    if not week_list:
        return ()

    rng = _DeterministicRng(_seed_int(seed, location_id.strip(),
                                      keyword.strip()))
    latent = 1 + rng.randbelow(15)  # 1..15: plausible starting place
    observations: List[RankObservation] = []
    for index, label in enumerate(week_list):
        if index:
            latent += rng.randbelow(5) - 2  # -2..+2 drift per week
            if rng.randbelow(6) == 0:       # occasionally a bigger slip
                latent += 1 + rng.randbelow(6)
        latent = max(1, min(latent, MAX_POSITION + 6))
        position = latent if latent <= MAX_POSITION else None
        observations.append(RankObservation(
            location_id=location_id.strip(), keyword=keyword.strip(),
            week=label, position=position, source=SIMULATED))
    return tuple(observations)


def simulate_series(location_id: str, keyword: str, weeks: Sequence[str],
                    seed: Any = 90) -> RankSeries:
    """:func:`simulate_keyword_series` wrapped in a :class:`RankSeries`."""
    return RankSeries(simulate_keyword_series(location_id, keyword, weeks, seed))


#: The fixture location set: the demo salon plus one sibling location, both
#: identified by resource id (the shared multi-location convention).
DEFAULT_LOCATIONS: Tuple[str, ...] = (
    "accounts/1001/locations/loc-001",
    "accounts/1001/locations/loc-002",
)

#: The fixture keyword set (plain synthetic keyword text).
DEFAULT_KEYWORDS: Tuple[str, ...] = (
    "nail salon",
    "gel nails",
    "pedicure near me",
)

#: Default seed. The issue number, on purpose: reproducible and memorable.
DEFAULT_SEED = 90

#: The fixture period: eight consecutive ISO weeks starting 2026-W10.
DEFAULT_WEEKS: Tuple[str, ...] = iso_week_range("2026-W10", 8)


def simulate_dataset(seed: Any = DEFAULT_SEED,
                     locations: Sequence[str] = DEFAULT_LOCATIONS,
                     keywords: Sequence[str] = DEFAULT_KEYWORDS,
                     weeks: Sequence[str] = DEFAULT_WEEKS) -> RankSeries:
    """A fully synthetic dataset: every location x keyword x week cell.

    Deterministic in ``seed`` and in the *content* of the inputs. ``weeks`` is
    the time axis, so its order defines each series' trajectory; the order of
    ``locations`` and ``keywords`` never matters. Every cell is filled,
    including ``None`` ("outside top 20") weeks.
    """
    series = RankSeries()
    for location_id in locations:
        for keyword in keywords:
            for observation in simulate_keyword_series(
                    location_id, keyword, weeks, seed):
                series.add(observation)
    return series


# --------------------------------------------------------------------------
# metrics (definitions in METRIC_DEFINITIONS / the module docstring)
# --------------------------------------------------------------------------
def _observations(observations: Iterable[Any]) -> List[RankObservation]:
    items = list(observations)
    for item in items:
        if not isinstance(item, RankObservation):
            raise ValueError(
                "metrics take RankObservation values only, got "
                f"{type(item).__name__}")
    return items


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


def coverage(observations: Iterable[Any]) -> float:
    """Share of observations with a non-null position (3 decimals)."""
    items = _observations(observations)
    if not items:
        return 0.0
    non_null = sum(1 for obs in items if obs.position is not None)
    return round(non_null / len(items), 3)


def _positions(items: Sequence[RankObservation]) -> List[int]:
    return [obs.position for obs in items if obs.position is not None]


def average_position(observations: Iterable[Any]) -> Optional[float]:
    """Mean of the non-null positions only (3 decimals); ``None`` if none."""
    positions = _positions(_observations(observations))
    if not positions:
        return None
    return round(sum(positions) / len(positions), 3)


def best_position(observations: Iterable[Any]) -> Optional[int]:
    positions = _positions(_observations(observations))
    return min(positions) if positions else None


def worst_position(observations: Iterable[Any]) -> Optional[int]:
    positions = _positions(_observations(observations))
    return max(positions) if positions else None


def top_share(observations: Iterable[Any], limit: int) -> float:
    """Share of **all** observations with a non-null position ``<= limit``.

    Null ("outside top 20") weeks stay in the denominator, so the share cannot
    be inflated by dropping the weeks the salon was invisible.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"limit must be a positive int, got {limit!r}")
    items = _observations(observations)
    if not items:
        return 0.0
    hits = sum(1 for obs in items
               if obs.position is not None and obs.position <= limit)
    return round(hits / len(items), 3)


def week_over_week(observations: Iterable[Any]) -> Tuple[Dict[str, Any], ...]:
    """Delta of average position for consecutive ISO weeks (sorted).

    ``delta = later - earlier``; a negative delta means the average position
    *number* fell, i.e. the salon moved up. A week with no non-null position has
    ``None`` average and produces a ``None`` delta -- it is reported, never
    skipped.
    """
    items = _observations(observations)
    by_week: Dict[str, List[RankObservation]] = {}
    for obs in items:
        by_week.setdefault(obs.week, []).append(obs)
    weeks = sorted(by_week)
    out: List[Dict[str, Any]] = []
    for earlier, later in zip(weeks, weeks[1:]):
        earlier_avg = average_position(by_week[earlier])
        later_avg = average_position(by_week[later])
        if earlier_avg is None or later_avg is None:
            delta: Optional[float] = None
            direction = "unknown"
        else:
            delta = round(later_avg - earlier_avg, 3)
            direction = ("improved" if delta < 0
                         else "worsened" if delta > 0 else "flat")
        out.append({
            "from_week": earlier,
            "to_week": later,
            "from_average_position": earlier_avg,
            "to_average_position": later_avg,
            "delta": delta,
            "direction": direction,
        })
    return tuple(out)


def _keyword_score(obs: RankObservation) -> float:
    """``max(0, 21 - position) / 20``; a null week scores 0."""
    if obs.position is None:
        return 0.0
    return max(0, MAX_POSITION + 1 - obs.position) / MAX_POSITION


def visibility_by_keyword(observations: Iterable[Any]
                          ) -> Tuple[Dict[str, Any], ...]:
    """Per-keyword visibility score: mean of the per-observation scores."""
    items = _observations(observations)
    by_keyword: Dict[str, List[RankObservation]] = {}
    for obs in items:
        by_keyword.setdefault(obs.keyword, []).append(obs)
    out: List[Dict[str, Any]] = []
    for keyword in sorted(by_keyword):
        bucket = by_keyword[keyword]
        score = sum(_keyword_score(obs) for obs in bucket) / len(bucket)
        out.append({
            "keyword": keyword,
            "observations": len(bucket),
            "in_top_20": sum(1 for obs in bucket if obs.position is not None),
            "score": round(score, 4),
        })
    return tuple(out)


def visibility_index(observations: Iterable[Any]) -> Optional[float]:
    """INTERNAL index: mean over keywords of the per-keyword visibility score.

    See :data:`VISIBILITY_INDEX_LABEL` and :data:`METRIC_DEFINITIONS`: this is
    not a Google metric and is always reported next to the SIMULATED label.
    ``None`` when there are no observations.
    """
    per_keyword = visibility_by_keyword(observations)
    if not per_keyword:
        return None
    return round(sum(row["score"] for row in per_keyword) / len(per_keyword), 4)


# --------------------------------------------------------------------------
# per-location per-period summary
# --------------------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class LocationPeriodSummary:
    """One location, one period. ``simulated`` is REQUIRED and must be ``True``.

    There is no default for ``simulated``: omitting it is a ``TypeError``, and
    passing anything falsy raises ``ValueError``. A summary therefore cannot
    exist without the simulated marker, and it is impossible to publish an
    unlabelled summary by accident.
    """

    simulated: bool
    location_id: str
    weeks: Tuple[str, ...]
    keyword_count: int
    observation_count: int
    coverage: float
    average_position: Optional[float]
    best_position: Optional[int]
    worst_position: Optional[int]
    top_3_share: float
    top_10_share: float
    visibility_index: Optional[float]
    visibility_index_label: str = VISIBILITY_INDEX_LABEL
    week_over_week: Tuple[Dict[str, Any], ...] = ()
    visibility_by_keyword: Tuple[Dict[str, Any], ...] = ()
    metric_definitions: Dict[str, str] = field(
        default_factory=lambda: dict(METRIC_DEFINITIONS))

    def __post_init__(self) -> None:
        if self.simulated is not True:
            raise ValueError(
                "a ranking summary requires simulated=True; a summary of "
                "non-simulated data cannot be produced by this module")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "simulated": True,
            "label": SIMULATED_LABEL,
            "label_line": f"{SIMULATED_LABEL} DATA -- {SOURCE_LABEL}",
            "source": SIMULATED,
            "source_label": SOURCE_LABEL,
            "disclaimer": DISCLAIMER,
            "multi_location_note": MULTI_LOCATION_NOTE,
            "location_id": self.location_id,
            "weeks": list(self.weeks),
            "week_count": len(self.weeks),
            "keyword_count": self.keyword_count,
            "observation_count": self.observation_count,
            "metrics": {
                "coverage": self.coverage,
                "average_position": self.average_position,
                "best_position": self.best_position,
                "worst_position": self.worst_position,
                "top_3_share": self.top_3_share,
                "top_10_share": self.top_10_share,
                "visibility_index": self.visibility_index,
            },
            "visibility_index_label": self.visibility_index_label,
            "week_over_week": [dict(row) for row in self.week_over_week],
            "visibility_by_keyword": [dict(row)
                                      for row in self.visibility_by_keyword],
            "metric_definitions": dict(self.metric_definitions),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


def summarize_location(location_id: str,
                       observations: Iterable[RankObservation],
                       weeks: Optional[Sequence[str]] = None
                       ) -> LocationPeriodSummary:
    """Summarize one location's observations for one period.

    ``weeks`` fixes the period (and its order) and **bounds** the metrics: only
    observations inside it are counted, so a summary can never mix two periods.
    When it is omitted the period is the sorted set of weeks present in
    ``observations``. Only :class:`RankObservation` values are accepted, so only
    simulated data can reach a summary.
    """
    if not isinstance(location_id, str) or not location_id.strip():
        raise ValueError("location_id must be a non-empty string")
    location_id = location_id.strip()
    items = _observations(observations)
    foreign = sorted({obs.location_id for obs in items
                      if obs.location_id != location_id})
    if foreign:
        raise ValueError(
            f"summarize_location({location_id!r}) received observations for "
            f"other locations: {foreign!r}; one summary is one location")
    if weeks is None:
        period = tuple(sorted({obs.week for obs in items}))
    else:
        period = tuple(week_label(*parse_week(week)) for week in weeks)
        if len(set(period)) != len(period):
            raise ValueError("weeks must not repeat")
        # the period BOUNDS the metrics: weeks outside it are not counted, so a
        # summary can never mix two periods together.
        in_period = set(period)
        items = [obs for obs in items if obs.week in in_period]

    return LocationPeriodSummary(
        simulated=True,
        location_id=location_id,
        weeks=period,
        keyword_count=len({obs.keyword for obs in items}),
        observation_count=len(items),
        coverage=coverage(items),
        average_position=average_position(items),
        best_position=best_position(items),
        worst_position=worst_position(items),
        top_3_share=top_share(items, 3),
        top_10_share=top_share(items, 10),
        visibility_index=visibility_index(items),
        week_over_week=week_over_week(items),
        visibility_by_keyword=visibility_by_keyword(items),
    )


@dataclass(frozen=True, kw_only=True)
class RankingReport:
    """Every location's summary for one period, plus the simulated marker."""

    simulated: bool
    seed: Any
    weeks: Tuple[str, ...]
    summaries: Tuple[LocationPeriodSummary, ...]

    def __post_init__(self) -> None:
        if self.simulated is not True:
            raise ValueError(
                "a ranking report requires simulated=True; a report of "
                "non-simulated data cannot be produced by this module")
        for summary in self.summaries:
            if summary.simulated is not True:
                raise ValueError(
                    "every summary in a report must carry simulated=True")

    @property
    def location_ids(self) -> Tuple[str, ...]:
        return tuple(summary.location_id for summary in self.summaries)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "simulated": True,
            "label": SIMULATED_LABEL,
            "label_line": f"{SIMULATED_LABEL} DATA -- {SOURCE_LABEL}",
            "source": SIMULATED,
            "source_label": SOURCE_LABEL,
            "mode": MODE,
            "disclaimer": DISCLAIMER,
            "multi_location_note": MULTI_LOCATION_NOTE,
            "seed": self.seed,
            "weeks": list(self.weeks),
            "week_count": len(self.weeks),
            "location_count": len(self.summaries),
            "location_ids": list(self.location_ids),
            "visibility_index_label": VISIBILITY_INDEX_LABEL,
            "metric_definitions": dict(METRIC_DEFINITIONS),
            "summaries": [summary.as_dict() for summary in self.summaries],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


def summarize_series(series: RankSeries,
                     weeks: Optional[Sequence[str]] = None,
                     seed: Any = DEFAULT_SEED) -> RankingReport:
    """Summarize every location in ``series`` for one period."""
    if not isinstance(series, RankSeries):
        raise ValueError(
            "summarize_series takes a RankSeries, got "
            f"{type(series).__name__}")
    if weeks is None:
        period = tuple(series.weeks())
    else:
        period = tuple(week_label(*parse_week(week)) for week in weeks)
    summaries = tuple(
        summarize_location(location_id, series.select(location_id=location_id),
                           period)
        for location_id in series.locations())
    return RankingReport(simulated=True, seed=seed, weeks=period,
                         summaries=summaries)
