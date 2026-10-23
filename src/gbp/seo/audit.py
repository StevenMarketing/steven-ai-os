"""Deterministic, offline GMap SEO audit for multi-location nail salons.

Why this module exists
----------------------
Review replies (GReply) and profile completeness are the two levers a salon has
on Google Maps. GReply already covers replies; this module covers the *profile*
side, purely offline:

* it audits **synthetic** GBP fixtures (categories, services, description,
  hours, identity) and turns them into explainable, prioritized actions;
* it never contacts Google, never ranks competitors and never claims a ranking
  win -- it only says which profile fields are missing, invalid or inconsistent;
* it is deterministic: same fixtures in, byte-identical report out (sorted
  everywhere, no clock, no randomness, no set iteration order).

Multi-location identity
-----------------------
The shared dashboard keys a shop on the location resource id and distinguishes
two salons that share a name by their **ZIP** (see ``tests/test_zip_code.py``).
The audit mirrors that: two locations with the same title but distinct ZIPs are
kept apart and reported as "disambiguate", while two locations that share both
title *and* ZIP are reported as a probable duplicate listing.
"""

from __future__ import annotations

import copy
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

__all__ = [
    "ALLOWED_PRIMARY_CATEGORIES",
    "DISCLAIMER",
    "NAIL_SERVICE_VOCABULARY",
    "PREFERRED_PRIMARY_CATEGORY",
    "REQUIRED_FIELDS",
    "SEVERITY_ORDER",
    "Finding",
    "LocationAudit",
    "AuditReport",
    "audit_location",
    "audit_profiles",
    "load_profiles",
    "severity_rank",
]

#: Emitted on every report. Fixtures only -- no live connectivity, no rankings.
DISCLAIMER = (
    "Offline audit of synthetic Google Business Profile fixtures. No live Google "
    "connectivity, no competitor ranking data, no ranking improvement is claimed "
    "or implied, and no profile is edited automatically."
)

PREFERRED_PRIMARY_CATEGORY = "nail salon"

#: Categories that are legitimate for a nail shop. A primary category outside
#: this set is not an error, it is a signal that "Nail Salon" may fit better.
ALLOWED_PRIMARY_CATEGORIES = (
    "nail salon",
    "nail technician",
    "beauty salon",
    "day spa",
    "waxing service",
)

#: Fields a complete Maps profile is expected to carry.
REQUIRED_FIELDS = (
    "title",
    "primary_category",
    "city",
    "state",
    "postal_code",
    "phone",
    "website",
    "hours",
    "description",
    "services",
)

#: Severity ordering for the ``actions`` list, worst first. ``high`` before
#: ``medium`` before ``low`` -- a documented convention of this module, not a
#: Google metric.
SEVERITY_ORDER: Dict[str, int] = {"high": 0, "medium": 1, "low": 2}
#: Heuristic completeness-score penalty per finding, in descending steps
#: (15 / 7 / 3). Basis: product scoring heuristic -- Google publishes no profile
#: "score", so these weights are a deterministic, documented convention (see
#: ``docs/SEO-AUDIT.md``) whose only job is to rank findings worst-first. They
#: are not a Google ranking factor and imply no ranking outcome.
_SEVERITY_WEIGHT: Dict[str, int] = {"high": 15, "medium": 7, "low": 3}

#: Findings that are informational only and must NOT reduce the completeness
#: ``score``. ``SEO-IDENTITY-REUSED-NAME`` (the same business name across
#: locations that have *distinct* ZIPs) is normal for a chain, so it stays in
#: the report and in the ordered ``actions`` list but carries no score penalty.
#: The probable-duplicate ``SEO-IDENTITY-DUPLICATE-NAME-ZIP`` is deliberately
#: NOT listed here: sharing name *and* ZIP keeps its high-severity penalty.
#: ``SEO-HOURS-OVERNIGHT`` (a span that crosses midnight, e.g. ``22:00-02:00``)
#: is an owner decision (issue #105): it is a low-severity note to verify, not
#: an error, so it too is reported but never lowers the score.
_NON_SCORING_CODES = frozenset({"SEO-IDENTITY-REUSED-NAME", "SEO-HOURS-OVERNIGHT"})

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")

