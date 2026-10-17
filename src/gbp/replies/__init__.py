"""Offline review-reply workflow: draft -> edit -> approve -> publish-intent.

A self-contained, network-free increment (issue #34). The publish action stays
disabled: :meth:`gbp.replies.workflow.ReplyWorkflow.publish` fails closed and no
code path publishes automatically.

Modules:
    gbp.replies.states    -- the reply lifecycle state machine
    gbp.replies.models    -- Reply / Approval / AuditEntry value objects
    gbp.replies.generator -- pluggable draft generation (offline default)
    gbp.replies.audit     -- append-only transition audit trail
    gbp.replies.fixtures  -- synthetic review fixtures + validation
    gbp.replies.workflow  -- the orchestrating workflow
"""

from __future__ import annotations

from .audit import AuditTrail  # noqa: F401
from .fixtures import (  # noqa: F401
    SYNTHETIC_REVIEWS,
    FixtureError,
    parse_reviews,
    validate_review,
)
from .generator import DeterministicDraftGenerator, DraftGenerator  # noqa: F401
from .models import Approval, AuditEntry, Reply, text_hash  # noqa: F401
from .states import (  # noqa: F401
    ALLOWED,
    STATES,
    InvalidTransition,
    assert_transition,
    can_transition,
)
from .workflow import (  # noqa: F401
    ApprovalRefused,
    DuplicatePublish,
    PublishDisabled,
    ReplyWorkflow,
    UnknownReply,
    WorkflowError,
)

__all__ = [
    "ALLOWED",
    "STATES",
    "Approval",
    "ApprovalRefused",
    "AuditEntry",
    "AuditTrail",
    "DeterministicDraftGenerator",
    "DraftGenerator",
    "DuplicatePublish",
    "FixtureError",
    "InvalidTransition",
    "PublishDisabled",
    "Reply",
    "ReplyWorkflow",
    "SYNTHETIC_REVIEWS",
    "UnknownReply",
    "WorkflowError",
    "assert_transition",
    "can_transition",
    "parse_reviews",
    "text_hash",
    "validate_review",
]
