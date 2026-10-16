"""Duplicate / boilerplate detection and keyword-density checks.

Scope rule (docs/DOMAIN-RULES.md §3): comparisons are made ONLY against the
previous replies of the SAME ``location_id``. Metrics:

* exact duplicate match,
* max Jaccard similarity over 4-grams (warn > 0.35, fail > 0.50),
* repeated 8-word prefix (fail regardless of Jaccard),
* SEO keyword density (<= 2 keyword hits, business name / city at most once).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Sequence

__all__ = [
    "DuplicateResult",
    "DuplicateChecker",
    "normalize_tokens",
    "ngrams",
    "jaccard",
    "keyword_density_issues",
    "PASS",
    "WARN",
    "FAIL",
]

PASS = "pass"
WARN = "warn"
FAIL = "fail"

WARN_THRESHOLD = 0.35
FAIL_THRESHOLD = 0.50
NGRAM_SIZE = 4
PREFIX_WORDS = 8
MAX_KEYWORDS = 2

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_tokens(text: str) -> List[str]:
    """Lower-case word tokens with punctuation stripped (diacritics kept)."""
    return [t.lower() for t in _WORD_RE.findall(text or "")]


def ngrams(tokens: Sequence[str], size: int = NGRAM_SIZE) -> set:
    if size <= 0 or len(tokens) < size:
        return set()
    return {tuple(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _text_of(item: Any) -> str:
    """Extract reply text from a dict / sqlite3.Row / str."""
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("draft_text", "reply_text", "text"):
            if item.get(key):
                return str(item[key])
        return ""
    for key in ("draft_text", "reply_text", "text"):
        try:
            value = item[key]
        except (TypeError, KeyError, IndexError):
            value = getattr(item, key, None)
        if value:
            return str(value)
    return ""


def _location_of(item: Any) -> Optional[Any]:
    if item is None or isinstance(item, str):
        return None
    if isinstance(item, dict):
        return item.get("location_id")
    try:
        return item["location_id"]
    except (TypeError, KeyError, IndexError):
        return getattr(item, "location_id", None)


def _count_occurrences(haystack_lower: str, needle: str) -> int:
    needle = (needle or "").strip().lower()
    if not needle:
        return 0
    return haystack_lower.count(needle)


def keyword_density_issues(text: str, brand: Any) -> List[str]:
    """Return SEO problems in ``text`` for the given brand profile.

    Flags the doc's §2.2 rules: more than ``MAX_KEYWORDS`` keyword hits, two
    keywords packed into one sentence, or a repeated business name / city.
    """
    issues: List[str] = []
    lowered = (text or "").lower()
    if not lowered.strip():
        return issues

    keywords = list(getattr(brand, "seo_keywords", []) or [])
    hits = 0
    for keyword in keywords:
        kw = str(keyword).strip().lower()
        if kw and kw in lowered:
            hits += _count_occurrences(lowered, kw)
    if hits > MAX_KEYWORDS:
        issues.append(f"keyword_stuffing: {hits} SEO keyword hits (max {MAX_KEYWORDS})")

    # Two or more distinct keywords inside the same sentence.
    for sentence in re.split(r"[.!?\n]+", lowered):
        distinct = sum(1 for kw in keywords if str(kw).strip().lower() and str(kw).strip().lower() in sentence)
        if distinct >= 2:
            issues.append("keyword_stacking: >=2 SEO keywords in one sentence")
            break

    business_name = str(getattr(brand, "business_name", "") or "").strip().lower()
    if business_name and _count_occurrences(lowered, business_name) > 1:
        issues.append("business_name_repeated: more than once")

    city = str(getattr(brand, "city", "") or "").strip().lower()
    if city and _count_occurrences(lowered, city) > 1:
        issues.append("city_repeated: more than once")

    return issues


@dataclass
class DuplicateResult:
    status: str = PASS
    max_similarity: float = 0.0
    checked: int = 0
    reasons: List[str] = field(default_factory=list)
    matched_index: Optional[int] = None

    @property
    def is_fail(self) -> bool:
        return self.status == FAIL

    @property
    def is_warn(self) -> bool:
        return self.status == WARN

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "max_similarity": round(self.max_similarity, 4),
            "checked": self.checked,
            "reasons": list(self.reasons),
            "matched_index": self.matched_index,
        }

    def reason(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "ok"


class DuplicateChecker:
    """Compares a draft against the recent replies of the same location."""

    warn_threshold = WARN_THRESHOLD
    fail_threshold = FAIL_THRESHOLD

    def check(
        self,
        draft: str,
        recent_replies: Optional[Iterable[Any]] = None,
        location_id: Optional[Any] = None,
        brand: Any = None,
    ) -> DuplicateResult:
        draft = (draft or "").strip()
        result = DuplicateResult()
        if not draft:
            result.status = FAIL
            result.reasons.append("empty_draft")
            return result

        draft_norm = " ".join(normalize_tokens(draft))
        draft_tokens = normalize_tokens(draft)
        draft_grams = ngrams(draft_tokens)
        prefix = draft_tokens[:PREFIX_WORDS]

        best = 0.0
        for index, item in enumerate(recent_replies or []):
            if location_id is not None:
                item_location = _location_of(item)
                if item_location is not None and str(item_location) != str(location_id):
                    continue  # scope: same location only
            text = _text_of(item).strip()
            if not text:
                continue
            result.checked += 1

            if " ".join(normalize_tokens(text)) == draft_norm:
                result.status = FAIL
                result.max_similarity = 1.0
                result.matched_index = index
                result.reasons.append(f"exact_duplicate:reply#{index}")
                return result

            other_tokens = normalize_tokens(text)
            similarity = jaccard(draft_grams, ngrams(other_tokens))
            if similarity > best:
                best = similarity
                result.matched_index = index

            if len(prefix) == PREFIX_WORDS and other_tokens[:PREFIX_WORDS] == prefix:
                result.reasons.append(f"repeated_prefix_8:reply#{index}")
                result.status = FAIL
                result.max_similarity = max(best, similarity)
                return result

        result.max_similarity = best
        if best > self.fail_threshold:
            result.status = FAIL
            result.reasons.append(f"jaccard>{self.fail_threshold}")
        elif best > self.warn_threshold:
            result.status = WARN
            result.reasons.append(f"{self.warn_threshold}<jaccard<={self.fail_threshold}")

        if brand is not None:
            for issue in keyword_density_issues(draft, brand):
                result.reasons.append(issue)
                result.status = FAIL
        return result