#: 24-hour ``HH:MM`` only -- the shape GBP hours use.
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
#: Minutes in a day: a close of ``00:00`` is read as ``24:00`` (midnight at the
#: end of the day, not the start). See ``_check_hours`` for the exact rule.
_MINUTES_PER_DAY = 24 * 60
#: North-American Numbering Plan shape (3-3-4). Basis: Google's content
#: guidelines steer contact details into their dedicated fields, not the
#: description, so a phone-shaped run inside the description is flagged.
_PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")
#: Any http(s):// or www. prefix. Basis: Google's content guidelines bar links
#: from the description; the website belongs in the dedicated field.
_URL_RE = re.compile(r"(https?://|www\.)", re.IGNORECASE)

#: Google caps the GBP business description at 750 characters (documented field
#: limit -- text beyond it is truncated) and shows roughly the first 250
#: characters before truncating in search. 250 is therefore the practical
#: minimum for a useful, non-truncated description; 750 is the hard maximum.
_DESCRIPTION_MIN = 250
_DESCRIPTION_MAX = 750
#: Anti-keyword-stuffing heuristic. Google's guidelines forbid keyword stuffing
#: but publish no repetition number, so this is a deliberately conservative,
#: documented product heuristic: one content word is flagged only when it
#: repeats at least 6 times AND is at least 6% of the content words, which
#: ordinary prose does not reach.
_STUFFING_MIN_REPEAT = 6
_STUFFING_DENSITY = 0.06
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "you", "your", "our", "we", "a", "an", "of",
    "to", "in", "on", "at", "is", "are", "be", "it", "that", "this", "as",
    "by", "or", "from", "all", "any", "each", "we're", "us",
})

#: Small, hand-maintained vocabulary of well-known nail services. It exists ONLY
#: to cross-reference a description against the services list -- matching is
#: offline and deterministic (no ML, no network) and a match is never treated as
#: proof that the business provides anything. Terms are lowercase phrases.
NAIL_SERVICE_VOCABULARY: Tuple[str, ...] = (
    "manicure",
    "pedicure",
    "gel nails",
    "acrylic nails",
    "nail art",
    "gel polish",
    "dip powder",
    "shellac",
    "french tips",
    "nail extensions",
    "spa pedicure",
)


def severity_rank(severity: str) -> int:
    """Rank a severity so ``high`` sorts before ``medium`` before ``low``."""
    return SEVERITY_ORDER.get(str(severity).strip().lower(), len(SEVERITY_ORDER))


# --------------------------------------------------------------------------
# value objects
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    """One explainable audit result: what, how bad, why, and what to do."""

    code: str
    severity: str
    field: str
    message: str
    evidence: str = ""
    recommendation: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "field": self.field,
            "message": self.message,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


@dataclass
class LocationAudit:
    """The audit of exactly one location (one synthetic profile)."""

    location_name: str
    title: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    findings: List[Finding] = field(default_factory=list)

    @property
    def identity(self) -> Tuple[str, str]:
        """The dashboard identity convention: normalised ``(name, ZIP)``."""
        return (_norm(self.title), _norm(self.postal_code))

    @property
    def score(self) -> int:
        """Deterministic 0-100 completeness score (higher is more complete).

        Penalty is the summed severity weight, except that informational
        findings listed in :data:`_NON_SCORING_CODES` (currently only the
        reused-name note) are excluded: they are reported but never lower the
        score.
        """
        penalty = sum(_SEVERITY_WEIGHT.get(f.severity, 0)
                      for f in self.findings if f.code not in _NON_SCORING_CODES)
        return max(0, 100 - penalty)

    @property
    def actions(self) -> List[Finding]:
        """Findings ordered by priority: severity, then code, then field."""
        return sorted(self.findings,
                      key=lambda f: (severity_rank(f.severity), f.code, f.field))

    @property
    def highest_severity(self) -> Optional[str]:
        if not self.findings:
            return None
        return self.actions[0].severity

    def as_dict(self) -> Dict[str, Any]:
        return {
            "location_name": self.location_name,
            "title": self.title,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code,
            "identity": {"name": self.identity[0], "postal_code": self.identity[1]},
            "score": self.score,
            "highest_severity": self.highest_severity,
            "finding_count": len(self.findings),
            "findings": [f.as_dict() for f in self.actions],
        }


