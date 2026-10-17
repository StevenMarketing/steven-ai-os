"""Offline tests for the owner-authorized one-time retry grant (issue #25).

No network, no token, no live worker state: every case runs against a temporary
state directory and an injected scope checker.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _path in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import retry_grant as rg  # noqa: E402

ISSUE = rg.SUPPORTED_ISSUE
OWNER = rg.OWNER
AUTH = rg.AUTHORIZATION
REASON = "scope revision approved for the blocked retry"


def ok_scope(issue):
    return True, "open with the revised offline-only scope"


def _worker(state_dir, queue):
    grant = rg.RetryGrant(state_dir, scope_checker=ok_scope)
    try:
        grant.grant(ISSUE, REASON, OWNER, AUTH)
        queue.put("granted")
    except rg.GrantError as exc:
        queue.put(f"refused:{exc}")


class RetryGrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="retry-grant-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.state = os.path.join(self.tmp, "state.json")
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump({"version": 1,
                       "processed": {str(ISSUE): {"outcome": "blocked",
                                                  "pr": None, "cost": 0.01,
                                                  "note": "old scope"}},
                       "claims": {},
                       "usage": {"2026-10-16": {"issues": 1, "cost": 0.01}}}, fh)
        self.grant = rg.RetryGrant(self.tmp, scope_checker=ok_scope)

    def _ledger(self):
        with open(self.state, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # 1 -- missing authorization
    def test_missing_authorization_is_refused(self):
        for kwargs in ({"owner": None, "authorization": None},
                       {"owner": OWNER, "authorization": "wrong-phrase"},
                       {"owner": "someoneelse", "authorization": AUTH}):
            with self.subTest(**kwargs):
                with self.assertRaises(rg.GrantError):
                    self.grant.grant(ISSUE, REASON, kwargs["owner"],
                                     kwargs["authorization"])
        self.assertIn(str(ISSUE), self._ledger()["processed"])

    # 2 -- incorrect issue number
    def test_incorrect_issue_number_is_refused(self):
        for number in (18, 20, 7):
            with self.subTest(number=number):
                with self.assertRaises(rg.GrantError):
                    self.grant.grant(number, REASON, OWNER, AUTH)
        with self.assertRaises(rg.GrantError):
            self.grant.grant(None, REASON, OWNER, AUTH)

    # 3 -- repeated invocation with the same approval
    def test_second_invocation_same_approval_is_refused(self):
        self.grant.grant(ISSUE, REASON, OWNER, AUTH)
        with self.assertRaises(rg.GrantError):
            self.grant.grant(ISSUE, REASON, OWNER, AUTH)
        self.assertEqual(
            1, len([e for e in self.grant.history() if e["event"] == "granted"]))

    # 4 -- backup verification
    def test_backup_is_written_and_verifies(self):
        result = self.grant.grant(ISSUE, REASON, OWNER, AUTH)
        self.assertTrue(os.path.isfile(result["backup"]))
        self.assertTrue(rg.RetryGrant.verify_backup(result["backup"],
                                                   result["backup_sha256"]))
        self.assertFalse(rg.RetryGrant.verify_backup(result["backup"], "0" * 64))
        with open(result["backup"], "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual("blocked", payload["prior_outcome"]["outcome"])
        self.assertEqual(0o600, os.stat(result["backup"]).st_mode & 0o777)

    # 5 -- audit history
    def test_audit_history_is_append_only_and_ordered(self):
        self.grant.grant(ISSUE, REASON, OWNER, AUTH)
        events = [e["event"] for e in self.grant.history()]
        self.assertEqual(["backup", "granted"], events)
        row = self.grant.history()[-1]
        self.assertEqual(rg.approval_id(ISSUE, OWNER, AUTH), row["approval_id"])
        self.assertEqual("blocked", row["prior_outcome"])

    # 6 -- concurrent access
    def test_concurrent_grants_allow_only_one(self):
        queue = mp.Queue()
        procs = [mp.Process(target=_worker, args=(self.tmp, queue)) for _ in range(3)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
        outcomes = [queue.get(timeout=5) for _ in procs]
        self.assertEqual(1, outcomes.count("granted"), outcomes)
        self.assertEqual(1, len([e for e in self.grant.history()
                                 if e["event"] == "granted"]))

    # 7 -- invalid scope
    def test_invalid_scope_is_refused(self):
        for detail in ("issue #19 is not open (state=closed)",
                       "issue #19 no longer carries the offline-only scope"):
            checker = rg.RetryGrant(
                self.tmp, scope_checker=lambda issue, d=detail: (False, d))
            with self.subTest(detail=detail):
                with self.assertRaises(rg.GrantError):
                    checker.grant(ISSUE, REASON, OWNER, AUTH)
        self.assertIn(str(ISSUE), self._ledger()["processed"])

    # 8 -- failure recovery (atomic apply + rollback)
    def test_failure_during_apply_rolls_back(self):
        before = self._ledger()

        class Boom(rg.RetryGrant):
            def _after_apply(self, data):
                raise RuntimeError("injected failure")

        boom = Boom(self.tmp, scope_checker=ok_scope)
        with self.assertRaises(rg.GrantError):
            boom.grant(ISSUE, REASON, OWNER, AUTH)
        self.assertEqual(before, self._ledger())
        events = [e["event"] for e in boom.history()]
        self.assertEqual(["backup", "failed"], events)
        self.assertTrue(boom.history()[-1]["rolled_back"])

    # 9 -- limits, policy and protections are preserved
    def test_limits_and_policy_are_preserved(self):
        before = self._ledger()
        self.grant.grant(ISSUE, REASON, OWNER, AUTH)
        after = self._ledger()
        self.assertEqual(before["usage"], after["usage"])
        self.assertEqual(before["claims"], after["claims"])
        self.assertEqual(before["version"], after["version"])
        self.assertNotIn(str(ISSUE), after["processed"])
        self.assertEqual("available", after["retry_grants"][str(ISSUE)]["status"])

    # 10 -- dry run writes nothing
    def test_preview_writes_nothing(self):
        plan = self.grant.plan(ISSUE, REASON, OWNER, AUTH)
        self.assertTrue(plan["dry_run"])
        self.assertTrue(plan["scope_ok"])
        self.assertFalse(os.path.exists(self.grant.audit_path))
        self.assertIn(str(ISSUE), self._ledger()["processed"])
        with self.assertRaises(rg.GrantError):
            self.grant.plan(18, REASON, OWNER, AUTH)


if __name__ == "__main__":
    unittest.main()
