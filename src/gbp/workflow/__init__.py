"""Draft approval workflow (Group 4B).

Modules:
    gbp.workflow.states   -- the draft state machine + transition guard
    gbp.workflow.service  -- ApprovalService: queue, edit, approve, reject,
                             bulk-approve, publish guard, idempotency, conflicts

The service is stdlib-only, offline, and NEVER publishes anything: publishing is
a later phase. The only side effects are rows in ``reply_drafts``,
``approval_records``, ``audit_logs`` and ``idempotency_keys``.
"""

from __future__ import annotations

from .service import (  # noqa: F401
    ApprovalService,
    ConflictError,
    NotFoundError,
    PublishBlocked,
    WorkflowError,
)
from .states import (  # noqa: F401
    ALLOWED,
    STATES,
    InvalidTransition,
    assert_transition,
    may_publish,
)

__all__ = [
    "ApprovalService",
    "ConflictError",
    "NotFoundError",
    "PublishBlocked",
    "WorkflowError",
    "ALLOWED",
    "STATES",
    "InvalidTransition",
    "assert_transition",
    "may_publish",
]
