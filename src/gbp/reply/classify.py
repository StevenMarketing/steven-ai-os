"""Review classification: star parsing, rating-only detection, sensitivity.

Pure functions, standard library only. ``classify`` accepts a dict,
``sqlite3.Row`` or a :class:`gbp.models.Review` dataclass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

__all__ = ["Classification", "classify", "STAR_MAP", "SIGNAL_RULES"]

#: GBP returns ``starRating`` as a word.
STAR_MAP = {
    "ONE": 1,
    "TWO": 2,
    "THREE": 3,
    "FOUR": 4,
    "FIVE": 5,
}

#: signal name -> compiled regex over the lower-cased comment.
SIGNAL_RULES = [
    (
        "health",
        re.compile(
            r"\b(infection|infected|bleed\w*|burn\w*|unsanitary|unsterili\w*|"
            r"sterili\w*|dirty (tools|towel|equipment)|fungus|fungal|hospital|"
            r"doctor|allergic reaction|medical|hygien\w*)\b"
        ),
    ),
    (
        "discrimination",
        re.compile(
            r"\b(racist|racism|discriminat\w*|sexi\w*|homophob\w*|"
            r"ignored me|only served|because of my (race|accent|language))\b"
        ),
    ),
    (
        "threat_abuse",
        re.compile(
            r"\b(kill|destroy|threat\w*|idiot|stupid|moron|scum|"
            r"f\*\*\*|i will come back|hate you)\b"
        ),
    ),
    (
        "legal_refund",
        re.compile(
            r"\b(sue|lawsuit|lawyer|attorney|legal action|refund|reimburse|"
            r"charge ?back|small claims|state board|health department|"
            r"\bbbb\b|complaint)\b"
        ),
    ),
    (
        "pii",
        re.compile(
            r"(\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"
            r"|[\w.+-]+@[\w-]+\.[\w.]{2,}"
            r"|\b\d{1,5}\s+\w+\s+(street|st|avenue|ave|road|rd|blvd|lane|ln)\b)"
        ),
    ),
    (
        "spam",
        re.compile(
            r"(https?://|www\.|\.example|\d{1,3}\s?%\s?off|coupon|"
            r"promo code|visit us at|best in town)"
        ),
    ),
]

#: explicit fixture ``sensitiveCategory`` -> canonical signal name.
CATEGORY_SIGNALS = {
    "health_safety": "health",
    "discrimination": "discrimination",
    "threat_abuse": "threat_abuse",
    "legal_financial": "legal_refund",
    "pii": "pii",
    "spam_competitor": "spam",
}


@dataclass
class Classification:
    """Outcome of classifying one review."""

    stars: Optional[int] = None
    is_rating_only: bool = False
    is_sensitive: bool = False
    signals: List[str] = field(default_factory=list)
    requires_manual_approval: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "stars": self.stars,
            "is_rating_only": self.is_rating_only,
            "is_sensitive": self.is_sensitive,
            "signals": list(self.signals),
            "requires_manual_approval": self.requires_manual_approval,
            "reason": self.reason,
        }


def _get(row: Any, key: str, default: Any = None) -> Any:
    """Field access that works for dicts, sqlite3.Row and dataclasses."""
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return getattr(row, key, default)


def parse_stars(review: Any) -> Optional[int]:
    """Parse ``starRating`` ("ONE".."FIVE") or a numeric ``rating``."""
    raw = _get(review, "starRating")
    if raw is None:
        raw = _get(review, "rating")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = int(raw)
        return value if 1 <= value <= 5 else None
    text = str(raw).strip().upper()
    if text in STAR_MAP:
        return STAR_MAP[text]
    if text.isdigit():
        value = int(text)
        return value if 1 <= value <= 5 else None
    return None


def detect_signals(comment: str) -> List[str]:
    """Return the sensitive signals present in ``comment`` (dedup, ordered)."""
    text = (comment or "").lower()
    if not text.strip():
        return []
    signals: List[str] = []
    for name, pattern in SIGNAL_RULES:
        if pattern.search(text) and name not in signals:
            signals.append(name)
    return signals


def classify(review: Any) -> Classification:
    """Classify one review.

    ``requires_manual_approval`` is True when the rating is 1-2 stars, when any
    sensitivity signal fires, or for a rating-only review of <= 4 stars.
    """
    stars = parse_stars(review)
    comment = _get(review, "comment") or ""
    comment = str(comment).strip()
    is_rating_only = comment == ""

    signals = detect_signals(comment)
    category = str(_get(review, "sensitiveCategory") or "").strip().lower()
    mapped = CATEGORY_SIGNALS.get(category)
    if mapped and mapped not in signals:
        signals.append(mapped)

    is_sensitive = bool(signals)

    effective_stars = stars if stars is not None else 0
    requires_manual = (
        effective_stars <= 2
        or is_sensitive
        or (is_rating_only and effective_stars <= 4)
    )

    reasons: List[str] = []
    if stars is None:
        reasons.append("unparsed_rating")
    if effective_stars <= 2:
        reasons.append("low_stars")
    if is_rating_only:
        reasons.append("rating_only")
    if is_sensitive:
        reasons.append("sensitive:" + ",".join(signals))

    return Classification(
        stars=stars,
        is_rating_only=is_rating_only,
        is_sensitive=is_sensitive,
        signals=signals,
        requires_manual_approval=requires_manual,
        reason=";".join(reasons),
    )
