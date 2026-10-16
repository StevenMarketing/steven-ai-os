"""The draft state machine for the approval workflow.

States and the transitions the workflow service is allowed to perform. This is
the single source of truth used by :class:`gbp.workflow.service.ApprovalService`.

Note on ``draft -> approved``: the reply engine persists a clean, auto-draftable
reply as ``draft`` and a manual-approval case as ``needs_review``. A human may
approve either one, so ``draft`` lists ``approved`` as a legal target here. The
stricter "no shortcut" wording in docs/DOMAIN-RULES.md §5 applies to the engine's
own auto-promotion, not to an explicit human approval of an auto-drafted reply.
"""

from __future__ import annotations

from typing import Dict, FrozenSet

__all__ = [
    "STATES",
    "ALLOWED",
    "InvalidTransition",
    "assert_transition",
    "may_publish",
]


class InvalidTransition(Exception):
    """Raised when ``src -> dst`` is not a legal state transition."""

    def __init__(self, src: str, dst: str):
        self.src = src
        self.dst = dst
        super().__init__(f"invalid transition: {src!r} -> {dst!r}")


#: Every state a ``reply_drafts`` row may hold in the workflow.
STATES = (
    "draft",
    "needs_review",
    "approved",
    "rejected",
    "ready_to_publish",
    "published",
    "failed",
)

#: Allowed transitions, ``src -> {dst, ...}``.
ALLOWED: Dict[str, FrozenSet[str]] = {
    "draft": frozenset(
        {"needs_review", "approved", "ready_to_publish", "rejected", "failed"}
    ),
    "needs_review": frozenset(
        {"approved", "ready_to_publish", "rejected", "draft", "failed"}
    ),
    "rejected": frozenset({"needs_review", "draft", "failed"}),
    "approved": frozenset({"ready_to_publish", "published", "rejected", "failed"}),
    "ready_to_publish": frozenset({"published", "approved", "failed"}),
    "published": frozenset(),
    "failed": frozenset({"draft", "needs_review"}),
}


def assert_transition(src: str, dst: str) -> None:
    """Raise :class:`InvalidTransition` unless ``src -> dst`` is allowed.

    Unknown source states are treated as having no outgoing transitions.
    """
    if dst not in ALLOWED.get(src, frozenset()):
        raise InvalidTransition(src, dst)


def may_publish(state: str) -> bool:
    """True only for ``approved`` / ``ready_to_publish``."""
    return state in ("approved", "ready_to_publish")
