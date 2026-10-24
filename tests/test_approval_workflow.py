"""Offline tests for the approval workflow service + JSON API (Group 4B).

Standard library only, MOCK data only -- nothing here touches the network.
Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import create_server  # noqa: E402
from gbp.brand import BrandLoader  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import (  # noqa: E402
    ApprovalService,
    ConflictError,
    NotFoundError,
    PublishBlocked,
)
from gbp.workflow.states import (  # noqa: E402
    ALLOWED,
    STATES,
    InvalidTransition,
    assert_transition,
    may_publish,
)

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")

#: Brand fixtures keyed by the GBP location resource name (as the engine uses).
BRAND_FIXTURE = {
    "profiles": [
        {
            "location_id": "accounts/1001/locations/loc-001",
            "business_name": "Demo Nails — Garden Grove",
            "city": "Garden Grove",
            "services_json": ["Manicure", "Pedicure", "Gel Nails"],
            "language": "en",
            "tone": "warm-professional",
            "seo_keywords_json": ["Nail Salon"],
            "rules_json": {"do_not_mention": ["Kim"]},
            "auto_publish_allowed": 0,
        },
        {
            "location_id": "accounts/1001/locations/loc-005",
            "business_name": "Demo Nails — Westminster",
            "city": "Westminster",
            "services_json": ["Manicure", "Pedicure", "Dip Powder"],
            "language": "vi",
            "tone": "friendly",
            "seo_keywords_json": ["Nail Salon", "Pedicure"],
            "rules_json": {"do_not_mention": ["Anna"]},
            "auto_publish_allowed": 1,
        },
    ]
}

SENSITIVE_COMMENT = "I got a serious infection after my pedicure and my foot was bleeding."
CLEAN_COMMENT = "Loved my gel manicure, thank you so much!"


class WorkflowTestCase(unittest.TestCase):
    """In-memory repo + four locations + a temporary brand profile file."""

    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc1 = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-001",
            title="Demo Nails — Garden Grove", city="Garden Grove",
            state="CA", language="en",
        )
        self.loc2 = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-002",
            title="Demo Nails — San Jose", city="San Jose", state="CA", language="en",
        )
        self.loc3 = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-003",
            title="Demo Nails — Houston", city="Houston", state="TX", language="vi",
        )
        self.loc5 = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-005",
            title="Demo Nails — Westminster", city="Westminster",
            state="CA", language="vi",
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.brand_path = os.path.join(self.tmp.name, "brand_profiles.json")
        with open(self.brand_path, "w", encoding="utf-8") as fh:
            json.dump(BRAND_FIXTURE, fh, ensure_ascii=False)
        self.loader = BrandLoader.from_file(self.brand_path)
        self.service = ApprovalService(self.repo, brand_loader=self.loader)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ #
    def add_review(self, location_id, name, stars, comment=None, reply_text=None):
        review_id = self.repo.upsert_review(
            location_id=location_id,
            review_name=name,
            rating=stars,
            comment=comment,
            reply_text=reply_text,
            reply_update_time="2026-10-01T10:00:00Z" if reply_text else None,
        )
        return review_id

    def make_draft(self, review_id, text="Thanks for the feedback!", status="needs_review"):
        return self.repo.insert_reply_draft(review_id, draft_text=text, status=status)

    def get_draft(self, draft_id):
        return self.repo.conn.execute(
            "SELECT * FROM reply_drafts WHERE id = ?", (draft_id,)
        ).fetchone()

    def count(self, table, where="", params=()):
        row = self.repo.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} {where}", params
        ).fetchone()
        return int(row["n"])


# ====================================================================== #
class StateMachineTests(unittest.TestCase):
    def test_states_tuple(self):
        # Issue #126 adds ``publish-intent`` (a human approved AND requested
        # publish) and the terminal ``sent`` (reachable only from it).
        self.assertEqual(
            set(STATES),
            {"draft", "needs_review", "approved", "rejected",
             "ready_to_publish", "publish-intent", "published", "failed",
             "sent"},
        )

    def test_sent_is_reachable_only_from_publish_intent(self):
        assert_transition("approved", "publish-intent")
        assert_transition("ready_to_publish", "publish-intent")
        assert_transition("publish-intent", "sent")
        for src in ("draft", "needs_review", "rejected", "approved",
                    "ready_to_publish", "published", "failed"):
            with self.assertRaises(InvalidTransition):
                assert_transition(src, "sent")
        with self.assertRaises(InvalidTransition):
            assert_transition("sent", "publish-intent")

    def test_may_publish_only_for_approved_states(self):
        self.assertTrue(may_publish("approved"))
        self.assertTrue(may_publish("ready_to_publish"))
        for state in ("draft", "needs_review", "rejected", "published", "failed"):
            self.assertFalse(may_publish(state))

    def test_assert_transition_allows_and_blocks(self):
        assert_transition("needs_review", "approved")  # no raise
        assert_transition("rejected", "needs_review")
        with self.assertRaises(InvalidTransition):
            assert_transition("published", "draft")
        with self.assertRaises(InvalidTransition):
            assert_transition("draft", "published")

    def test_published_is_terminal(self):
        self.assertEqual(set(ALLOWED["published"]), set())


# ====================================================================== #
class QueueAndCountsTests(WorkflowTestCase):
    def test_create_draft_and_list_queue(self):
        rid = self.add_review(self.loc1, "reviews/q1", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        queue = self.service.list_queue()
        self.assertEqual(len(queue), 1)
        item = queue[0]
        self.assertEqual(item["draft_id"], did)
        self.assertEqual(item["status"], "needs_review")
        self.assertEqual(item["location_id"], self.loc1)
        self.assertEqual(item["rating"], 5)
        self.assertEqual(item["version"], 1)
        self.assertFalse(item["has_reply"])
        self.assertFalse(item["is_sensitive"])

    def test_dashboard_counts(self):
        rid1 = self.add_review(self.loc1, "reviews/c1", 5, CLEAN_COMMENT)
        rid2 = self.add_review(self.loc1, "reviews/c2", 4, "Nice")
        self.make_draft(rid1, status="needs_review")
        self.make_draft(rid2, status="draft")
        counts = self.service.dashboard_counts()
        self.assertEqual(counts["needs_review"], 1)
        self.assertEqual(counts["draft"], 1)
        self.assertEqual(counts["approved"], 0)
        self.assertEqual(counts["ready_to_publish"], 0)
        self.assertEqual(counts["total"], 2)

    def test_queue_filters(self):
        rid1 = self.add_review(self.loc1, "reviews/f1", 5, CLEAN_COMMENT)
        rid2 = self.add_review(self.loc2, "reviews/f2", 3, "It was ok")
        self.make_draft(rid1, status="needs_review")
        self.make_draft(rid2, status="draft")
        self.assertEqual(len(self.service.list_queue(location_id=self.loc1)), 1)
        self.assertEqual(len(self.service.list_queue(location_id=self.loc2)), 1)
        self.assertEqual(len(self.service.list_queue(stars=5)), 1)
        self.assertEqual(len(self.service.list_queue(status="draft")), 1)
        self.assertEqual(len(self.service.list_queue(language="en")), 2)

    def test_list_locations_brand_flags(self):
        locations = {row["location_name"]: row for row in self.service.list_locations()}
        self.assertTrue(locations["accounts/1001/locations/loc-001"]["brand_complete"])
        self.assertFalse(
            locations["accounts/1001/locations/loc-001"]["auto_publish_allowed"]
        )
        self.assertTrue(
            locations["accounts/1001/locations/loc-005"]["auto_publish_allowed"]
        )
        self.assertFalse(locations["accounts/1001/locations/loc-003"]["brand_complete"])


# ====================================================================== #
class EditDraftTests(WorkflowTestCase):
    def test_edit_draft_updates_text_and_audit(self):
        rid = self.add_review(self.loc1, "reviews/e1", 5, CLEAN_COMMENT)
        did = self.make_draft(rid, text="old text")
        result = self.service.edit_draft(did, "new text", "owner@x")
        self.assertEqual(result["draft_text"], "new text")
        self.assertEqual(result["version"], 2)
        self.assertEqual(self.get_draft(did)["draft_text"], "new text")
        self.assertEqual(self.get_draft(did)["status"], "needs_review")

        self.assertEqual(
            self.count("approval_records", "WHERE draft_id = ? AND action = 'edit'", (did,)), 1
        )
        audits = self.service.get_audit(entity="reply_draft", entity_id=str(did))
        self.assertEqual([a["action"] for a in audits], ["draft_edited"])
        self.assertEqual(audits[0]["actor"], "owner@x")

    def test_edit_rejected_draft_moves_to_needs_review(self):
        rid = self.add_review(self.loc1, "reviews/e2", 5, CLEAN_COMMENT)
        did = self.make_draft(rid, status="rejected")
        result = self.service.edit_draft(did, "rewritten", "owner@x")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(self.get_draft(did)["status"], "needs_review")

    def test_edit_missing_draft_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.edit_draft(9999, "x", "owner@x")


# ====================================================================== #
class ApproveRejectTests(WorkflowTestCase):
    def test_approve_sets_approved_when_brand_disallows(self):
        rid = self.add_review(self.loc1, "reviews/a1", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        result = self.service.approve(did, "owner@x")
        self.assertEqual(result["status"], "approved")
        self.assertFalse(result["auto_publish_eligible"])
        self.assertEqual(self.get_draft(did)["status"], "approved")
        self.assertEqual(
            self.count("approval_records", "WHERE draft_id = ? AND action = 'approve'", (did,)), 1
        )
        audits = self.service.get_audit(entity="reply_draft", entity_id=str(did))
        self.assertEqual([a["action"] for a in audits], ["draft_approved"])

    def test_approve_ready_to_publish_when_brand_allows(self):
        rid = self.add_review(self.loc5, "reviews/a2", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        result = self.service.approve(did, "owner@x")
        self.assertEqual(result["status"], "ready_to_publish")
        self.assertTrue(result["auto_publish_eligible"])
        self.assertEqual(self.get_draft(did)["status"], "ready_to_publish")

    def test_reject_sets_rejected_with_note(self):
        rid = self.add_review(self.loc1, "reviews/r1", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        result = self.service.reject(did, "owner@x", note="tone too casual")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.get_draft(did)["status"], "rejected")
        row = self.repo.conn.execute(
            "SELECT * FROM approval_records WHERE draft_id = ?", (did,)
        ).fetchone()
        self.assertEqual(row["action"], "reject")
        self.assertEqual(row["note"], "tone too casual")
        audits = self.service.get_audit(entity="reply_draft", entity_id=str(did))
        self.assertEqual([a["action"] for a in audits], ["draft_rejected"])

    def test_approve_missing_review_raises_not_found(self):
        # draft referencing a non-existent review id cannot be created (FK), so
        # a bogus draft id is the reachable 404 path.
        with self.assertRaises(NotFoundError):
            self.service.approve(4242, "owner@x")


# ====================================================================== #
class BulkApproveTests(WorkflowTestCase):
    def test_bulk_approve_valid(self):
        rid = self.add_review(self.loc5, "reviews/b1", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        out = self.service.bulk_approve([did], "owner@x")
        self.assertEqual(len(out["approved"]), 1)
        self.assertEqual(out["approved"][0]["status"], "ready_to_publish")
        self.assertEqual(out["skipped"], [])

    def test_bulk_approve_skips_low_stars(self):
        rid = self.add_review(self.loc5, "reviews/b2", 2, "Slow and rude.")
        did = self.make_draft(rid)
        out = self.service.bulk_approve([did], "owner@x")
        self.assertEqual(out["approved"], [])
        self.assertEqual(len(out["skipped"]), 1)
        self.assertIn("low_stars", out["skipped"][0]["reason"])
        self.assertEqual(self.get_draft(did)["status"], "needs_review")

    def test_bulk_approve_skips_sensitive(self):
        rid = self.add_review(self.loc5, "reviews/b3", 5, SENSITIVE_COMMENT)
        did = self.make_draft(rid)
        out = self.service.bulk_approve([did], "owner@x")
        self.assertEqual(out["approved"], [])
        self.assertIn("sensitive", out["skipped"][0]["reason"])

    def test_bulk_approve_skips_missing_brand(self):
        rid = self.add_review(self.loc3, "reviews/b4", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        out = self.service.bulk_approve([did], "owner@x")
        self.assertEqual(out["approved"], [])
        self.assertEqual(out["skipped"][0]["reason"], "missing_brand_profile")

    def test_bulk_approve_skips_brand_disabled(self):
        rid = self.add_review(self.loc1, "reviews/b5", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        out = self.service.bulk_approve([did], "owner@x")
        self.assertEqual(out["approved"], [])
        self.assertEqual(out["skipped"][0]["reason"], "no_brand_auto_publish_eligibility")

    def test_bulk_approve_skips_unknown_id(self):
        out = self.service.bulk_approve([7777], "owner@x")
        self.assertEqual(out["approved"], [])
        self.assertEqual(out["skipped"][0]["reason"], "draft_not_found")


# ====================================================================== #
class PublishGuardTests(WorkflowTestCase):
    def test_publish_guard_blocks_when_not_approved(self):
        rid = self.add_review(self.loc5, "reviews/g1", 5, CLEAN_COMMENT)
        did = self.make_draft(rid, status="needs_review")
        with self.assertRaises(PublishBlocked) as ctx:
            self.service.publish_guard(did)
        self.assertIn("state_not_publishable", ctx.exception.reason)

    def test_publish_guard_blocks_when_review_already_has_reply(self):
        rid = self.add_review(
            self.loc5, "reviews/g2", 5, CLEAN_COMMENT, reply_text="already replied"
        )
        did = self.make_draft(rid, status="approved")
        with self.assertRaises(PublishBlocked) as ctx:
            self.service.publish_guard(did)
        self.assertEqual(ctx.exception.reason, "review_already_has_reply")

    def test_publish_guard_blocks_when_brand_disabled(self):
        rid = self.add_review(self.loc1, "reviews/g3", 5, CLEAN_COMMENT)
        did = self.make_draft(rid, status="approved")
        with self.assertRaises(PublishBlocked) as ctx:
            self.service.publish_guard(did)
        self.assertEqual(ctx.exception.reason, "brand_auto_publish_disabled")

    def test_publish_guard_blocks_low_stars_and_sensitive(self):
        rid = self.add_review(self.loc5, "reviews/g4", 1, "Terrible.")
        did = self.make_draft(rid, status="approved")
        with self.assertRaises(PublishBlocked) as ctx:
            self.service.publish_guard(did)
        self.assertEqual(ctx.exception.reason, "low_stars_1_2")

        rid2 = self.add_review(self.loc5, "reviews/g5", 5, SENSITIVE_COMMENT)
        did2 = self.make_draft(rid2, status="approved")
        with self.assertRaises(PublishBlocked) as ctx2:
            self.service.publish_guard(did2)
        self.assertEqual(ctx2.exception.reason, "sensitive_review")

    def test_publish_guard_ok(self):
        rid = self.add_review(self.loc5, "reviews/g6", 5, CLEAN_COMMENT)
        did = self.make_draft(rid, status="approved")
        out = self.service.publish_guard(did)
        self.assertTrue(out["publishable"])
        # No side effects: no new approval/audit rows from a guard call.
        self.assertEqual(self.count("approval_records"), 0)
        self.assertEqual(self.count("audit_logs"), 0)


# ====================================================================== #
class IsolationAndBrandTests(WorkflowTestCase):
    def test_two_locations_stay_isolated(self):
        rid1 = self.add_review(self.loc1, "reviews/i1", 5, CLEAN_COMMENT)
        rid2 = self.add_review(self.loc2, "reviews/i2", 5, CLEAN_COMMENT)
        did1 = self.make_draft(rid1)
        did2 = self.make_draft(rid2)

        queue1 = self.service.list_queue(location_id="accounts/1001/locations/loc-001")
        self.assertEqual([row["draft_id"] for row in queue1], [did1])
        self.assertNotIn(did2, [row["draft_id"] for row in queue1])

        queue1_by_id = self.service.list_queue(location_id=self.loc1)
        self.assertEqual([row["draft_id"] for row in queue1_by_id], [did1])

        # Approving through loc-001's queue never touches loc-002's draft.
        self.service.approve(did1, "owner@x")
        self.assertEqual(self.get_draft(did2)["status"], "needs_review")

    def test_missing_brand_profile_approves_but_never_ready_to_publish(self):
        rid = self.add_review(self.loc3, "reviews/m1", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        result = self.service.approve(did, "owner@x")
        self.assertEqual(result["status"], "approved")
        self.assertFalse(result["auto_publish_eligible"])
        # publish_guard still refuses: no brand -> auto_publish not allowed.
        with self.assertRaises(PublishBlocked) as ctx:
            self.service.publish_guard(did)
        self.assertEqual(ctx.exception.reason, "brand_auto_publish_disabled")


# ====================================================================== #
class IdempotencyAndConflictTests(WorkflowTestCase):
    def test_idempotency_key_performs_no_second_write(self):
        rid = self.add_review(self.loc5, "reviews/k1", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        first = self.service.approve(did, "owner@x", idempotency_key="key-1")
        second = self.service.approve(did, "owner@x", idempotency_key="key-1")
        self.assertEqual(first, second)
        self.assertEqual(
            self.count("approval_records", "WHERE draft_id = ?", (did,)), 1
        )
        self.assertEqual(
            self.count("audit_logs", "WHERE entity_id = ?", (str(did),)), 1
        )
        self.assertEqual(self.count("idempotency_keys"), 1)

    def test_idempotency_on_edit_and_reject(self):
        rid = self.add_review(self.loc1, "reviews/k2", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        self.service.edit_draft(did, "one", "a", idempotency_key="e-1")
        self.service.edit_draft(did, "one", "a", idempotency_key="e-1")
        self.assertEqual(
            self.count("approval_records", "WHERE draft_id = ? AND action = 'edit'", (did,)), 1
        )
        self.service.reject(did, "a", note="no", idempotency_key="r-1")
        self.service.reject(did, "a", note="no", idempotency_key="r-1")
        self.assertEqual(
            self.count("approval_records", "WHERE draft_id = ? AND action = 'reject'", (did,)), 1
        )

    def test_concurrent_approve_conflict_stale_version(self):
        rid = self.add_review(self.loc1, "reviews/k3", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        winner = self.service.approve(did, "approver-1", expected_version=1)
        self.assertEqual(winner["version"], 2)
        with self.assertRaises(ConflictError):
            self.service.approve(did, "approver-2", expected_version=1)
        # exactly one approval record + one audit row survived
        self.assertEqual(
            self.count("approval_records", "WHERE draft_id = ? AND action = 'approve'", (did,)), 1
        )
        self.assertEqual(
            self.count("audit_logs", "WHERE entity_id = ? AND action = 'draft_approved'", (str(did),)), 1
        )

    def test_edit_conflict_stale_version(self):
        rid = self.add_review(self.loc1, "reviews/k4", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        self.service.edit_draft(did, "v2", "a", expected_version=1)
        with self.assertRaises(ConflictError):
            self.service.edit_draft(did, "stale", "b", expected_version=1)


# ====================================================================== #
class AuditAndHistoryTests(WorkflowTestCase):
    def test_audit_log_and_edit_history(self):
        rid = self.add_review(self.loc1, "reviews/h1", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        self.service.edit_draft(did, "first revision", "owner@x")
        self.service.edit_draft(did, "second revision", "owner@x")

        history = self.service.get_approval_history(did)
        self.assertEqual([h["action"] for h in history], ["edit", "edit"])
        self.assertEqual(history[-1]["actor"], "owner@x")

        audits = self.service.get_audit(entity="reply_draft", entity_id=str(did))
        self.assertEqual([a["action"] for a in audits], ["draft_edited", "draft_edited"])
        self.assertEqual(self.get_draft(did)["version"], 3)

    def test_audit_filter_by_actor(self):
        rid = self.add_review(self.loc1, "reviews/h2", 5, CLEAN_COMMENT)
        did = self.make_draft(rid)
        self.service.edit_draft(did, "x", "alice")
        self.service.reject(did, "bob", note="no")
        self.assertEqual(len(self.service.get_audit(actor="alice")), 1)
        self.assertEqual(len(self.service.get_audit(actor="bob")), 1)
        self.assertEqual(len(self.service.get_audit(actor="carol")), 0)


# ====================================================================== #
class ApiSmokeTests(WorkflowTestCase):
    """In-process server on port 0 -- loopback only, no external network."""

    def setUp(self):
        super().setUp()
        rid = self.add_review(self.loc5, "reviews/api1", 5, CLEAN_COMMENT)
        self.draft_id = self.make_draft(rid)
        self.server = create_server(self.service, port=0)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _request(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_server_binds_loopback_only(self):
        self.assertEqual(self.host, "127.0.0.1")
        self.assertNotEqual(self.port, 0)

    def test_get_dashboard_and_queue(self):
        code, body = self._request("GET", "/api/dashboard")
        self.assertEqual(code, 200)
        self.assertEqual(body["needs_review"], 1)

        code, queue = self._request("GET", "/api/queue")
        self.assertEqual(code, 200)
        self.assertIsInstance(queue, dict)
        self.assertEqual(queue["items"][0]["draft_id"], self.draft_id)

    def test_get_locations_and_audit(self):
        code, body = self._request("GET", "/api/locations")
        self.assertEqual(code, 200)
        self.assertTrue(any(loc["brand_complete"] for loc in body))

        code, body = self._request("GET", "/api/audit")
        self.assertEqual(code, 200)
        self.assertIsInstance(body, list)

    def test_post_approve_and_publish_guard(self):
        code, body = self._request(
            "POST", f"/api/drafts/{self.draft_id}/approve", {"actor": "api-user"}
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ready_to_publish")

        code, body = self._request("POST", f"/api/drafts/{self.draft_id}/publish-guard")
        self.assertEqual(code, 200)
        self.assertTrue(body["publishable"])

    def test_post_edit_and_reject(self):
        code, body = self._request(
            "POST", f"/api/drafts/{self.draft_id}/edit",
            {"text": "edited via api", "actor": "api-user"},
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["draft_text"], "edited via api")

        code, body = self._request(
            "POST", f"/api/drafts/{self.draft_id}/reject",
            {"note": "no good", "actor": "api-user"},
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "rejected")

    def test_bulk_approve_route(self):
        code, body = self._request(
            "POST", "/api/drafts/bulk-approve",
            {"ids": [self.draft_id], "actor": "api-user"},
        )
        self.assertEqual(code, 200)
        self.assertEqual(len(body["approved"]), 1)
        self.assertEqual(body["skipped"], [])

    def test_error_codes(self):
        # 404 unknown draft
        code, body = self._request("POST", "/api/drafts/99999/approve", {"actor": "x"})
        self.assertEqual(code, 404)
        self.assertEqual(body["error"], "not_found")

        # 404 unknown route
        code, _ = self._request("GET", "/api/nope")
        self.assertEqual(code, 404)

        # 409 conflict via stale expected_version
        code, _ = self._request(
            "POST", f"/api/drafts/{self.draft_id}/approve",
            {"actor": "x", "expected_version": 99},
        )
        self.assertEqual(code, 409)

        # 423 publish blocked (still needs_review)
        code, body = self._request(
            "POST", f"/api/drafts/{self.draft_id}/publish-guard"
        )
        self.assertEqual(code, 423)
        self.assertEqual(body["error"], "publish_blocked")

        # 400 invalid body
        code, _ = self._request(
            "POST", f"/api/drafts/{self.draft_id}/edit", {"actor": "x"}
        )
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
