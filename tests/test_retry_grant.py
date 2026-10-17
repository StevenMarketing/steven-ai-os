"""Offline tests for the owner-authorized one-time retry grant (issue #25).

No network, no token, no live worker state: every case runs against a temporary
state directory and an injected scope checker.  Covers the review asks on PR
#26: a real approval *artifact* (not a shared phrase), an operator allow-list,
the worker-agent ledger guard, fail-closed reads, a required prior ``blocked``
record, exact-scope / approved-revision validation, and one-time-audit
reliability (rollback + pending marker) when the audit cannot be written.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _path in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import retry_grant as rg  # noqa: E402

ISSUE = rg.SUPPORTED_ISSUE
OWNER = rg.AUTHORIZED_OWNER
OPERATOR = "vps-operator"
NONCE = "0123456789abcdef"
REASON = "scope revision approved for the blocked retry"
#: a body that satisfies the revised scope requirements
GOOD_BODY = ("## Scope\nOnly web/, tests/, and permitted standalone scripts/. "
             "Do not modify protected server files.\n")
GOOD_SHA = rg.body_sha256(GOOD_BODY)


def ok_scope(issue, expected_sha=None):
    return True, "open at the approved revision with the revised scope"


def make_grant(state_dir, scope_checker=ok_scope, **kwargs):
    return rg.RetryGrant(state_dir, scope_checker=scope_checker,
                         allowed_operators=[OPERATOR], **kwargs)


def _worker(state_dir, queue):
    grant = make_grant(state_dir)
    approval = rg.load_approval(os.path.join(state_dir, "approval.json"))
    try:
        grant.grant(ISSUE, REASON, approval, OPERATOR)
        queue.put("granted")
    except rg.GrantError as exc:
        queue.put(f"refused:{exc}")


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="retry-grant-")
        prev = os.environ.get(rg.STATE_ENV)
        self.addCleanup(self._restore_env, prev)
        os.environ[rg.STATE_ENV] = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.state = os.path.join(self.tmp, "state.json")
        self.write_state({
            "version": 1,
            "processed": {str(ISSUE): {"outcome": "blocked", "pr": None,
                                       "cost": 0.01, "note": "old scope"}},
            "claims": {},
            "usage": {"2026-10-16": {"issues": 1, "cost": 0.01}},
        })
        self.approval_path = os.path.join(self.tmp, "approval.json")
        rg.write_approval(self.approval_path, nonce=NONCE,
                          approved_body_sha256=GOOD_SHA)
        self.grant = make_grant(self.tmp, approval_path=self.approval_path)

    def _restore_env(self, prev):
        if prev is None:
            os.environ.pop(rg.STATE_ENV, None)
        else:
            os.environ[rg.STATE_ENV] = prev

    def write_state(self, data):
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def ledger(self):
        with open(self.state, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def approval(self):
        return rg.load_approval(self.approval_path)


# --------------------------------------------------------------------------
# review ask (1): a real approval artifact, never a shared pass-phrase
# --------------------------------------------------------------------------
class ApprovalArtifactTests(BaseCase):
    def test_missing_artifact_is_refused(self):
        with self.assertRaises(rg.GrantError):
            rg.load_approval(None)
        with self.assertRaises(rg.GrantError):
            rg.load_approval(os.path.join(self.tmp, "nope.json"))

    def test_insecure_artifact_permissions_are_refused(self):
        os.chmod(self.approval_path, 0o644)
        with self.assertRaises(rg.GrantError):
            rg.load_approval(self.approval_path)

    def test_malformed_artifact_is_refused(self):
        with open(self.approval_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        os.chmod(self.approval_path, 0o600)
        with self.assertRaises(rg.GrantError):
            rg.load_approval(self.approval_path)

    def test_wrong_owner_or_issue_or_revision_is_refused(self):
        for mutate in (
            lambda d: d.update({"owner": "someone-else"}),
            lambda d: d.update({"issue": 20}),
            lambda d: d.update({"approved_body_sha256": "deadbeef"}),
            lambda d: d.update({"approval_nonce": "short"}),
        ):
            with self.subTest(mutate=mutate):
                with open(self.approval_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                mutate(data)
                with open(self.approval_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.chmod(self.approval_path, 0o600)
                with self.assertRaises(rg.GrantError):
                    rg.load_approval(self.approval_path)

    def test_artifact_is_consumed_on_success(self):
        self.grant.grant(ISSUE, REASON, self.approval(), OPERATOR)
        self.assertFalse(os.path.exists(self.approval_path))
        assert not os.path.exists(self.approval_path)
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith("approval.json.used-")]
        self.assertEqual(1, len(leftovers))

    def test_no_shared_phrase_constant_remains(self):
        self.assertFalse(hasattr(rg, "AUTHORIZATION"))


# --------------------------------------------------------------------------
# review ask (1, second half): separately verified operator permission
# --------------------------------------------------------------------------
class OperatorPermissionTests(BaseCase):
    def test_operator_must_be_named(self):
        with self.assertRaises(rg.GrantError):
            rg.validate_request(ISSUE, REASON, self.approval(), None,
                                allowed_operators=[OPERATOR])
        with self.assertRaises(rg.GrantError):
            rg.validate_request(ISSUE, REASON, self.approval(), "intruder",
                                allowed_operators=[OPERATOR])

    def test_no_authorized_operator_is_refused(self):
        grant = rg.RetryGrant(self.tmp, scope_checker=ok_scope,
                              allowed_operators=[])
        with self.assertRaises(rg.GrantError):
            grant.grant(ISSUE, REASON, self.approval(), OPERATOR)


# --------------------------------------------------------------------------
# review ask (2): exact scope / stable approved revision
# --------------------------------------------------------------------------
class ScopeTests(BaseCase):
    def test_good_body_passes_scope(self):
        ok, _ = rg.evaluate_scope(GOOD_BODY)
        self.assertTrue(ok)

    def test_scope_missing_directories_is_refused(self):
        body = "## Scope\nJust docs. Do not modify protected server files.\n"
        ok, detail = rg.evaluate_scope(body)
        self.assertFalse(ok)
        self.assertIn("directories", detail)

    def test_scope_without_protected_exclusion_is_refused(self):
        body = "## Scope\nOnly web/, tests/, scripts/.\n"
        ok, detail = rg.evaluate_scope(body)
        self.assertFalse(ok)
        self.assertIn("protected server files", detail)

    def test_scope_naming_a_protected_path_is_refused(self):
        body = ("## Scope\nOnly web/, tests/, scripts/. Do not modify protected "
                "server files.\nSee src/gbp/api/server.py\n")
        ok, detail = rg.evaluate_scope(body)
        self.assertFalse(ok)
        self.assertIn("protected server path", detail)

    def test_approved_revision_mismatch_is_refused(self):
        # the injected checker is replaced by one that enforces the revision
        def strict(issue, expected_sha=None):
            if expected_sha != GOOD_SHA:
                return False, "revision mismatch"
            return True, "ok"

        # an approval for a *different* revision must not be granted
        rg.write_approval(self.approval_path, nonce=NONCE,
                          approved_body_sha256=rg.body_sha256("different body"))
        grant = make_grant(self.tmp, scope_checker=strict,
                           approval_path=self.approval_path)
        with self.assertRaises(rg.GrantError):
            grant.grant(ISSUE, REASON, rg.load_approval(self.approval_path), OPERATOR)

    def test_scope_failure_refuses_and_changes_nothing(self):
        grant = make_grant(self.tmp,
                           scope_checker=lambda i, s=None: (False, "closed"),
                           approval_path=self.approval_path)
        with self.assertRaises(rg.GrantError):
            grant.grant(ISSUE, REASON, self.approval(), OPERATOR)
        self.assertIn(str(ISSUE), self.ledger()["processed"])


# --------------------------------------------------------------------------
# review ask (3): fail-closed reads and a required prior blocked record
# --------------------------------------------------------------------------
class FailClosedLedgerTests(BaseCase):
    def test_missing_live_ledger_is_refused(self):
        os.unlink(self.state)
        with self.assertRaises(rg.GrantError):
            self.grant.grant(ISSUE, REASON, self.approval(), OPERATOR)

    def test_malformed_live_ledger_is_refused(self):
        with open(self.state, "w", encoding="utf-8") as fh:
            fh.write("][")
        with self.assertRaises(rg.GrantError):
            self.grant.grant(ISSUE, REASON, self.approval(), OPERATOR)

    def test_grant_without_blocked_record_is_refused(self):
        for processed in ({}, {str(ISSUE): {"outcome": "done"}}):
            with self.subTest(processed=processed):
                self.write_state({"version": 1, "processed": processed,
                                  "claims": {}, "usage": {}})
                with self.assertRaises(rg.GrantError):
                    self.grant.grant(ISSUE, REASON, self.approval(), OPERATOR)


# --------------------------------------------------------------------------
# review ask (4): only the worker-agent profile ledger may be touched
# --------------------------------------------------------------------------
class WorkerProfileGuardTests(BaseCase):
    def test_non_worker_path_is_refused(self):
        os.environ.pop(rg.STATE_ENV, None)
        grant = rg.RetryGrant(self.tmp, scope_checker=ok_scope,
                              allowed_operators=[OPERATOR], approval_path=self.approval_path)
        with self.assertRaises(rg.GrantError):
            grant.grant(ISSUE, REASON, self.approval(), OPERATOR)

    def test_state_env_pin_is_accepted(self):
        marker = os.path.join(self.tmp, "profiles", "worker-agent",
                              "ops", "github-worker")
        os.makedirs(marker, exist_ok=True)
        shutil.copy(self.state, os.path.join(marker, "state.json"))
        os.environ.pop(rg.STATE_ENV, None)
        grant = rg.RetryGrant(marker, scope_checker=ok_scope,
                              allowed_operators=[OPERATOR])
        ok, detail = grant.worker_profile_reason()
        self.assertTrue(ok, detail)

    def test_preview_reports_the_exact_target(self):
        plan = self.grant.plan(ISSUE, REASON, self.approval(), OPERATOR)
        self.assertEqual(self.state, plan["state_path"])
        self.assertTrue(plan["request_ok"])
        self.assertTrue(plan["eligible_prior_blocked"])


# --------------------------------------------------------------------------
# core behaviour: grant, one-shot, backup, audit, limits preserved
# --------------------------------------------------------------------------
class GrantBehaviourTests(BaseCase):
    def test_incorrect_issue_number_is_refused(self):
        for number in (18, 20, 7, None):
            with self.subTest(number=number):
                with self.assertRaises(rg.GrantError):
                    self.grant.grant(number, REASON, self.approval(), OPERATOR)

    def test_short_reason_is_refused(self):
        with self.assertRaises(rg.GrantError):
            self.grant.grant(ISSUE, "no", self.approval(), OPERATOR)

    def test_second_invocation_same_approval_is_refused(self):
        approval = self.approval()
        self.grant.grant(ISSUE, REASON, approval, OPERATOR)
        # the artifact is gone; and even re-supplying the dict is refused
        with self.assertRaises(rg.GrantError):
            self.grant.grant(ISSUE, REASON, approval, OPERATOR)
        self.assertEqual(1, len([e for e in self.grant.history()
                                 if e["event"] == "granted"]))

    def test_backup_is_written_and_verifies(self):
        result = self.grant.grant(ISSUE, REASON, self.approval(), OPERATOR)
        self.assertTrue(os.path.isfile(result["backup"]))
        self.assertTrue(rg.RetryGrant.verify_backup(result["backup"],
                                                   result["backup_sha256"]))
        self.assertFalse(rg.RetryGrant.verify_backup(result["backup"], "0" * 64))
        with open(result["backup"], "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual("blocked", payload["prior_outcome"]["outcome"])
        self.assertEqual(0o600, os.stat(result["backup"]).st_mode & 0o777)

    def test_audit_history_is_append_only_and_ordered(self):
        self.grant.grant(ISSUE, REASON, self.approval(), OPERATOR)
        events = [e["event"] for e in self.grant.history()]
        self.assertEqual(["backup", "granted"], events)
        row = self.grant.history()[-1]
        self.assertEqual("blocked", row["prior_outcome"])
        self.assertEqual(OPERATOR, row["operator"])

    def test_limits_and_policy_are_preserved(self):
        before = self.ledger()
        self.grant.grant(ISSUE, REASON, self.approval(), OPERATOR)
        after = self.ledger()
        self.assertEqual(before["usage"], after["usage"])
        self.assertEqual(before["claims"], after["claims"])
        self.assertEqual(before["version"], after["version"])
        self.assertNotIn(str(ISSUE), after["processed"])
        self.assertEqual("available", after["retry_grants"][str(ISSUE)]["status"])
        self.assertIsNone(self.grant.pending_marker())

    def test_preview_writes_nothing(self):
        plan = self.grant.plan(ISSUE, REASON, self.approval(), OPERATOR)
        self.assertTrue(plan["dry_run"])
        self.assertTrue(plan["scope_ok"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, rg.AUDIT_NAME)))
        self.assertIn(str(ISSUE), self.ledger()["processed"])


# --------------------------------------------------------------------------
# review ask (5): one-time audit reliability
# --------------------------------------------------------------------------
class AuditReliabilityTests(BaseCase):
    def test_failure_during_apply_rolls_back(self):
        before = self.ledger()

        class Boom(rg.RetryGrant):
            def _after_apply(self, data):
                raise RuntimeError("injected failure")

        boom = Boom(self.tmp, scope_checker=ok_scope, allowed_operators=[OPERATOR],
                    approval_path=self.approval_path)
        with self.assertRaises(rg.GrantError):
            boom.grant(ISSUE, REASON, self.approval(), OPERATOR)
        self.assertEqual(before, self.ledger())
        self.assertEqual(["backup", "failed"],
                         [e["event"] for e in boom.history()])
        self.assertTrue(boom.history()[-1]["rolled_back"])
        self.assertIsNone(boom.pending_marker())

    def test_audit_failure_after_ledger_change_rolls_back(self):
        before = self.ledger()

        class NoGrantedAudit(rg.RetryGrant):
            def _append_audit(self, event):
                if event.get("event") == "granted":
                    raise OSError("audit filesystem full")
                return super()._append_audit(event)

        grant = NoGrantedAudit(self.tmp, scope_checker=ok_scope,
                               allowed_operators=[OPERATOR],
                               approval_path=self.approval_path)
        with self.assertRaises(rg.GrantError):
            grant.grant(ISSUE, REASON, self.approval(), OPERATOR)
        # the ledger must be back to exactly the prior state ...
        self.assertEqual(before, self.ledger())
        self.assertIn(str(ISSUE), self.ledger()["processed"])
        # ... the failure is recorded, and no stale pending marker is left
        events = [e["event"] for e in grant.history()]
        self.assertEqual(["backup", "failed"], events)
        self.assertFalse(grant.history()[-1]["rolled_back"] is False)
        self.assertIsNone(grant.pending_marker())
        # the artifact was NOT consumed, so a fixed retry can run again
        self.assertTrue(os.path.isfile(self.approval_path))

    def test_pending_marker_blocks_a_new_grant_until_resolved(self):
        with open(os.path.join(self.tmp, rg.PENDING_NAME), "w", encoding="utf-8") as fh:
            json.dump({"approval_id": "x", "started_at": "now"}, fh)
        with self.assertRaises(rg.GrantError):
            self.grant.grant(ISSUE, REASON, self.approval(), OPERATOR)
        self.assertIn(str(ISSUE), self.ledger()["processed"])


# --------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------
class ConcurrencyTests(BaseCase):
    def test_concurrent_grants_allow_only_one(self):
        # give every process its own copy of the approval artifact path
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


class PreviewScopeTokenTests(unittest.TestCase):
    """Regression: `preview` must install the token-scoped checker. Before the fix a dry
    run always reported "no GitHub token -- cannot confirm the live scope", so it could
    never PASS even with a token available."""

    def test_preview_installs_the_token_scoped_checker(self):
        import os
        import retry_grant as rg

        captured = {}

        def fake_default(issue, expected_body_sha256=None, token=None):
            captured["token"] = token
            return True, "ok"

        class StubGrant:
            """Stands in for RetryGrant so the test does not need a real worker ledger."""
            approval_path = "/nonexistent/approval.json"      # load_approval -> GrantError
            supported_issue = 19

            def __init__(self, *args, **kwargs):
                self.scope_checker = None

            def plan(self, *args, **kwargs):
                captured["checker"] = self.scope_checker         # what main() installed
                return {"dry_run": True, "scope_ok": True}

        orig_default, orig_cls = rg.default_scope_checker, rg.RetryGrant
        rg.default_scope_checker, rg.RetryGrant = fake_default, StubGrant
        try:
            os.environ["GITHUB_TOKEN"] = "probe-token-value"
            self.addCleanup(os.environ.pop, "GITHUB_TOKEN", None)
            rc = rg.main(["preview", "--issue", "19", "--reason", "probe", "--operator", "StevenMarketing"])
            self.assertEqual(rc, 0)
            checker = captured.get("checker")
            self.assertIsNotNone(checker, "preview did not install a scope checker")
            ok, _ = checker(19, None)
            self.assertTrue(ok)
            self.assertEqual(captured.get("token"), "probe-token-value")
        finally:
            rg.default_scope_checker, rg.RetryGrant = orig_default, orig_cls




if __name__ == "__main__":
    unittest.main()
