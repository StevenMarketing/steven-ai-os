"""Offline tests for the GReply draft/edit/approve reply workflow (issue #34).

Standard library only, synthetic fixtures only, no network at all.
Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_replies_workflow
"""

from __future__ import annotations

import json
import os
import sys
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.replies import (  # noqa: E402
    ALLOWED,
    STATES,
    ApprovalRefused,
    DeterministicDraftGenerator,
    DuplicatePublish,
    FixtureError,
    InvalidTransition,
    PublishDisabled,
    ReplyWorkflow,
    SYNTHETIC_REVIEWS,
    assert_transition,
    parse_reviews,
    validate_review,
)

FIXTURE_PATH = os.path.join(_ROOT, "tests", "fixtures", "replies_offline.json")

REVIEW = {
    "review_id": "rev-1001",
    "location_id": "accounts/1001/locations/loc-001",
    "reviewer": "Synthetic Guest A",
    "rating": 5,
    "comment": "loved my gel manicure the staff were so friendly",
}


class _GuardTransport:
    """A transport that fails loudly if it is ever called. It must never be."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("a live network call was attempted")


class _SpyGenerator:
    """An injectable, model-like generator to prove the interface is pluggable."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.seen = []

    def draft(self, review):
        self.seen.append(review)
        return self.text


class RepliesWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wf = ReplyWorkflow()

    # -- states ---------------------------------------------------------
    def test_states_module_is_the_expected_machine(self):
        self.assertEqual(STATES, ("draft", "edited", "approved", "publish-intent"))
        assert_transition("draft", "edited")
        assert_transition("approved", "publish-intent")
        with self.assertRaises(InvalidTransition):
            assert_transition("publish-intent", "approved")
        with self.assertRaises(InvalidTransition):
            assert_transition("nonsense", "draft")
        self.assertEqual(ALLOWED["publish-intent"], frozenset())

    # -- draft creation -------------------------------------------------
    def test_create_draft_from_synthetic_review(self):
        reply = self.wf.create_draft(REVIEW)
        self.assertEqual(reply.state, "draft")
        self.assertEqual(reply.revision, 1)
        self.assertTrue(reply.text.strip())
        self.assertIn("Synthetic Guest A", reply.text)

    def test_create_draft_refuses_malformed_fixture_without_partial_write(self):
        for bad in (
            {"location_id": "x", "rating": 5, "comment": "ok"},          # no id
            {"review_id": "r1", "rating": 5, "comment": "ok"},           # no location
            {"review_id": "r1", "location_id": "x", "rating": 5, "comment": "  "},  # empty text
            {"review_id": "r1", "location_id": "x", "rating": 9, "comment": "ok"},   # bad rating
            {"review_id": "r1", "location_id": "x", "rating": 5, "comment": "ok",
             "state": "teleported"},                                      # unknown state
        ):
            with self.assertRaises(FixtureError):
                self.wf.create_draft(bad)
        self.assertEqual(len(self.wf.audit), 0)
        with self.assertRaises(Exception):
            self.wf.get("reply-r1")

    # -- edit / approve -------------------------------------------------
    def test_edit_moves_draft_to_edited(self):
        self.wf.create_draft(REVIEW)
        reply = self.wf.edit("reply-rev-1001", "Thanks for the gel manicure love!")
        self.assertEqual(reply.state, "edited")
        self.assertEqual(reply.revision, 2)

    def test_approve_after_edit(self):
        self.wf.create_draft(REVIEW)
        self.wf.edit("reply-rev-1001", "Thanks for the gel manicure love!")
        reply = self.wf.approve("reply-rev-1001", "Synthetic Human", 2)
        self.assertEqual(reply.state, "approved")
        self.assertIsNotNone(reply.approval)
        self.assertTrue(reply.approval.authorises(2, reply.text))

    def test_approve_requires_nonempty_approver(self):
        self.wf.create_draft(REVIEW)
        with self.assertRaises(ApprovalRefused):
            self.wf.approve("reply-rev-1001", "   ", 1)
        self.assertEqual(self.wf.get("reply-rev-1001").state, "draft")

    def test_approve_refuses_superseded_revision(self):
        self.wf.create_draft(REVIEW)
        self.wf.edit("reply-rev-1001", "Edited text")
        with self.assertRaises(ApprovalRefused):
            self.wf.approve("reply-rev-1001", "Synthetic Human", 1)  # stale revision
        self.assertIsNone(self.wf.get("reply-rev-1001").approval)

    def test_edit_after_approval_invalidates_approval(self):
        self.wf.create_draft(REVIEW)
        self.wf.edit("reply-rev-1001", "First approved text")
        self.wf.approve("reply-rev-1001", "Synthetic Human", 2)
        reply = self.wf.edit("reply-rev-1001", "Changed after approval")
        self.assertEqual(reply.state, "edited")
        self.assertIsNone(reply.approval)
        # The workflow returns to draft then edited; a fresh approval is needed.
        with self.assertRaises(ApprovalRefused):
            self.wf.publish("reply-rev-1001")

    # -- invalid transitions -------------------------------------------
    def test_invalid_transition_refused_and_state_unchanged(self):
        self.wf.create_draft(REVIEW)
        self.wf.edit("reply-rev-1001", "Text")
        self.wf.approve("reply-rev-1001", "Synthetic Human", 2)
        # Approving an already-terminal publish-intent reply is invalid.
        with self.assertRaises(PublishDisabled):
            self.wf.publish("reply-rev-1001")
        before = self.wf.get("reply-rev-1001").state
        self.assertEqual(before, "publish-intent")
        with self.assertRaises(InvalidTransition):
            self.wf.approve("reply-rev-1001", "Synthetic Human", 2)
        self.assertEqual(self.wf.get("reply-rev-1001").state, "publish-intent")

    # -- publish fails closed ------------------------------------------
    def test_publish_refused_while_live_disabled(self):
        self.wf.create_draft(REVIEW)
        self.wf.approve("reply-rev-1001", "Synthetic Human", 1)
        with self.assertRaises(PublishDisabled):
            self.wf.publish("reply-rev-1001")
        # The intent was booked, but no publish happened.
        self.assertEqual(self.wf.get("reply-rev-1001").state, "publish-intent")
        self.assertTrue(self.wf.get("reply-rev-1001").publish_intent)

    def test_publish_requires_approval_first(self):
        self.wf.create_draft(REVIEW)
        with self.assertRaises(ApprovalRefused):
            self.wf.publish("reply-rev-1001")

    def test_duplicate_publish_refused(self):
        self.wf.create_draft(REVIEW)
        self.wf.approve("reply-rev-1001", "Synthetic Human", 1)
        with self.assertRaises(PublishDisabled):
            self.wf.publish("reply-rev-1001")
        with self.assertRaises(DuplicatePublish):
            self.wf.send("reply-rev-1001")

    # -- audit trail ----------------------------------------------------
    def test_audit_trail_one_entry_per_transition(self):
        self.wf.create_draft(REVIEW)                       # draft
        self.wf.edit("reply-rev-1001", "Edited")           # draft->edited
        self.wf.approve("reply-rev-1001", "Synthetic Human", 2)  # edited->approved
        with self.assertRaises(PublishDisabled):
            self.wf.publish("reply-rev-1001")              # approved->publish-intent
        states = [(e.from_state, e.to_state) for e in self.wf.audit.entries()]
        self.assertEqual(
            states,
            [("", "draft"), ("draft", "edited"), ("edited", "approved"),
             ("approved", "publish-intent")],
        )
        last = self.wf.audit.entries()[-1]
        self.assertEqual(last.review_id, "rev-1001")
        self.assertEqual(last.location_id, "accounts/1001/locations/loc-001")
        self.assertEqual(last.revision, 2)
        self.assertTrue(last.text_hash)

    def test_audit_trail_redacts_credentials(self):
        self.wf.create_draft(REVIEW, actor="Bearer sk-abcdef1234567890SECRET")
        for entry in self.wf.audit.entries():
            self.assertNotIn("abcdef1234567890SECRET", entry.actor)
            self.assertIn("[REDACTED]", entry.actor)

    # -- no network anywhere -------------------------------------------
    def test_no_network_call_anywhere(self):
        guard = _GuardTransport()
        wf = ReplyWorkflow(transport=guard)
        wf.create_draft(REVIEW)
        wf.edit("reply-rev-1001", "Edited")
        wf.approve("reply-rev-1001", "Synthetic Human", 2)
        with self.assertRaises(PublishDisabled):
            wf.publish("reply-rev-1001")
        self.assertEqual(guard.calls, 0)

    # -- generator interface -------------------------------------------
    def test_generator_is_injectable(self):
        spy = _SpyGenerator("Custom injected draft")
        wf = ReplyWorkflow(generator=spy)
        reply = wf.create_draft(REVIEW)
        self.assertEqual(reply.text, "Custom injected draft")
        self.assertEqual(len(spy.seen), 1)
        self.assertIsInstance(wf.generator, _SpyGenerator)
        # Default generator is the offline one.
        self.assertIsInstance(ReplyWorkflow().generator, DeterministicDraftGenerator)

    # -- fixtures -------------------------------------------------------
    def test_synthetic_fixture_matches_committed_json(self):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk, SYNTHETIC_REVIEWS)
        parsed = parse_reviews(on_disk)
        self.assertEqual(len(parsed), len(SYNTHETIC_REVIEWS))
        for item in parsed:
            self.assertEqual(validate_review(item)["review_id"], item["review_id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
