"""The quality checklist for a reply draft.

Checks (docs/DOMAIN-RULES.md §4): relevance, brand_fit, naturalness, local_seo,
no_fabrication, duplication, approval_compliance, plus the Group-4 additions
``length_ok`` and ``grounding``. Each returns pass/warn/fail with a
human-readable reason. ``ValidationResult.ok`` is True only when no check
fails and fewer than two checks warn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

from ..brand import BrandProfile
from .classify import Classification
from .duplicate import DuplicateChecker, keyword_density_issues, normalize_tokens

__all__ = ["CheckResult", "ValidationResult", "validate", "CHECK_NAMES", "FABRICATION_SERVICES"]

PASS = "pass"
WARN = "warn"
FAIL = "fail"

CHECK_NAMES = (
    "relevance",
    "brand_fit",
    "naturalness",
    "local_seo",
    "no_fabrication",
    "duplication",
    "approval_compliance",
    "length_ok",
    "grounding",
)

#: service vocabulary used for fabrication / grounding detection.
FABRICATION_SERVICES = (
    "manicure",
    "pedicure",
    "gel",
    "acrylic",
    "dip powder",
    "nail art",
    "wax",
    "facial",
    "eyebrow",
    "lash",
    "spa",
)

#: Known city names, taken from fixtures/locations.json + fixtures/brand_profiles.json.
#: A city in the draft must appear in the review OR be the profile's verified
#: city (has_city) or it fails grounding.
KNOWN_CITIES = (
    "garden grove",
    "san jose",
    "houston",
    "dallas",
    "westminster",
    "atlanta",
)

#: Known state/region codes from the same fixtures (matched case-sensitively as
#: whole uppercase tokens so "CA" never matches inside an unrelated word).
KNOWN_STATES = ("CA", "TX", "GA")

_PRICE_RE = re.compile(
    r"(\$\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*\s?(?:dollars?|usd|đ|vnd)|€\s?\d+|£\s?\d+)",
    re.IGNORECASE,
)

_PROMISE_RE = re.compile(
    r"\b(discount|% ?off|\d{1,3} ?% off|refund|reimburse|free of charge|for free|"
    r"complimentary|voucher|coupon|on us|make it right for free|"
    r"hoàn tiền|giảm giá|miễn phí)\b"
)
_RANKING_RE = re.compile(
    r"\b(google ranking|rank(ing)? (higher|better|up)|improve (our|your) ranking|"
    r"thứ hạng|top of google|seo boost|search ranking)\b"
)
_AI_ARTIFACT_RE = re.compile(r"(\{\{|\[insert|as an ai|lorem ipsum|placeholder|xxx)", re.I)
_PII_DRAFT_RE = re.compile(
    r"(\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b|[\w.+-]+@[\w-]+\.[\w.]{2,})"
)
#: sentence-initial or harmless capitalised words that are never fabrication.
_SAFE_CAPS = {"google", "maps", "i", "we", "our", "you", "thank", "thanks", "hello", "hi"}


@dataclass
class CheckResult:
    name: str
    status: str = PASS
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.status != FAIL

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "passed": self.passed, "reason": self.reason}


@dataclass
class ValidationResult:
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> List[CheckResult]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> List[CheckResult]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        return not self.failed and len(self.warnings) < 2

    def by_name(self, name: str) -> Optional[CheckResult]:
        for check in self.checks:
            if check.name == name:
                return check
        return None

    def reason(self) -> str:
        parts = [f"{c.name}:{c.reason}" for c in self.checks if c.status != PASS]
        return "; ".join(parts) if parts else "ok"

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "failed": [c.name for c in self.failed],
            "warnings": [c.name for c in self.warnings],
        }


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return getattr(row, key, default)


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"[.!?\n]+", text or "") if s.strip()]


def _proper_nouns(text: str) -> List[str]:
    """Capitalised tokens that are not the first word of a sentence."""
    out: List[str] = []
    for sentence in _sentences(text):
        tokens = re.findall(r"[^\W\d_]+", sentence, re.UNICODE)
        for token in tokens[1:]:
            if token[:1].isupper() and len(token) >= 3:
                out.append(token)
    return out


def _allowed_service_terms(brand: BrandProfile, review: Any) -> set:
    allowed = {s.strip().lower() for s in (brand.services or [])}
    comment = str(_get(review, "comment") or "").lower()
    for service in FABRICATION_SERVICES:
        if service in comment:
            allowed.add(service)
    return allowed


def _mentions_unverified_service(draft: str, brand: BrandProfile, review: Any) -> Optional[str]:
    allowed = _allowed_service_terms(brand, review)
    lowered = draft.lower()
    for service in FABRICATION_SERVICES:
        if service in lowered and service not in allowed:
            return service
    return None


def _length_limit(classification: Classification) -> int:
    """Max sentences allowed: 3 for an ordinary positive review, else 4."""
    stars = classification.stars
    if stars in (4, 5) and not classification.is_sensitive:
        return 3
    return 4


def _grounding_issues(draft: str, brand: BrandProfile, review: Any) -> List[str]:
    """Return ungrounded city/state/service/staff-name/price mentions.

    An item is grounded when it appears in the review text OR the brand profile
    exposes it with the matching completeness flag set.
    """
    issues: List[str] = []
    draft_lower = (draft or "").lower()
    review_text = str(_get(review, "comment") or "")
    review_lower = review_text.lower()

    city = (brand.city or "").strip().lower()
    for known in KNOWN_CITIES:
        if known in draft_lower:
            if known in review_lower:
                continue
            if brand.has_city and known == city:
                continue
            issues.append(f"city '{known}' is not grounded")

    state = (brand.state or "").strip().lower()
    for known in KNOWN_STATES:
        pattern = r"\b" + re.escape(known) + r"\b"
        if re.search(pattern, draft or ""):
            if re.search(pattern, review_text, re.IGNORECASE):
                continue
            if brand.has_state and known.lower() == state:
                continue
            issues.append(f"state '{known}' is not grounded")

    allowed_services = (
        {s.strip().lower() for s in (brand.services or [])} if brand.has_services else set()
    )
    for service in FABRICATION_SERVICES:
        if service in draft_lower and service not in review_lower and service not in allowed_services:
            issues.append(f"service '{service}' is not grounded")

    allowed_caps = set(_SAFE_CAPS)
    allowed_caps |= {t.lower() for t in normalize_tokens(brand.business_name or "")}
    allowed_caps |= {t.lower() for t in normalize_tokens(brand.city or "")}
    allowed_caps |= {t.lower() for t in normalize_tokens(brand.state or "")}
    allowed_caps |= {t.lower() for t in normalize_tokens(brand.category or "")}
    allowed_caps |= {t.lower() for t in normalize_tokens(" ".join(brand.seo_keywords or []))}
    allowed_caps |= {t.lower() for t in normalize_tokens(review_text)}
    for noun in _proper_nouns(draft or ""):
        if noun.lower() not in allowed_caps:
            issues.append(f"name '{noun}' is not grounded")

    price = _PRICE_RE.search(draft or "")
    if price:
        issues.append(f"price '{price.group(0)}' is not grounded")

    return issues


def validate(
    draft: str,
    review: Any,
    brand: BrandProfile,
    recent_replies: Optional[Sequence[Any]] = None,
    classification: Optional[Classification] = None,
    dup_checker: Optional[DuplicateChecker] = None,
) -> ValidationResult:
    """Run all 7 checks and return a :class:`ValidationResult`."""
    draft = (draft or "").strip()
    brand = brand or BrandProfile.neutral()
    classification = classification or Classification()
    recent_replies = list(recent_replies or [])
    dup_checker = dup_checker or DuplicateChecker()
    checks: List[CheckResult] = []

    # 1. relevance ------------------------------------------------------ #
    if not draft:
        checks.append(CheckResult("relevance", FAIL, "empty draft"))
    else:
        draft_tokens = set(normalize_tokens(draft))
        review_tokens = [t for t in normalize_tokens(str(_get(review, "comment") or "")) if len(t) >= 4]
        if review_tokens:
            review_set = set(review_tokens)
            if draft_tokens & review_set:
                checks.append(CheckResult("relevance", PASS, "echoes a detail from the review"))
            else:
                checks.append(CheckResult("relevance", FAIL, "does not reference any detail of the review"))
        else:
            comments = _get(review, "comment")
            has_comment = bool(str(comments or "").strip())
            if has_comment:
                checks.append(CheckResult("relevance", PASS, "short review, nothing specific to echo"))
            else:
                checks.append(CheckResult("relevance", PASS, "rating-only review"))

    # 2. brand_fit ------------------------------------------------------ #
    banned_hits = [
        term
        for term in (brand.do_not_mention or [])
        if term and str(term).strip().lower() in draft.lower()
    ]
    if banned_hits:
        checks.append(CheckResult("brand_fit", FAIL, f"mentions do-not-mention term(s): {banned_hits}"))
    else:
        checks.append(CheckResult("brand_fit", PASS, "no banned terms"))

    # 3. naturalness ---------------------------------------------------- #
    artifacts = _AI_ARTIFACT_RE.search(draft)
    tokens = normalize_tokens(draft)
    if artifacts:
        checks.append(CheckResult("naturalness", FAIL, f"template artifact: {artifacts.group(0)!r}"))
    elif len(tokens) < 4:
        checks.append(CheckResult("naturalness", FAIL, "too short to read naturally"))
    elif draft.count("!") >= 4:
        checks.append(CheckResult("naturalness", WARN, "excessive exclamation marks"))
    elif len(set(_sentences(draft))) < len(_sentences(draft)):
        checks.append(CheckResult("naturalness", FAIL, "repeated identical sentence"))
    else:
        checks.append(CheckResult("naturalness", PASS, "reads naturally"))

    # 4. local_seo ------------------------------------------------------ #
    seo_issues = keyword_density_issues(draft, brand)
    if seo_issues:
        checks.append(CheckResult("local_seo", FAIL, "; ".join(seo_issues)))
    else:
        checks.append(CheckResult("local_seo", PASS, "keyword use within limits"))

    # 5. no_fabrication ------------------------------------------------- #
    fabrications: List[str] = []
    bad_service = _mentions_unverified_service(draft, brand, review)
    if bad_service:
        fabrications.append(f"unverified service '{bad_service}'")
    promise = _PROMISE_RE.search(draft.lower())
    if promise:
        fabrications.append(f"promises '{promise.group(0)}'")
    review_text = str(_get(review, "comment") or "").lower()
    allowed_caps = set(_SAFE_CAPS)
    allowed_caps |= {t.lower() for t in normalize_tokens(brand.business_name or "")}
    allowed_caps |= {t.lower() for t in normalize_tokens(brand.city or "")}
    allowed_caps |= {t.lower() for t in normalize_tokens(brand.state or "")}
    allowed_caps |= {t.lower() for t in normalize_tokens(review_text)}
    for noun in _proper_nouns(draft):
        if noun.lower() not in allowed_caps:
            fabrications.append(f"unverified name '{noun}'")
    if fabrications:
        checks.append(CheckResult("no_fabrication", FAIL, "; ".join(sorted(set(fabrications)))))
    else:
        checks.append(CheckResult("no_fabrication", PASS, "every claim traces to review or brand"))

    # 6. duplication ---------------------------------------------------- #
    location_id = _get(review, "location_id")
    dup = dup_checker.check(draft, recent_replies, location_id=location_id, brand=None)
    if dup.status == FAIL:
        checks.append(CheckResult("duplication", FAIL, dup.reason()))
    elif dup.status == WARN:
        checks.append(CheckResult("duplication", WARN, dup.reason()))
    else:
        checks.append(CheckResult("duplication", PASS, "no near-duplicate found"))

    # 7. approval_compliance -------------------------------------------- #
    safety_issues: List[str] = []
    pii = _PII_DRAFT_RE.search(draft)
    if pii:
        safety_issues.append(f"reveals PII '{pii.group(0)}'")
    ranking = _RANKING_RE.search(draft.lower())
    if ranking:
        safety_issues.append(f"claims ranking effect '{ranking.group(0)}'")
    if classification.is_sensitive and (promise or re.search(r"\b(you are wrong|not true|fake review|liar)\b", draft.lower())):
        safety_issues.append("argumentative/deal-making reply on a sensitive case")
    if safety_issues:
        checks.append(CheckResult("approval_compliance", FAIL, "; ".join(safety_issues)))
    else:
        checks.append(CheckResult("approval_compliance", PASS, "safe for the approval policy"))

    # 8. length_ok ------------------------------------------------------ #
    limit = _length_limit(classification)
    sentence_count = len(_sentences(draft))
    if sentence_count > limit:
        checks.append(
            CheckResult(
                "length_ok",
                FAIL,
                f"{sentence_count} sentences exceeds the {limit}-sentence limit",
            )
        )
    else:
        checks.append(CheckResult("length_ok", PASS, f"{sentence_count} sentences within {limit}"))

    # 9. grounding ------------------------------------------------------ #
    grounding = _grounding_issues(draft, brand, review)
    if grounding:
        checks.append(CheckResult("grounding", FAIL, "; ".join(sorted(set(grounding)))))
    else:
        checks.append(CheckResult("grounding", PASS, "every mention traces to review or brand"))

    return ValidationResult(checks=checks)
