"""Offline reply workflow: draft -> edit -> approve -> publish-intent.

This module implements the human-in-the-loop reply lifecycle required by issue
#34. It is entirely offline:

* drafts come from a pluggable generator (offline deterministic default);
* every transition is validated against :mod:`gbp.replies.states` and recorded
  in an append-only :class:`~gbp.replies.audit.AuditTrail`;
* an approval is bound to the exact revision *and* a hash of the exact text;
* the ``publish``/``send`` entry point **fails closed** while live Google access
  is disabled -- it records the publish *intent* and then raises. There is no
  code path that publishes a reply automatically, and no network call is ever
  made (an optional transport object is accepted but never used).

Canonical path
--------------
This is an **in-memory prototype** and is *not* the canonical approval path.
The canonical, DB-backed workflow used by the API, the web UI and the offline
integration seam is :class:`gbp.workflow.service.ApprovalService` (see
``docs/GREPLY-APPROVAL-WORKFLOW.md``). Nothing outside ``gbp.replies`` may
import this module; a regression test in ``tests/test_greply_approval_seam.py``
enforces that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from ..security.redact import redact_text
from .audit import AuditTrail
from .fixtures import validate_review
from .generator import DeterministicDraftGenerator, DraftGenerator
from .models import Approval, AuditEntry, Reply, text_hash
from .states import assert_transition

__all__ = [
    "ReplyWorkflow",
    "WorkflowError",
    "UnknownReply",
    "ApprovalRefused",
    "PublishDisabled",
    "DuplicatePublish",
]


class WorkflowError(Exception):
    """Base class for reply-workflow errors."""


class UnknownReply(WorkflowError):
    """No reply exists for the given id."""


class ApprovalRefused(WorkflowError):
    """An approval was requested that is not valid (empty approver / stale)."""


class PublishDisabled(WorkflowError):
    """Live publishing is disabled; the publish action must fail closed."""


class DuplicatePublish(WorkflowError):
    """The same approved revision was already marked for publish."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReplyWorkflow:
    """Drive one reply through draft/edit/approve/publish-intent offline."""

    def __init__(
        self,
        generator: Optional[DraftGenerator] = None,
        *,
        live_enabled: bool = False,
        clock: Optional[Callable[[], str]] = None,
        audit: Optional[AuditTrail] = None,
        transport: Any = None,
    ) -> None:
        self.generator = generator or DeterministicDraftGenerator()
        #: Live Google access is OFF by construction and must stay that way.
        self.live_enabled = bool(live_enabled)
        self._clock = clock or _utc_now
        self.audit = audit or AuditTrail()
        #: Accepted only so a test can prove it is never called. Never used.
        self.transport = transport
        self._replies: Dict[str, Reply] = {}

    # -- helpers ---------------------------------------------------------
    def _get(self, reply_id: str) -> Reply:
        try:
            return self._replies[reply_id]
        except KeyError:
            raise UnknownReply(f"unknown reply {reply_id!r}")

    def _audit(self, reply: Reply, from_state: str, to_state: str, actor: str) -> None:
        self.audit.record(
            AuditEntry(
                reply_id=reply.reply_id,
                review_id=reply.review_id,
                location_id=reply.location_id,
                from_state=from_state,
                to_state=to_state,
                actor=redact_text(str(actor)),
                at=self._clock(),
                revision=reply.revision,
                text_hash=text_hash(reply.text),
            )
        )

    def get(self, reply_id: str) -> Reply:
        return self._get(reply_id)

    # -- lifecycle -------------------------------------------------------
    def create_draft(self, review: dict, actor: str = "system") -> Reply:
        """Validate the review, generate a draft, and store it as ``draft``."""
        clean = validate_review(review)
        reply_id = f"reply-{clean['review_id']}"
        if reply_id in self._replies:
            raise WorkflowError(f"reply {reply_id!r} already exists")

        text = str(self.generator.draft(clean) or "").strip()
        if not text:
            raise WorkflowError("generator produced empty draft text")

        reply = Reply(
            reply_id=reply_id,
            review_id=clean["review_id"],
            location_id=clean["location_id"],
            text=text,
            state="draft",
            revision=1,
        )
        self._replies[reply_id] = reply
        self._audit(reply, from_state="", to_state="draft", actor=actor)
        return reply

    def edit(self, reply_id: str, new_text: str, actor: str = "system") -> Reply:
        """Edit the reply text. Editing an approved reply invalidates approval."""
        reply = self._get(reply_id)
        new_text = str(new_text)
        if not new_text.strip():
            raise WorkflowError("edited reply text must not be empty")

        src = reply.state
        if src == "approved":
            # Invalidation edge: an edit after approval drops back to draft and
            # the old approval no longer authorises anything.
            assert_transition("approved", "draft")
            reply.approval = None
            reply.publish_intent = False
            reply.state = "draft"
            self._audit(reply, "approved", "draft", actor)
            src = "draft"

        assert_transition(src, "edited")
        reply.state = "edited"
        reply.text = new_text
        reply.revision += 1
        self._audit(reply, src, "edited", actor)
        return reply

    def approve(
        self, reply_id: str, approver: str, revision: int, actor: Optional[str] = None
    ) -> Reply:
        """Approve the exact current revision. Requires a real approver identity."""
        reply = self._get(reply_id)
        approver = str(approver or "").strip()
        if not approver:
            raise ApprovalRefused("approver identity is required")
        if int(revision) != reply.revision:
            raise ApprovalRefused(
                f"revision {revision} is superseded (current {reply.revision})"
            )

        src = reply.state
        assert_transition(src, "approved")
        reply.state = "approved"
        reply.approval = Approval(
            revision=reply.revision,
            approver=approver,
            approved_at=self._clock(),
            text_hash=text_hash(reply.text),
        )
        self._audit(reply, src, "approved", actor or approver)
        return reply

    def publish(self, reply_id: str, actor: str = "system") -> Reply:
        """Record a publish intent, then refuse -- live publishing is disabled.

        The first call for an approved reply records ``approved ->
        publish-intent`` and raises :class:`PublishDisabled` *without* any
        network call. A second call for the same reply is refused as a
        :class:`DuplicatePublish`.
        """
        reply = self._get(reply_id)

        if reply.state == "publish-intent" or reply.publish_intent:
            raise DuplicatePublish(
                f"reply {reply_id!r} revision {reply.revision} already has a publish intent"
            )

        if reply.state != "approved" or reply.approval is None:
            raise ApprovalRefused("publish requires an approved reply")
        if not reply.approval.authorises(reply.revision, reply.text):
            # Defence in depth: approval must cover the current text.
            raise ApprovalRefused("approval does not cover the current reply text")

        assert_transition("approved", "publish-intent")
        reply.state = "publish-intent"
        reply.publish_intent = True
        self._audit(reply, "approved", "publish-intent", actor)

        # Fail closed: no live call, no automatic publish, ever.
        raise PublishDisabled(
            "live Google access is disabled; publishing a reply is not implemented"
        )

    #: Alias kept so a caller looking for "send" gets the same refusal.
    send = publish
