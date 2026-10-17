"""Offline sync -> reply-workflow integration (fixtures only, no network).

This package wires the offline review sync (:mod:`gbp.sync.fixture_sync`) into
the offline reply/approval workflow (:mod:`gbp.workflow.service`) so a synced
review can be drafted, edited and approved end to end while the human-approval
gate and the *disabled* publish path are preserved across the whole chain.

Nothing here calls Google, publishes a reply, or reaches the network. The
publish step is a refusal by design.
"""

from __future__ import annotations

from .chain import (  # noqa: F401
    ChainError,
    OfflineGuard,
    ReviewReplyChain,
    run_chain,
)

__all__ = [
    "ChainError",
    "OfflineGuard",
    "ReviewReplyChain",
    "run_chain",
]
