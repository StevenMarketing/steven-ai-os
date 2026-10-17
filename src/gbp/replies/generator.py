"""Pluggable draft generators.

The default :class:`DeterministicDraftGenerator` is fully offline and pure: the
same review always yields the same draft, with no model and no network call. A
model-backed generator can be injected by satisfying the :class:`DraftGenerator`
protocol, but it is never required.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable

__all__ = ["DraftGenerator", "DeterministicDraftGenerator"]


@runtime_checkable
class DraftGenerator(Protocol):
    """Anything that can turn a review into a first-draft reply text."""

    def draft(self, review: Dict[str, Any]) -> str:  # pragma: no cover - protocol
        ...


class DeterministicDraftGenerator:
    """Offline default: deterministic, grounded in the review, no network."""

    def draft(self, review: Dict[str, Any]) -> str:
        reviewer = str(review.get("reviewer") or "").strip() or "there"
        rating = review.get("rating")
        comment = str(review.get("comment") or "").strip()

        try:
            positive = int(rating) >= 4
        except (TypeError, ValueError):
            positive = False

        if positive:
            opener = f"Thank you so much for the kind words, {reviewer}!"
        else:
            opener = f"Thank you for taking the time to share this, {reviewer}."

        detail = ""
        words: List[str] = comment.split()
        if words:
            snippet = " ".join(words[:6])
            detail = f" We appreciate that you mentioned {snippet}."

        return f"{opener}{detail} We hope to welcome you back soon."
