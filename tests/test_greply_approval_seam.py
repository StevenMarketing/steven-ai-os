"""Regression tests for the GReply approval seam (issue #38).

After PR #35 and #37 there are two approval implementations in the tree:

* ``gbp.workflow.service.ApprovalService`` -- the DB-backed, canonical
  human-in-the-loop machine over ``reply_drafts`` rows. The JSON API
  (``gbp.api.server``), the web UI and the offline integration chain
  (``gbp.integration.chain.ReviewReplyChain``) all use it.
* ``gbp.replies.workflow.ReplyWorkflow`` -- an in-memory, network-free
  prototype with its own state machine. It must not be wired into any
  production path.

These tests lock the seam down so the two cannot silently diverge again:

* the canonical path is the one the chain and the API actually call;
* ``ReplyWorkflow`` is referenced nowhere outside its own package;
* nothing publishes or sends automatically, on either path, and no network
  call is ever made.

Standard library only, synthetic fixtures only, in-memory SQLite -- offline.

Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest \\
        tests.test_greply_approval_seam
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.integration import OfflineGuard, ReviewReplyChain, run_chain  # noqa: E402
from gbp.replies.workflow import PublishDisabled, ReplyWorkflow  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService, PublishBlocked  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
DATASET_PATH = os.path.join(_ROOT, "tests", "fixtures", "reviews_offline_dataset.json")
SRC_ROOT = os.path.join(_ROOT, "src", "gbp")

#: Brand profiles with auto-publish OFF, so an approval can only ever reach the
#: plain ``approved`` state -- never an auto-publishable one.
BRAND_FIXTURE = {
    "profiles": [
        {
            "location_id": "accounts/9001/locations/loc-101",
            "business_name": "Demo Nails - Garden",
            "city": "Garden Grove",
            "services_json": ["Manicure", "Pedicure"],
            "language": "en",
            "tone": "warm-professional",
            "seo_keywords_json": ["Nail Salon"],
            "rules_json": {},
            "auto_publish_allowed": 0,
        }
    ]
}

REVIEW = {
    "review_id": "rev-seam-1",
    "location_id": "accounts/9001/locations/loc-101",
    "reviewer": "Synthetic Guest",
    "rating": 5,
    "comment": "loved my gel manicure, the staff were so friendly",
}

#: A transport that fails loudly if it is ever called -- it must never be.
class _GuardTransport:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("a live network call was attempted")


def _iter_source_modules(root: str):
    """Every ``.py`` file under ``root`` (skips ``__pycache__``)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


# ====================================================================== #
class CanonicalAuthorityTests(unittest.TestCase):
    """Which approval implementation the real callers actually use."""

    def test_chain_service_is_the_canonical_approval_service(self):
        repo = Repository(":memory:")
        try:
            repo.init_db(SCHEMA_PATH)
            chain = ReviewReplyChain(repo, client=OfflineGuard())
            self.assertIsInstance(chain.service, ApprovalService)
        finally:
            repo.close()

    def test_api_server_uses_the_canonical_approval_service(self):
        path = os.path.join(SRC_ROOT, "api", "server.py")
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("ApprovalService", source)
        self.assertNotIn("ReplyWorkflow", source)

    def test_reply_workflow_is_not_wired_outside_its_package(self):
        """The in-memory prototype must not be referenced by any other module."""
        replies_pkg = os.path.join(SRC_ROOT, "replies")
        offenders = []
        for path in _iter_source_modules(SRC_ROOT):
            if os.path.commonpath([os.path.abspath(path), replies_pkg]) == replies_pkg:
                continue
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            text = unicodedata.normalize("NFKC", text)
            if re.search(r"\bReplyWorkflow\b", text):
                offenders.append(os.path.relpath(path, _ROOT))
        self.assertEqual(
            offenders, [],
            f"ReplyWorkflow referenced outside src/gbp/replies: {offenders}",
        )


# ====================================================================== #
class SeamNoAutoPublishTests(unittest.TestCase):
    """End-to-end: the seam drafts, edits and approves, but never publishes."""

    def setUp(self) -> None:
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        import tempfile

        from gbp.brand import BrandLoader

        self.tmp = tempfile.TemporaryDirectory()
        self.brand_path = os.path.join(self.tmp.name, "brand_profiles.json")
        with open(self.brand_path, "w", encoding="utf-8") as fh:
            json.dump(BRAND_FIXTURE, fh, ensure_ascii=False)
        self.loader = BrandLoader.from_file(self.brand_path)
        with open(DATASET_PATH, "r", encoding="utf-8") as fh:
            self.dataset = json.load(fh)

    def tearDown(self) -> None:
        self.repo.close()
        self.tmp.cleanup()

    def test_full_chain_never_publishes_and_makes_no_network_call(self):
        guard = OfflineGuard()
        summary = run_chain(
            self.repo, self.dataset, brand_loader=self.loader, client=guard
        )
        self.assertFalse(summary["published"])
        self.assertTrue(summary["ok"])
        publish = [s for s in summary["steps"] if s["step"] == "publish"][0]["result"]
        self.assertTrue(publish["refused"])
        self.assertEqual(publish["reason"], "live_access_disabled")
        # No code path reached the network, even with a live client injected.
        self.assertEqual(guard.calls, 0)
        # The approval gate ran: a real approval row exists for the draft.
        rows = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM approval_records"
        ).fetchone()
        self.assertGreaterEqual(int(rows["n"]), 1)

    def test_publish_guard_is_a_refusal_not_a_publish(self):
        chain = ReviewReplyChain(self.repo, brand_loader=self.loader)
        chain.run(self.dataset)
        draft_id = int(
            self.repo.conn.execute("SELECT id FROM reply_drafts LIMIT 1").fetchone()["id"]
        )
        # auto-publish is OFF, so the approved draft is not publishable.
        with self.assertRaises(PublishBlocked):
            chain.service.publish_guard(draft_id)

    def test_reply_workflow_publish_fails_closed_without_network(self):
        guard = _GuardTransport()
        wf = ReplyWorkflow(transport=guard)
        wf.create_draft(REVIEW)
        wf.approve("reply-rev-seam-1", "Synthetic Human", 1)
        with self.assertRaises(PublishDisabled):
            wf.publish("reply-rev-seam-1")
        self.assertEqual(guard.calls, 0)

    def test_neither_implementation_needs_a_live_client_to_run(self):
        """Both machines run fully offline; the guard transport is never used."""
        guard = OfflineGuard()
        # in-memory prototype
        wf = ReplyWorkflow()
        wf.create_draft(REVIEW)
        # canonical seam
        run_chain(self.repo, self.dataset, brand_loader=self.loader, client=guard)
        self.assertEqual(guard.calls, 0)


if __name__ == "__main__":
    unittest.main()
