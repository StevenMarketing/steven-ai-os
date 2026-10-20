"""Multi-tenant scoping and the unanswered-review view for GReply (Issue #71, increment 1).

GReply serves many nail salons from one account, so every read must be scoped to exactly one
location and must never leak another salon's data. This module is the single place that decides
which reviews belong to which salon and which of them still need an answer; it reuses the value
objects in :mod:`gbp.replies.models` rather than inventing a second model.

Nothing here touches the network: it operates on the synthetic fixture shape used by the
offline sync (`dataset["locations"][i]["reviews"][j]`) and on reply records that carry a
`review_id`, `location_id` and `state`.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "location_ids",
    "reviews_for_location",
    "replies_for_location",
    "is_answered",
    "unanswered_reviews",
    "newest_first",
    "select_reviews",
    "location_summary",
]

#: States that mean the review already has a reply the owner accepted or intends to publish.
ANSWERED_STATES = ("approved", "publish-intent", "sent")


def location_ids(dataset: Dict[str, Any]) -> List[str]:
    """Every location id in the dataset, in a stable order."""
    entries = (dataset or {}).get("locations") or []
    ids = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        location = entry.get("location")
        if isinstance(location, str) and location:
            ids.append(location)
    return sorted(ids)


def reviews_for_location(dataset: Dict[str, Any], location: str) -> List[Dict[str, Any]]:
    """Only the reviews of ``location``; an unknown or empty location yields nothing.

    The dataset may legitimately carry a location without reviews, and a caller may ask for a
    location that does not exist. Both cases must return an empty list -- never every review.
    """
    if not isinstance(location, str) or not location:
        return []
    entries = (dataset or {}).get("locations") or []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("location") != location:
            continue
        reviews = entry.get("reviews") or []
        return [r for r in reviews if isinstance(r, dict)]
    return []


def replies_for_location(replies: Iterable[Any], location: str) -> List[Any]:
    """Only the replies that belong to ``location`` (by ``location_id``)."""
    if not isinstance(location, str) or not location:
        return []
    kept = []
    for reply in replies or ():
        if getattr(reply, "location_id", None) == location:
            kept.append(reply)
    return kept


def is_answered(review: Dict[str, Any], replies: Sequence[Any]) -> bool:
    """True when this review already has a reply from *its own* location.

    A reply recorded for a different location must never mark this review answered, and a
    reply that was edited back to ``draft`` means the review needs attention again.
    """
    review_id = review.get("reviewId") or review.get("review_id")
    if not review_id:
        return False
    location = review.get("_location")
    for reply in replies or ():
        if getattr(reply, "review_id", None) != review_id:
            continue
        reply_location = getattr(reply, "location_id", None)
        if location and reply_location and reply_location != location:
            continue
        if str(getattr(reply, "state", "")) in ANSWERED_STATES:
            return True
    return False


def unanswered_reviews(dataset: Dict[str, Any], location: str,
                       replies: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    """Reviews of one location that still need an answer."""
    scoped = reviews_for_location(dataset, location)
    for review in scoped:
        review.setdefault("_location", location)
    own = replies_for_location(replies, location)
    return [r for r in scoped if not is_answered(r, own)]


def _created(review: Dict[str, Any]) -> str:
    value = review.get("createTime") or review.get("create_time") or ""
    return value if isinstance(value, str) else ""


def newest_first(reviews: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Newest review first, using the ISO-8601 timestamp the fixture already carries.

    Reviews without a usable timestamp sort last but keep their relative order, so a malformed
    fixture can never make a review disappear from the view.
    """
    indexed: List[Tuple[int, int, str, Dict[str, Any]]] = []
    for position, review in enumerate(reviews):
        stamp = _created(review)
        indexed.append((0 if stamp else 1, position, stamp, review))
    indexed.sort(key=lambda item: (item[0], _neg_key(item[2]), item[1]))
    return [item[3] for item in indexed]


def _neg_key(stamp: str) -> str:
    """Sort key that reverses ISO-8601 string order (newest first) without parsing dates."""
    return "".join(chr(255 - ord(ch)) for ch in stamp) if stamp else ""


def select_reviews(dataset: Dict[str, Any], location: str, *, unanswered_only: bool = True,
                   replies: Sequence[Any] = (), limit: Optional[int] = None,
                   newest: bool = True) -> List[Dict[str, Any]]:
    """The query the salon switcher needs: one location, unanswered by default, newest first."""
    chosen = (unanswered_reviews(dataset, location, replies) if unanswered_only
              else reviews_for_location(dataset, location))
    if newest:
        chosen = newest_first(chosen)
    if isinstance(limit, int) and limit >= 0:
        chosen = chosen[:limit]
    return chosen


def location_summary(dataset: Dict[str, Any], replies: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    """Per-salon counters for the dashboard: total reviews and how many still need an answer."""
    summary = []
    for location in location_ids(dataset):
        scoped = reviews_for_location(dataset, location)
        pending = unanswered_reviews(dataset, location, replies)
        newest = max((_created(r) for r in scoped), default="")
        summary.append({"location": location, "reviews": len(scoped),
                        "unanswered": len(pending), "newest_review": newest})
    return summary