@dataclass
class AuditReport:
    """A whole-account audit: per-location results plus cross-location identity."""

    locations: List[LocationAudit] = field(default_factory=list)
    duplicate_names: Dict[str, List[str]] = field(default_factory=dict)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    fixture_only: bool = True

    @property
    def total_findings(self) -> int:
        return sum(len(loc.findings) for loc in self.locations)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "fixture_only": self.fixture_only,
            "source": "synthetic-fixture",
            "disclaimer": DISCLAIMER,
            "location_count": len(self.locations),
            "total_findings": self.total_findings,
            "duplicate_names": {k: list(v) for k, v in sorted(self.duplicate_names.items())},
            "skipped": sorted(self.skipped, key=lambda d: d.get("reason", "")),
            "locations": [loc.as_dict()
                          for loc in sorted(self.locations,
                                            key=lambda l: (l.identity, l.location_name))],
        }

    def to_json(self) -> str:
        """Canonical JSON (sorted keys) so two runs are byte-identical."""
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _norm(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _lower(value: Any) -> str:
    return _norm(value).lower()


def _as_list(value: Any) -> List[str]:
    """Coerce a fixture field into a list of non-empty trimmed strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"expected a list, got {type(value).__name__}")
    out: List[str] = []
    for item in value:
        text = _norm(item)
        if text:
            out.append(text)
    return out


def _is_blank(value: Any) -> bool:
    """True when a required field carries no usable (normalized) content.

    Emptiness is judged on normalized content, not raw truthiness: a list or
    tuple whose items are all blank (e.g. ``['']`` or ``['   ']``) is empty,
    exactly like ``[]``.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return not _as_list(value)
    if isinstance(value, dict):
        return not value
    return False


def _minutes(hhmm: str) -> Optional[int]:
    match = _TIME_RE.match(_norm(hhmm))
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _finding(code: str, severity: str, fld: str, message: str,
             evidence: str = "", recommendation: str = "") -> Finding:
    return Finding(code=code, severity=severity, field=fld, message=message,
                   evidence=evidence, recommendation=recommendation)


_WORD_RE = re.compile(r"[a-z0-9]+")


def _stem(token: str) -> str:
    """Conservative plural fold, used only for service/description matching.

    ``nails`` -> ``nail`` and ``manicures`` -> ``manicure``; words that already
    end in ``ss`` (e.g. ``glass``) are left unchanged so the rule stays simple
    and deterministic.
    """
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _stems(text: Any) -> set:
    """The set of folded word stems in ``text`` (for membership tests only)."""
    return {_stem(t) for t in _WORD_RE.findall(str(text).lower())}


def _phrase_stems(phrase: str) -> List[str]:
    return [_stem(t) for t in _WORD_RE.findall(phrase.lower())]


def _mentions(text_stems: Any, phrase: str) -> bool:
    """True when every word of ``phrase`` appears among ``text_stems``.

    Order is ignored; a phrase counts as present only when all of its tokens
    are present, so a single shared generic word (e.g. ``nail``) does not make a
    multi-word service (e.g. ``gel nails``) "present".
    """
    parts = _phrase_stems(phrase)
    return bool(parts) and all(p in text_stems for p in parts)


# --------------------------------------------------------------------------
# individual checks (each returns a list of findings; pure + deterministic)
# --------------------------------------------------------------------------
def _check_completeness(profile: Dict[str, Any]) -> List[Finding]:
    out: List[Finding] = []
    for name in REQUIRED_FIELDS:
        value = profile.get(name)
        if _is_blank(value):
            out.append(_finding(
                "SEO-MISSING-FIELD", "high", name,
                f"Required profile field '{name}' is missing or empty.",
                evidence=f"{name}=<empty>",
                recommendation=(f"Add the '{name}' field to the profile so the "
                                "listing is complete; a complete profile is "
                                "easier for customers to evaluate. No ranking "
                                "outcome is guaranteed.")))
    return out


def _check_primary_category(profile: Dict[str, Any]) -> List[Finding]:
    primary = _lower(profile.get("primary_category"))
    if not primary:
        return []  # already covered by completeness
    if primary not in ALLOWED_PRIMARY_CATEGORIES:
        return [_finding(
            "SEO-PRIMARY-CATEGORY-OFF", "medium", "primary_category",
            "Primary category is not a nail-shop category.",
            evidence=f"primary_category={_norm(profile.get('primary_category'))}",
            recommendation=(f"Set the primary category to "
                            f"'{PREFERRED_PRIMARY_CATEGORY.title()}' (or another "
                            "nail/beauty category) so the shop matches the right "
                            "searches."))]
    if primary != PREFERRED_PRIMARY_CATEGORY:
        return [_finding(
            "SEO-PRIMARY-CATEGORY-SUBOPTIMAL", "low", "primary_category",
            "Primary category is valid but not the closest match for a nail salon.",
            evidence=f"primary_category={_norm(profile.get('primary_category'))}",
            recommendation=(f"Consider '{PREFERRED_PRIMARY_CATEGORY.title()}' as "
                            "the primary category for a nail shop."))]
    return []


def _check_secondary_categories(profile: Dict[str, Any]) -> List[Finding]:
    out: List[Finding] = []
    try:
        secondary = _as_list(profile.get("secondary_categories"))
    except ValueError as exc:
        return [_finding("SEO-PROFILE-MALFORMED", "medium", "secondary_categories",
                         "secondary_categories is not a list.", evidence=str(exc),
                         recommendation="Store the field as a JSON array of strings.")]
    if not secondary:
        out.append(_finding(
            "SEO-SECONDARY-CATEGORIES-EMPTY", "medium", "secondary_categories",
            "No secondary categories defined.",
            recommendation=("Add specific secondary categories (e.g. Manicure, "
                            "Pedicure, Nail Art) that describe real services.")))
        return out
    lowered = [_lower(s) for s in secondary]
    dupes = sorted({s for s in lowered if lowered.count(s) > 1})
    if dupes:
        out.append(_finding(
            "SEO-SECONDARY-CATEGORIES-DUPLICATE", "low", "secondary_categories",
            "Duplicate secondary categories.",
            evidence=", ".join(dupes),
            recommendation="Remove duplicate categories."))
    primary = _lower(profile.get("primary_category"))
    if primary and primary in lowered:
        out.append(_finding(
            "SEO-SECONDARY-CATEGORIES-OVERLAP-PRIMARY", "low",
            "secondary_categories",
            "A secondary category repeats the primary category.",
            evidence=primary,
            recommendation="Keep the primary category out of the secondary list."))
    unique = sorted(set(lowered))
    # Basis: Google allows one primary plus up to 9 additional categories, but
    # its guidance is to use the fewest that describe the core business, so a
    # focused set of 3-5 secondaries is the practical target. OWNER DECISION
    # (Steven, 2026-10-22): the > 5 rule stays a low-severity advisory and its
    # behaviour is UNCHANGED -- this pass only records its basis in code.
    if len(unique) > 5:
        out.append(_finding(
            "SEO-SECONDARY-CATEGORIES-TOO-MANY", "low", "secondary_categories",
            "More than five secondary categories.",
            evidence=f"count={len(unique)}",
            recommendation=("Keep 3-5 secondary categories that reflect real "
                            "services; extra ones dilute the profile.")))
    return out


def _check_services(profile: Dict[str, Any]) -> List[Finding]:
    out: List[Finding] = []
    try:
        services = _as_list(profile.get("services"))
    except ValueError as exc:
        return [_finding("SEO-PROFILE-MALFORMED", "medium", "services",
                         "services is not a list.", evidence=str(exc),
                         recommendation="Store the field as a JSON array of strings.")]
    if not services:
        return out  # completeness already flags it
    lowered = [_lower(s) for s in services]
    dupes = sorted({s for s in lowered if lowered.count(s) > 1})
    if dupes:
        out.append(_finding(
            "SEO-SERVICES-DUPLICATE", "low", "services",
            "Duplicate service entries.",
            evidence=", ".join(dupes),
            recommendation="Remove duplicate services."))
    # Basis: a profile should name the shop's services; 3 is the documented
    # practical minimum this module uses (below that the list cannot describe
    # the shop). Google recommends listing every service the business sells.
    if len(set(lowered)) < 3:
        out.append(_finding(
            "SEO-SERVICES-TOO-FEW", "low", "services",
            "Fewer than three services listed.",
            evidence=f"count={len(set(lowered))}",
            recommendation=("List at least three real services so customers can "
                            "see what the shop offers.")))
    return out


def _check_description_services_coherence(profile: Dict[str, Any]) -> List[Finding]:
    """Deterministic cross-reference between the description and services list.

    Two independent, purely offline checks:

    * (a) a service is listed whose words never appear in the description;
    * (b) a well-known nail term (from :data:`NAIL_SERVICE_VOCABULARY`) appears
      in the description but not in the services list.

    Findings report only the cross-reference result and quote the matched terms;
    they never assert what the business actually offers, and they invent no
    service or category. The check is skipped when either side is missing --
    that case belongs to the completeness check.
    """
    try:
        services = _as_list(profile.get("services"))
    except ValueError:
        return []  # malformed services already reported by _check_services
    raw = profile.get("description")
    if not services or raw is None or not str(raw).strip():
        return []

    desc_stems = _stems(raw)
    service_stems = _stems(" ".join(services))

    out: List[Finding] = []
    for service in sorted(services, key=lambda s: (s.lower(), s)):
        if not _mentions(desc_stems, service):
            out.append(_finding(
                "SEO-SERVICES-NOT-IN-DESCRIPTION", "low", "services",
                "A listed service is not mentioned in the description.",
                evidence=f"service={service!r} not mentioned in description",
                recommendation=("Mention the service in the description, or "
                                "drop it from the services list, so the two "
                                "fields agree.")))
    missing_terms = sorted(
        term for term in NAIL_SERVICE_VOCABULARY
        if _mentions(desc_stems, term) and not _mentions(service_stems, term))
    for term in missing_terms:
        out.append(_finding(
            "SEO-DESCRIPTION-SERVICE-NOT-LISTED", "low", "description",
            "The description mentions a nail service the services list omits.",
            evidence=f"description term={term!r} not in services list",
            recommendation=("Add the service to the services list, or reword "
                            "the description so it does not imply it.")))
    return out


def _check_description(profile: Dict[str, Any]) -> List[Finding]:
    out: List[Finding] = []
    raw = profile.get("description")
    if raw is None or not str(raw).strip():
        return out  # completeness handles the missing case
    text = str(raw)
    length = len(text.strip())
    if length < _DESCRIPTION_MIN:
        out.append(_finding(
            "SEO-DESCRIPTION-TOO-SHORT", "medium", "description",
            "Description is shorter than the recommended minimum.",
            evidence=f"length={length} (min {_DESCRIPTION_MIN})",
            recommendation=("Expand the description to roughly "
                            f"{_DESCRIPTION_MIN}-{_DESCRIPTION_MAX} characters "
                            "covering real services and the neighbourhood.")))
    if length > _DESCRIPTION_MAX:
        out.append(_finding(
            "SEO-DESCRIPTION-TOO-LONG", "low", "description",
            "Description exceeds the recommended maximum.",
            evidence=f"length={length} (max {_DESCRIPTION_MAX})",
            recommendation="Trim the description to stay within the limit."))
    if _PHONE_RE.search(text):
        out.append(_finding(
            "SEO-DESCRIPTION-PHONE", "medium", "description",
            "Description contains a phone number.",
            evidence=_PHONE_RE.search(text).group(0),
            recommendation=("Remove phone numbers from the description; keep "
                            "contact details in their dedicated fields.")))
    if _URL_RE.search(text):
        out.append(_finding(
            "SEO-DESCRIPTION-URL", "medium", "description",
            "Description contains a URL.",
            evidence=_URL_RE.search(text).group(0),
            recommendation=("Remove links from the description; use the website "
                            "field instead.")))
    # Guard: require at least 20 letters so short labels or abbreviations are
    # not misjudged, and treat > 50% uppercase as "mostly upper case".
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 20 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.5:
        out.append(_finding(
            "SEO-DESCRIPTION-ALL-CAPS", "low", "description",
            "Description is written mostly in upper case.",
            evidence=f"uppercase_ratio>0.5 across {len(letters)} letters",
            recommendation="Rewrite in sentence case; shouting reads as spam."))
    # Ignore stopwords and 1-2 character tokens so they cannot dominate the
    # repetition/density heuristic (see _STUFFING_* above).
    words = [w for w in re.findall(r"[a-z0-9']+", text.lower())
             if w not in _STOPWORDS and len(w) > 2]
    if words:
        counts: Dict[str, int] = {}
        for w in words:
            counts[w] = counts.get(w, 0) + 1
        top_word = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if top_word[1] >= _STUFFING_MIN_REPEAT and \
                top_word[1] / len(words) >= _STUFFING_DENSITY:
            out.append(_finding(
                "SEO-DESCRIPTION-KEYWORD-STUFFING", "medium", "description",
                "A single word is repeated unnaturally often.",
                evidence=f"'{top_word[0]}' x{top_word[1]} of {len(words)} words",
                recommendation=("Reword to natural prose; do not repeat a keyword "
                                "to game matching. Keyword stuffing is against "
                                "Google's guidelines.")))
    return out


def _check_hours(profile: Dict[str, Any]) -> List[Finding]:
    hours = profile.get("hours")
    if not hours:
        return []  # completeness handles the missing case
    if not isinstance(hours, dict):
        return [_finding("SEO-HOURS-MALFORMED", "medium", "hours",
                         "hours is not a mapping of day -> interval.",
                         evidence=type(hours).__name__,
                         recommendation="Provide hours as day -> {open, close}.")]
    out: List[Finding] = []
    closed_days = 0
    checked = 0
    for day in _WEEKDAYS:
        if day not in hours:
            out.append(_finding("SEO-HOURS-DAY-MISSING", "medium", "hours",
                                f"Opening hours for {day} are not set.",
                                evidence=f"missing day={day}",
                                recommendation=f"Set hours for {day} or mark it closed."))
            continue
        value = hours[day]
        if value in (None, "closed", "Closed"):
            closed_days += 1
            checked += 1
            continue
        if not isinstance(value, dict):
            out.append(_finding("SEO-HOURS-MALFORMED", "medium", "hours",
                                f"Invalid hours entry for {day}.",
                                evidence=f"{day}={value!r}",
                                recommendation="Use {open:'HH:MM', close:'HH:MM'}."))
            continue
        checked += 1
        start = _minutes(str(value.get("open") or ""))
        end = _minutes(str(value.get("close") or ""))
        # Midnight close (issue #98): ``00:00`` on the close side means the end
        # of the day (24:00), not the start of it, so a shop open 09:00-00:00
        # closes at midnight rather than having an inverted range. The rewrite
        # applies ONLY when the open time is a real time after 00:00 (start > 0)
        # and the close is exactly midnight, so:
        #   * 09:00-00:00 -> close read as 1440, valid (no finding);
        #   * 00:00-00:00 -> start == 0, unchanged, still an invalid range.
        if end == 0 and start is not None and start > 0:
            end = _MINUTES_PER_DAY
        if start is None or end is None:
            out.append(_finding("SEO-HOURS-MALFORMED", "medium", "hours",
                                f"Unparseable time for {day}.",
                                evidence=f"{day}={value!r}",
                                recommendation="Times must be 24h HH:MM."))
        elif start == end:
            # Zero-length span: open and close are the same instant.
            out.append(_finding("SEO-HOURS-INVALID-RANGE", "medium", "hours",
                                f"Opening time is not before closing time for {day}.",
                                evidence=f"{day}: {value.get('open')}-{value.get('close')}",
                                recommendation="Fix the open/close times."))
        elif start > end:
            # Overnight span (issue #105, owner decision 2026-10-23): both times
            # parse and the close is earlier on the clock than the open, i.e. the
            # span crosses midnight (22:00-02:00, 19:00-09:00). This is a valid
            # way for a shop to stay open past midnight, so it is a low-severity
            # NOTE to verify, not an error, and never lowers the score.
            out.append(_finding("SEO-HOURS-OVERNIGHT", "low", "hours",
                                f"Opening hours for {day} cross midnight; "
                                "confirm this is intentional.",
                                evidence=f"{day}: {value.get('open')}-{value.get('close')}",
                                recommendation=("If the shop is deliberately open "
                                                "past midnight, keep the span; "
                                                "otherwise fix the open/close times.")))
    if checked and closed_days == checked:
        out.append(_finding("SEO-HOURS-ALL-CLOSED", "high", "hours",
                            "Every day is closed, so the shop never appears open.",
                            evidence=f"closed_days={closed_days}",
                            recommendation="Set real opening hours."))
    return out


# --------------------------------------------------------------------------
# per-location and whole-account entry points
# --------------------------------------------------------------------------
def audit_location(profile: Dict[str, Any]) -> LocationAudit:
    """Audit exactly one synthetic profile. Never raises on malformed input."""
    if not isinstance(profile, dict):
        raise ValueError(f"profile must be a mapping, got {type(profile).__name__}")
    findings: List[Finding] = []
    findings += _check_completeness(profile)
    findings += _check_primary_category(profile)
    findings += _check_secondary_categories(profile)
    findings += _check_services(profile)
    findings += _check_description(profile)
    findings += _check_description_services_coherence(profile)
    findings += _check_hours(profile)
    return LocationAudit(
        location_name=_norm(profile.get("location_name")),
        title=_norm(profile.get("title")),
        city=_norm(profile.get("city")),
        state=_norm(profile.get("state")),
        postal_code=_norm(profile.get("postal_code")),
        findings=findings,
    )


def _identity_findings(locations: Sequence[LocationAudit]) -> Dict[str, List[str]]:
    """Cross-location identity checks; annotates each location in place."""
    by_name: Dict[str, List[LocationAudit]] = {}
    for loc in locations:
        by_name.setdefault(_norm(loc.title).lower(), []).append(loc)
    duplicate_names: Dict[str, List[str]] = {}
    for name in sorted(by_name):
        group = by_name[name]
        if len(group) < 2 or not name:
            continue
        duplicate_names[name] = sorted(
            loc.location_name or f"{loc.title}|{loc.postal_code}" for loc in group)
        # same name + same ZIP (or both missing) => probable duplicate listing
        seen: Dict[str, List[LocationAudit]] = {}
        for loc in group:
            seen.setdefault(_norm(loc.postal_code), []).append(loc)
        for zipcode, shared in sorted(seen.items()):
            if len(shared) > 1:
                for loc in shared:
                    loc.findings.append(_finding(
                        "SEO-IDENTITY-DUPLICATE-NAME-ZIP", "high", "identity",
                        "Another location shares the same name and ZIP.",
                        evidence=f"name={_norm(loc.title)!r} zip={zipcode or '<none>'}",
                        recommendation=("Confirm whether these are duplicate "
                                        "listings; keep one profile per real shop.")))
            else:
                for loc in shared:
                    loc.findings.append(_finding(
                        "SEO-IDENTITY-REUSED-NAME", "low", "identity",
                        "Business name is reused by another location.",
                        evidence=f"name={_norm(loc.title)!r}",
                        recommendation=("Distinguish the locations with distinct "
                                        "ZIPs and the Google resource id; the name "
                                        "alone is not an identifier.")))
    return duplicate_names


def audit_profiles(profiles: Sequence[Any]) -> AuditReport:
    """Audit many synthetic profiles. Non-mappings are skipped, never invented."""
    locations: List[LocationAudit] = []
    skipped: List[Dict[str, str]] = []
    for index, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            skipped.append({"index": str(index),
                            "reason": f"not a mapping ({type(profile).__name__})"})
            continue
        locations.append(audit_location(profile))
    duplicate_names = _identity_findings(locations)
    return AuditReport(locations=locations, duplicate_names=duplicate_names,
                       skipped=skipped)


# --------------------------------------------------------------------------
# synthetic fixtures
# --------------------------------------------------------------------------
def _full_hours() -> Dict[str, Dict[str, str]]:
    """9am-7pm every day, as a fresh mapping (deterministic fixture helper)."""
    return {day: {"open": "09:00", "close": "19:00"} for day in _WEEKDAYS}


#: A tiny, fully synthetic demo account: one complete shop, one incomplete shop
#: and a second shop that reuses the first shop's name under a different ZIP.
DEFAULT_PROFILES: List[Dict[str, Any]] = [
    {
        "location_name": "accounts/1001/locations/loc-001",
        "title": "Demo Nails",
        "city": "Garden Grove", "state": "CA", "postal_code": "92841",
        "primary_category": "Nail Salon",
        "secondary_categories": ["Manicure", "Pedicure", "Nail Art"],
        "services": ["Manicure", "Pedicure", "Gel Nails", "Acrylic Nails"],
        "description": ("Demo Nails in Garden Grove offers manicures, pedicures, "
                        "gel and acrylic nails in a calm, hygienic studio. Our "
                        "team works with walk-ins and appointments and takes care "
                        "of nail health first. Visit us for everyday care or a "
                        "special occasion and settle in while our technicians "
                        "handle the details from shaping to finish."),
        "hours": _full_hours(),
        "phone": "+1 714-555-0142",
        "website": "https://example.invalid/demo-nails",
        "_fixture": True,
    },
    {
        "location_name": "accounts/1001/locations/loc-002",
        "title": "Demo Nails",
        "city": "Westminster", "state": "CA", "postal_code": "92683",
        "primary_category": "Nail Salon",
        "secondary_categories": ["Manicure", "Manicure"],
        "services": ["Manicure"],
        "description": "Great nails.",
        "hours": {"monday": {"open": "19:00", "close": "09:00"}},
        "phone": "+1 714-555-0199",
        "website": "https://example.invalid/demo-nails-west",
        "_fixture": True,
    },
]


def load_profiles(path: Optional[Union[str, pathlib.Path]] = None) -> List[Dict[str, Any]]:
    """Return synthetic profiles.

    With no ``path`` the embedded :data:`DEFAULT_PROFILES` are returned (a deep
    copy, so callers cannot mutate the module constant). With a ``path`` the
    JSON file is read from disk -- still offline, still no network.
    """
    if path is None:
        return copy.deepcopy(DEFAULT_PROFILES)
    with pathlib.Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = data.get("locations", data.get("profiles", []))
    if not isinstance(data, list):
        raise ValueError("fixture file must hold a list of profiles")
    return list(data)
