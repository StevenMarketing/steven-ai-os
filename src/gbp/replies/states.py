"""The reply lifecycle state machine for the offline draft/edit/approve workflow.

This is a *separate*, stricter state machine from ``gbp.workflow.states``: it
models the human-facing reply lifecycle required by issue #34:

    ``draft`` -> ``edited`` -> ``approved`` -> ``publish-intent``

Every transition is validated here before it is applied. ``publish-intent`` is a
terminal bookkeeping state: it records that a human approved the exact text and a
publish was *requested*, but the live publish action itself stays disabled (see
:mod:`gbp.replies.workflow`).
"""

from __future__ import annotations

from typing import Dict, FrozenSet

__all__ = [
    "STATES",
    "ALLOWED",
    "InvalidTransition",
    "assert_transition",
    "can_transition",
]


class InvalidTransition(Exception):
    """Raised when ``src -> dst`` is not a legal transition."""

    def __init__(self, src: str, dst: str):
        self.src = src
        self.dst = dst
        super().__init__(f"invalid transition: {src!r} -> {dst!r}")


#: Every state a reply may hold in this workflow.
STATES = ("draft", "edited", "approved", "publish-intent")

#: Allowed transitions, ``src -> {dst, ...}``.
#:
#: ``draft -> approved`` is allowed so a human may approve a freshly generated
#: draft without forcing an edit first. ``edited -> edited`` allows repeated
#: edits pre-approval. ``approved -> draft`` is the invalidation edge applied
#: when the approved text is edited afterwards.
ALLOWED: Dict[str, FrozenSet[str]] = {
    "draft": frozenset({"edited", "approved"}),
    "edited": frozenset({"edited", "approved"}),
    "approved": frozenset({"draft", "publish-intent"}),
    "publish-intent": frozenset(),
}


def can_transition(src: str, dst: str) -> bool:
    """True when ``src -> dst`` is allowed. Unknown sources have no edges."""
    return dst in ALLOWED.get(src, frozenset())


def assert_transition(src: str, dst: str) -> None:
    """Raise :class:`InvalidTransition` unless ``src -> dst`` is allowed."""
    if not can_transition(src, dst):
        raise InvalidTransition(src, dst)
