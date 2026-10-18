"""Synthetic review/reply fixtures for the offline reply workflow.

Everything here is fake ("Synthetic Guest A", invented comments). The tests
import :data:`SYNTHETIC_REVIEWS` (also mirrored in
``tests/fixtures/replies_offline.json``) so no real customer data and no network
access are ever involved.

:func:`validate_review` refuses malformed fixtures *before* any state is
written, so a bad fixture can never cause a partial write.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .states import STATES

__all__ = [
    "FixtureError",
    "SYNTHETIC_REVIEWS",
    "validate_review",
    "parse_reviews",
]


class FixtureError(ValueError):
    """A synthetic fixture is malformed and must be refused."""


#: Canonical synthetic fixtures. Deterministic order, no real data.
SYNTHETIC_REVIEWS: List[Dict[str, Any]] = [
    {
        "review_id": "rev-1001",
        "location_id": "accounts/1001/locations/loc-001",
        "reviewer": "Synthetic Guest A",
        "rating": 5,
        "comment": "loved my gel manicure the staff were so friendly",
    },
    {
        "review_id": "rev-1002",
        "location_id": "accounts/1001/locations/loc-005",
        "reviewer": "Synthetic Guest B",
        "rating": 2,
        "comment": "waited a long time for my pedicure",
    },
]


def validate_review(raw: Any) -> Dict[str, Any]:
    """Return a normalized copy of ``raw`` or raise :class:`FixtureError`.

    Refuses (without writing anything): a non-mapping, a missing/empty review
    id or location id, empty review text, a rating outside 1..5, and an unknown
    starting state.
    """
    if not isinstance(raw, dict):
        raise FixtureError("review fixture must be an object")

    review_id = str(raw.get("review_id") or "").strip()
    if not review_id:
        raise FixtureError("review fixture is missing a review_id")

    location_id = str(raw.get("location_id") or "").strip()
    if not location_id:
        raise FixtureError(f"review {review_id!r} is missing a location_id")

    comment = str(raw.get("comment") or "").strip()
    if not comment:
        raise FixtureError(f"review {review_id!r} has empty review text")

    rating = raw.get("rating")
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        raise FixtureError(f"review {review_id!r} has a non-numeric rating")
    if not 1 <= rating <= 5:
        raise FixtureError(f"review {review_id!r} rating must be 1..5")

    state = raw.get("state", "draft")
    if state not in STATES:
        raise FixtureError(f"review {review_id!r} has unknown state {state!r}")

    normalized = dict(raw)
    normalized.update(
        review_id=review_id,
        location_id=location_id,
        comment=comment,
        rating=rating,
        state=state,
        reviewer=str(raw.get("reviewer") or "").strip(),
    )
    return normalized


def parse_reviews(raw: Any) -> List[Dict[str, Any]]:
    """Validate a whole collection atomically.

    Either every review validates and is returned, or a :class:`FixtureError`
    is raised and *nothing* is returned -- no partial write.
    """
    if not isinstance(raw, (list, tuple)):
        raise FixtureError("review collection must be a list")
    return [validate_review(item) for item in raw]
