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
    "publish-intent",
    "published",
    "failed",
    "sent",
)

#: Allowed transitions, ``src -> {dst, ...}``.
#:
#: Issue #126 adds exactly two states and three edges:
#: ``approved -> publish-intent`` / ``ready_to_publish -> publish-intent``
#: (only from a state a human already approved) and the terminal
#: ``publish-intent -> sent``. ``sent`` is reachable ONLY from
#: ``publish-intent``, so no other state can ever claim a reply was posted.
ALLOWED: Dict[str, FrozenSet[str]] = {
    "draft": frozenset(
        {"needs_review", "approved", "ready_to_publish", "rejected", "failed"}
    ),
    "needs_review": frozenset(
        {"approved", "ready_to_publish", "rejected", "draft", "failed"}
    ),
    "rejected": frozenset({"needs_review", "draft", "failed"}),
    "approved": frozenset(
        {"ready_to_publish", "publish-intent", "published", "rejected", "failed"}
    ),
    "ready_to_publish": frozenset(
        {"publish-intent", "published", "approved", "failed"}
    ),
    #: A human approved AND requested publish; the ONLY state a live publish
    #: may start from, and the only one that may reach ``sent``.
    "publish-intent": frozenset({"sent"}),
    "published": frozenset(),
    "failed": frozenset({"draft", "needs_review"}),
    #: Terminal: the reply really was posted to Google.
    "sent": frozenset(),
}


def assert_transition(src: str, dst: str) -> None:
    """Raise :class:`InvalidTransition` unless ``src -> dst`` is allowed.

    Unknown source states are treated as having no outgoing transitions.
    """
    if dst not in ALLOWED.get(src, frozenset()):
        raise InvalidTransition(src, dst)


def may_publish(state: str) -> bool:
    """True only for ``approved`` / ``ready_to_publish``.

    Deliberately UNCHANGED by Issue #126: ``publish_guard`` stays the dry-run
    it always was, so a ``publish-intent`` row is still refused by the guard.
    """
    return state in ("approved", "ready_to_publish")
