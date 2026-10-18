"""Offline end-to-end tests for the sync -> reply-workflow integration.

Standard library only, synthetic fixtures only -- nothing here touches the
network. Every test drives the fixture dataset through the whole chain and
proves the human-approval gate and the disabled publish path survive.

Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest \
        tests.test_offline_chain_integration
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.brand import BrandLoader  # noqa: E402
from gbp.integration import (  # noqa: E402
    ChainError,
    OfflineGuard,
    ReviewReplyChain,
    run_chain,
)
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ConflictError  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
DATASET_PATH = os.path.join(_ROOT, "tests", "fixtures", "reviews_offline_dataset.json")

LOC_A = "accounts/9001/locations/loc-101"
LOC_B = "accounts/9001/locations/loc-202"

#: Brand fixtures keyed by the GBP location resource name (the sync uses the
#: resource name as the location's ``location_name``). auto-publish is OFF, so
#: an approval can never promote a reply to ``ready_to_publish``.
BRAND_FIXTURE = {
    "profiles": [
        {
            "location_id": LOC_A,
            "business_name": "Demo Nails - Garden",
            "city": "Garden Grove",
            "services_json": ["Manicure", "Pedicure"],
            "language": "en",
            "tone": "warm-professional",
            "seo_keywords_json": ["Nail Salon"],
            "rules_json": {},
            "auto_publish_allowed": 0,
        },
        {
            "location_id": LOC_B,
            "business_name": "Demo Nails - Second",
            "city": "Second City",
            "services_json": ["Manicure"],
            "language": "en",
            "tone": "warm-professional",
            "seo_keywords_json": ["Nail Salon"],
            "rules_json": {},
            "auto_publish_allowed": 0,
        },
    ]
}


class ChainTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        self.tmp = tempfile.TemporaryDirectory()
        self.brand_path = os.path.join(self.tmp.name, "brand_profiles.json")
        with open(self.brand_path, "w", encoding="utf-8") as fh:
            json.dump(BRAND_FIXTURE, fh, ensure_ascii=False)
        self.loader = BrandLoader.from_file(self.brand_path)
        with open(DATASET_PATH, "r", encoding="utf-8") as fh:
            self.dataset = json.load(fh)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def chain(self, **kw):
        return ReviewReplyChain(self.repo, brand_loader=self.loader, **kw)

    def count(self, table):
        row = self.repo.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])


# ====================================================================== #
class FullChainTests(ChainTestCase):
    def test_full_chain_happy_path(self):
        guard = OfflineGuard()
        summary = run_chain(self.repo, self.dataset,
                            brand_loader=self.loader, client=guard)
        self.assertTrue(summary["ok"])
        self.assertFalse(summary["published"])
        steps = [s["step"] for s in summary["steps"]]
        self.assertEqual(
            steps, ["sync", "select_review", "create_draft", "edit", "approve", "publish"]
        )
        self.assertEqual(summary["review"]["location"], LOC_A)
        # publish is refused with a clear reason
        publish = [s for s in summary["steps"] if s["step"] == "publish"][0]["result"]
        self.assertEqual(publish["reason"], "live_access_disabled")
        self.assertTrue(publish["refused"])
        # exactly one review row per deduplicated fixture review + one draft
        self.assertEqual(self.count("reviews"), 4)
        self.assertEqual(self.count("reply_drafts"), 1)

    def test_transport_guard_never_called(self):
        guard = OfflineGuard()
        run_chain(self.repo, self.dataset, brand_loader=self.loader, client=guard)
        self.assertEqual(guard.calls, 0)


class IdempotencyTests(ChainTestCase):
    def test_rerun_is_idempotent(self):
        chain = self.chain()
        chain.run(self.dataset)
        reviews_1 = self.count("reviews")
        drafts_1 = self.count("reply_drafts")
        audit_1 = len(chain.audit_entries())

        summary2 = chain.run(self.dataset)
        self.assertEqual(self.count("reviews"), reviews_1)
        self.assertEqual(self.count("reply_drafts"), drafts_1)
        self.assertEqual(len(chain.audit_entries()), audit_1)
        self.assertEqual(summary2["sync"]["totals"]["created"], 0)
        self.assertEqual(summary2["sync"]["totals"]["updated"], 0)

    def test_no_duplicate_draft_across_resync(self):
        chain = self.chain()
        chain.run(self.dataset)
        chain.run(self.dataset)
        chain.run(self.dataset)
        self.assertEqual(self.count("reply_drafts"), 1)

    def test_changed_review_updates_in_place(self):
        chain = self.chain()
        chain.run(self.dataset)
        reviews_1 = self.count("reviews")
        drafts_1 = self.count("reply_drafts")

        changed = copy.deepcopy(self.dataset)
        # rev-3 in LOC_A: change the comment and the rating.
        for entry in changed["locations"]:
            if entry["location"] == LOC_A:
                for rev in entry["reviews"]:
                    if rev.get("reviewId") == "rev-3":
                        rev["comment"] = "Updated comment"
                        rev["rating"] = 5
        summary = chain._sync_step(changed)
        self.assertEqual(summary["totals"]["updated"], 1)
        self.assertEqual(self.count("reviews"), reviews_1)
        self.assertEqual(self.count("reply_drafts"), drafts_1)
        row = self.repo.conn.execute(
            "SELECT comment, rating FROM reviews WHERE review_name = ?",
            (f"{LOC_A}/reviews/rev-3",),
        ).fetchone()
        self.assertEqual(row["comment"], "Updated comment")
        self.assertEqual(int(row["rating"]), 5)


class IsolationTests(ChainTestCase):
    def test_per_location_isolation(self):
        chain = self.chain()
        # draft the LOC_A copy of "rev-1"
        chain.run(self.dataset, review_name=f"{LOC_A}/reviews/rev-1")
        a_review = self.repo.conn.execute(
            "SELECT id FROM reviews WHERE review_name = ?",
            (f"{LOC_A}/reviews/rev-1",),
        ).fetchone()
        b_review = self.repo.conn.execute(
            "SELECT id FROM reviews WHERE review_name = ?",
            (f"{LOC_B}/reviews/rev-1",),
        ).fetchone()
        # the LOC_B copy must have no draft
        b_drafts = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM reply_drafts WHERE review_id = ?",
            (int(b_review["id"]),),
        ).fetchone()
        self.assertEqual(int(b_drafts["n"]), 0)
        a_drafts = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM reply_drafts WHERE review_id = ?",
            (int(a_review["id"]),),
        ).fetchone()
        self.assertEqual(int(a_drafts["n"]), 1)


class ApprovalGateTests(ChainTestCase):
    def test_approval_bound_to_exact_revision(self):
        chain = self.chain()
        chain._sync_step(self.dataset)
        name = f"{LOC_A}/reviews/rev-1"
        review = chain._review_row(name)
        draft = chain._draft_step(int(review["id"]), name, "A first draft", "owner")
        draft_id = draft["draft_id"]
        # edit once so there is a superseded revision (version 1)
        chain.service.edit_draft(draft_id, "A revised draft", "owner")
        row = chain._draft_row(int(review["id"]))
        self.assertEqual(int(row["version"]), 2)
        # a stale (superseded) revision is refused
        with self.assertRaises(ConflictError):
            chain.approve_exact(draft_id, "owner", 1)
        # the current revision can still be approved
        result = chain.approve_exact(draft_id, "owner", int(row["version"]))
        self.assertIn(result["status"], ("approved", "ready_to_publish"))

    def test_edit_after_approval_invalidates(self):
        chain = self.chain()
        summary = chain.run(self.dataset)
        draft_id = [s["draft_id"] for s in summary["steps"]
                    if s["step"] == "create_draft"][0]
        result = chain.edit_after_approval(draft_id, "A brand new reply text", "owner")
        self.assertFalse(result["status"] in ("approved", "ready_to_publish"))
        # publish stays refused after the edit
        pub = chain.publish_request(draft_id)
        self.assertTrue(pub["refused"])
        self.assertFalse(pub["published"])
        # an explicit invalidation audit entry was recorded
        actions = [r["action"] for r in chain.audit_entries()]
        self.assertIn("approval_invalidated", actions)

    def test_publish_refused_end_to_end(self):
        chain = self.chain()
        summary = chain.run(self.dataset)
        draft_id = [s["draft_id"] for s in summary["steps"]
                    if s["step"] == "create_draft"][0]
        pub = chain.publish_request(draft_id)
        self.assertFalse(pub["published"])
        self.assertTrue(pub["refused"])
        self.assertEqual(pub["reason"], "live_access_disabled")


class MalformedInputTests(ChainTestCase):
    def test_malformed_fixture_refused_without_partial_records(self):
        chain = self.chain()
        with self.assertRaises(ChainError):
            chain.run({"locations": "not-a-list"})
        self.assertEqual(self.count("reviews"), 0)
        self.assertEqual(self.count("reply_drafts"), 0)
        self.assertEqual(len(chain.audit_entries()), 0)

    def test_unreadable_dataset_file_refused(self):
        chain = self.chain()
        bad = os.path.join(self.tmp.name, "broken.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(ChainError):
            chain.run(path=bad)
        self.assertEqual(self.count("reviews"), 0)


class AuditTests(ChainTestCase):
    def test_audit_covers_both_layers_and_is_redacted(self):
        chain = self.chain()
        chain.run(self.dataset)
        entries = chain.audit_entries()
        entities = {e["entity"] for e in entries}
        self.assertIn("review_sync", entities)
        self.assertIn("reply_draft", entities)
        actions = [e["action"] for e in entries]
        self.assertIn("sync_applied", actions)
        self.assertIn("draft_created", actions)
        # reply-layer transitions are recorded by the approval service too
        service_actions = [r["action"] for r in chain.service.get_audit(entity="reply_draft")]
        self.assertIn("draft_edited", service_actions)
        self.assertIn("draft_approved", service_actions)
        # redaction: no credential-shaped material anywhere in the trail
        self.assertEqual(chain.redaction_check(), [])
        blob = json.dumps(entries, ensure_ascii=False)
        self.assertNotIn("token", blob.lower())
        self.assertNotIn("secret", blob.lower())


if __name__ == "__main__":
    unittest.main()
