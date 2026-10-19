"""Regression tests for two verified production defects (owner-approved P0 fixes).

DEFECT 1: the validator recorded an accepted command in the live ledger, and the activation
gate then saw that same entry and refused every live command as `replay_duplicate`.
DEFECT 2: the watchdog read page 1 of the status issue (oldest-first), so once the issue
passed 100 comments it evaluated an ancient document and raised a false `status_stale`.
"""
import os as _os
import tempfile as _tempfile

# Test hygiene: keep every claim in this file inside its own claims directory,
# set BEFORE bridge_commands is imported (the class attribute reads it at import).
# Without this the tests share the real ops/bridge/ledger-claims path and a claim
# left by one test refuses a later test's first-time admission.
_os.environ["STEVEN_BRIDGE_CLAIMS_DIR"] = _tempfile.mkdtemp(prefix="bridge-claims-")

import json
import os
import sys
import tempfile
import time
import unittest
import uuid

from gbp.bridge.commands import CommandLedger, validate_command
from gbp.bridge.executor import CommandExecutor
from gbp.bridge.identity import TrustedContext
from gbp.bridge import live_queue as lq
from bridge_commands import LedgerSnapshot, live_gate_factory
from bridge_watchdog_check import newest_status_document, collect_and_evaluate
from gbp.bridge.external_watchdog import evaluate_external

REPO = "StevenMarketing/steven-ai-os"
OWNER = "StevenMarketing"
HEADINGS = ("## Goal\nx\n## Scope\nx\n## Acceptance Criteria\nx\n"
            "## Tests Required\nx\n## Constraints\nx\n")


def write_activation():
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"enabled": True, "operation": "queue_issue", "repository": REPO,
               "approved_by": OWNER, "expires_at": time.time() + 86400}, handle)
    handle.close()
    return handle.name


class FakeGitHub:
    """Unlabelled target so an enqueue is a real transition; records every label POST."""

    def __init__(self, *, post_error=None, confirm=True):
        self.post_error = post_error
        self.confirm = confirm
        self.label_posts = []

    def __call__(self, method, path, token=None, body=None):
        if method == "POST" and path.endswith("/labels"):
            self.label_posts.append(body)
            if self.post_error:
                raise RuntimeError(self.post_error)
            labels = [{"name": "hermes-ready"}] if self.confirm else []
            return ({"labels": labels}, {})
        if "/collaborators/" in path:
            return ({"permission": "admin"}, {})
        if "/issues/comments/" in path:
            return ({"user": {"login": OWNER}, "author_association": "OWNER"}, {})
        if "/issues/57" in path:
            return ({"number": 57, "state": "open", "body": HEADINGS, "labels": []}, {})
        return ([], {})


def envelope():
    now = time.time()
    unique = uuid.uuid4().hex[:10]          # unique per call: tests cannot collide on a key
    return {"operation": "queue_issue", "issue": 57, "request_id": "req-%s" % unique,
            "idempotency_key": "idem-%s" % unique, "actor": OWNER, "origin": REPO,
            "created_at": now, "ttl_seconds": 900, "dry_run": False}


def trusted():
    return TrustedContext(REPO, "issue_comment", 1, OWNER, "OWNER", "admin", "issue_comment", True)


class Defect1Tests(unittest.TestCase):
    def setUp(self):
        self.activation = write_activation()
        self.addCleanup(os.unlink, self.activation)

    def _run(self, gh, env, snapshot, *, live_ledger=None):
        guest = live_ledger or CommandLedger()
        validated = validate_command(env, now=time.time(), ledger=guest,
                                     budget_guard=lambda op, i=None: {"allowed": True},
                                     live_queue_allowed=True)
        gate = live_gate_factory(gh, "tok", REPO, processed={}, ledger=snapshot,
                                 budget_guard=lambda op, i=None: {"allowed": True},
                                 activation_path=self.activation)
        return validated, CommandExecutor(trace_path=tempfile.mktemp(),
                                          live_gate=gate).execute(validated, trusted())

    def test_first_time_admission_succeeds_with_a_snapshot_gate(self):
        gh = FakeGitHub()
        env = envelope()
        validated, receipt = self._run(gh, env, LedgerSnapshot())
        self.assertTrue(validated["accepted"], validated.get("errors"))
        self.assertEqual(receipt["state"], "completed", receipt["result"])
        self.assertTrue(receipt["result"]["mutated"])
        self.assertEqual(len(gh.label_posts), 1)

    def test_genuine_replay_from_a_previous_run_is_refused(self):
        gh = FakeGitHub()
        env = envelope()
        snapshot = LedgerSnapshot()
        snapshot.record(env["idempotency_key"], env["request_id"])   # "already ran before restart"
        validated, receipt = self._run(gh, env, snapshot)
        self.assertTrue(validated["accepted"])                       # validator has a fresh ledger
        self.assertFalse(receipt["result"].get("mutated", False))
        self.assertIn("replay", json.dumps(receipt["result"]).lower())
        self.assertEqual(gh.label_posts, [])

    def test_concurrent_duplicate_within_one_run_mutates_once(self):
        gh = FakeGitHub()
        env = envelope()
        live = CommandLedger()
        snapshot = LedgerSnapshot()
        _, first = self._run(gh, env, snapshot, live_ledger=live)
        snapshot.record(env["idempotency_key"], env["request_id"])   # runner records after execution
        second = validate_command(env, now=time.time(), ledger=live,
                                  budget_guard=lambda op, i=None: {"allowed": True},
                                  live_queue_allowed=True)
        self.assertFalse(second["accepted"])
        self.assertEqual(second.get("replay"), "duplicate")
        self.assertEqual(len(gh.label_posts), 1)
        self.assertTrue(first["result"]["mutated"])

    def test_api_failure_and_unconfirmed_mutation_are_refusals(self):
        for gh in (FakeGitHub(post_error="502"), FakeGitHub(confirm=False)):
            _, receipt = self._run(gh, envelope(), LedgerSnapshot())
            self.assertFalse(receipt["result"].get("mutated", False), receipt["result"])

    def test_snapshot_round_trips_through_the_persisted_file(self):
        snapshot = LedgerSnapshot()
        snapshot.record("k1", "r1")
        path = tempfile.mktemp()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(snapshot.to_dict(), handle)
        restored = LedgerSnapshot.from_file(path)
        self.assertEqual(restored.seen("k1"), "k1")
        self.assertEqual(restored.seen_request("r1"), "r1")
        self.assertIsNone(restored.seen("other"))
        os.unlink(path)


class Defect2Tests(unittest.TestCase):
    def _status(self, generated_at, extra=None):
        doc = {"schema": "steven-ai-os/worker-status@1", "repository": REPO,
               "generated_at": generated_at, "worker": {"state": "idle"},
               "cron": {"state": "scheduled"}, "budget": {}}
        doc.update(extra or {})
        return "header\n```json\n%s\n```\n" % json.dumps(doc)

    def test_newest_status_is_found_when_the_issue_has_over_100_comments(self):
        old = [{"body": self._status("2026-01-01T00:00:00Z")} for _ in range(120)]
        newest = {"body": self._status("2026-10-19T16:08:00Z")}
        seen_paths = []

        def request(method, path, token=None, body=None):
            seen_paths.append(path)
            if "direction=desc" in path:
                return ([newest] + old[:99], {})           # newest first
            return (old, {})                               # untrusted ordering: oldest first

        found = newest_status_document(request, "tok", REPO, 46)
        self.assertEqual(found["generated_at"], "2026-10-19T16:08:00Z")
        self.assertTrue(any("direction=desc" in p for p in seen_paths))

    def test_malformed_newest_reports_unreadable_with_an_older_healthy_document(self):
        """The point of the fix: a broken newest status must ALERT, never fall back."""
        healthy = self._status("2026-10-19T15:00:00Z")
        page = [{"body": "```json\n{not json at all\n```"}, {"body": healthy}]

        def request(*a, **k):
            return (page, {})

        found = newest_status_document(request, "tok", REPO, 46)
        self.assertIsNotNone(found)
        self.assertEqual(found.get("reason"), "malformed_newest_status")
        self.assertIsNone(found.get("generated_at"))
        out = evaluate_external(found, now=time.time())
        self.assertFalse(out["ok"])
        self.assertIn("status_unreadable", [a["code"] for a in out["alerts"]])

    def test_newest_without_a_timestamp_is_unreadable_not_a_fallback(self):
        page = [{"body": '```json\n{"worker": {"state": "idle"}}\n```'},
                {"body": self._status("2026-10-19T15:00:00Z")}]

        def request(*a, **k):
            return (page, {})

        found = newest_status_document(request, "tok", REPO, 46)
        self.assertEqual(found.get("reason"), "newest_status_without_timestamp")
        self.assertFalse(evaluate_external(found, now=time.time())["ok"])

    def test_api_failure_is_not_hidden(self):
        def request(*a, **k):
            raise RuntimeError("403")

        with self.assertRaises(RuntimeError):
            newest_status_document(request, "tok", REPO, 46)

    def test_missing_timestamp_still_alerts_and_genuine_stale_still_alerts(self):
        no_timestamp = {"generated_at": None, "worker": {}, "cron": {}, "budget": {}}
        out = evaluate_external(no_timestamp, now=time.time())
        self.assertFalse(out["ok"])
        self.assertIn("status_unreadable", [a["code"] for a in out["alerts"]])

        genuinely_old = {"generated_at": "2026-10-19T00:00:00Z", "worker": {}, "cron": {}, "budget": {}}
        out = evaluate_external(genuinely_old, now=time.time(), max_age_seconds=900)
        self.assertFalse(out["ok"])
        self.assertIn("status_stale", [a["code"] for a in out["alerts"]])

    def test_fresh_status_after_the_fix_does_not_alert(self):
        fresh = json.loads(self._status("2026-10-19T16:08:00Z").split("```json")[1].split("```")[0])
        from datetime import datetime, timezone

        now = datetime(2026, 10, 19, 16, 10, tzinfo=timezone.utc).timestamp()
        self.assertTrue(evaluate_external(fresh, now=now, max_age_seconds=900)["ok"])


if __name__ == "__main__":
    unittest.main()



class CrossProcessReplayTests(unittest.TestCase):
    """Two REAL runner processes racing on one command: exactly one admits the mutation."""

    CHILD = r"""
import json, os, sys, time
ROOT = os.environ["FIX2ROOT"]
sys.path.insert(0, os.path.join(ROOT, "src")); sys.path.insert(0, os.path.join(ROOT, "scripts"))
while not os.path.exists(os.environ["GOFILE"]):
    time.sleep(0.005)
from gbp.bridge.commands import CommandLedger, validate_command
from gbp.bridge.identity import TrustedContext
from bridge_commands import LedgerSnapshot, live_gate_factory

REPO = "StevenMarketing/steven-ai-os"; OWNER = "StevenMarketing"
HEADINGS = "## Goal\nx\n## Scope\nx\n## Acceptance Criteria\nx\n## Tests Required\nx\n## Constraints\nx\n"


class GH:
    def __init__(self):
        self.label_posts = []

    def __call__(self, method, path, token=None, body=None):
        if method == "POST" and path.endswith("/labels"):
            self.label_posts.append(body); return ({"labels": [{"name": "hermes-ready"}]}, {})
        if "/collaborators/" in path: return ({"permission": "admin"}, {})
        if "/issues/comments/" in path: return ({"user": {"login": OWNER}, "author_association": "OWNER"}, {})
        if "/labels" in path: return ([{"name": "hermes-ready"}], {})
        if "/issues/57" in path: return ({"number": 57, "state": "open", "body": HEADINGS, "labels": []}, {})
        return ([], {})


env = json.loads(os.environ["ENVELOPE"])
gh = GH()
validated = validate_command(env, now=time.time(), ledger=CommandLedger(),
                             budget_guard=lambda op, i=None: {"allowed": True},
                             live_queue_allowed=True)
gate = live_gate_factory(gh, "tok", REPO, processed={}, ledger=LedgerSnapshot(),
                         budget_guard=lambda op, i=None: {"allowed": True},
                         activation_path=os.environ["ACTIVATION"])
try:
    receipt = gate(validated, TrustedContext(REPO, "issue_comment", 1, OWNER, "OWNER", "admin",
                                             "issue_comment", True))
except Exception as exc:                      # a refusal may surface as an exception
    receipt = {"result": {"mutated": False, "reason": str(exc)[:120]}}
blob = json.dumps(receipt)
print(json.dumps({"accepted": bool(validated.get("accepted")) if isinstance(validated, dict) else None,
                  "label_posts": len(gh.label_posts),
                  "mutated": "\"mutated\": true" in blob.replace("True", "true"),
                  "raw": blob[:200]}))
"""

    def test_two_processes_racing_on_the_same_command_mutate_exactly_once(self):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        claims = tempfile.mkdtemp()
        go = os.path.join(claims, "go")
        activation = write_activation()
        self.addCleanup(os.unlink, activation)

        child = tempfile.mktemp(suffix=".py")
        with open(child, "w", encoding="utf-8") as handle:
            handle.write(self.CHILD)

        env = dict(os.environ)
        env.update({"FIX2ROOT": root, "GOFILE": go, "ACTIVATION": activation,
                    "STEVEN_BRIDGE_CLAIMS_DIR": claims,
                    "PYTHONPATH": os.path.join(root, "src") + os.pathsep + os.path.join(root, "scripts")})
        # ONE envelope shared by both processes: same idempotency key and request id,
        # which is the only construction the race is about.
        shared = json.dumps(envelope())
        outs = []
        procs = []
        for _ in range(2):
            env["ENVELOPE"] = shared
            procs.append(subprocess.Popen([sys.executable, child], env=env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        time.sleep(0.2)
        open(go, "w").close()                      # release both processes together
        for proc in procs:
            out, err = proc.communicate(timeout=90)
            outs.append((out.decode() or err.decode())[-300:])

        parsed = []
        for text in outs:
            try:
                parsed.append(json.loads(text.strip().splitlines()[-1]))
            except Exception:
                parsed.append({"mutated": False, "raw": text})
        mutated = [p for p in parsed if p.get("mutated")]
        posts = sum(int(p.get("label_posts") or 0) for p in parsed)
        self.assertEqual(len(mutated), 1, parsed)          # exactly one winner
        self.assertEqual(posts, 1, parsed)                 # exactly one label mutation
        self.assertTrue(all(p.get("accepted") for p in parsed))   # both passed validation


if __name__ == "__main__":
    unittest.main()


class PartialClaimRecoveryTests(unittest.TestCase):
    """Fix 1: a claim that fails halfway must leave no residue and must not wedge the command."""

    def setUp(self):
        self.snapshot = LedgerSnapshot(claims_dir=tempfile.mkdtemp())

    def test_partial_claim_is_unwound(self):
        # another process already holds the request id
        self.assertTrue(self.snapshot.claim(None, "req-held"))
        self.assertFalse(self.snapshot.claim("idem-fresh", "req-held"))
        self.assertFalse(os.path.exists(self.snapshot._claim_path("idem-fresh")),
                         "the idempotency-key claim created before the failure must be unwound")
        self.assertIsNone(self.snapshot.seen("idem-fresh"))

    def test_recovery_after_a_partial_failure_is_possible(self):
        self.snapshot.claim(None, "req-held")
        self.snapshot.claim("idem-recover", "req-held")          # fails, unwinds
        # the same key with a free request id is admissible again: no permanent false rejection
        self.assertTrue(self.snapshot.claim("idem-recover", "req-fresh"))
        self.assertEqual(self.snapshot.seen("idem-recover"), "idem-recover")

    def test_already_held_identifier_is_a_conflict_not_a_noop(self):
        self.assertTrue(self.snapshot.claim("idem-x", "req-x"))
        # claiming again must FAIL (this was the defect: it returned True and left residue)
        self.assertFalse(self.snapshot.claim("idem-x", "req-x"))
        self.assertFalse(self.snapshot.claim("idem-x", None))

    def test_replay_protection_survives_the_fix(self):
        self.assertTrue(self.snapshot.claim("idem-y", "req-y"))
        other = LedgerSnapshot(claims_dir=self.snapshot.claims_dir)   # another process, same dir
        self.assertEqual(other.seen("idem-y"), "idem-y")             # visible on disk
        self.assertEqual(other.seen_request("req-y"), "req-y")
        self.assertFalse(other.claim("idem-y", "req-y"))             # genuine replay still refused


if __name__ == "__main__":
    unittest.main()


class ConcurrentPersistenceTests(unittest.TestCase):
    """Fix 2: two processes persisting at once must not lose each other's identifiers."""

    CHILD_SAVE = r"""
import json, os, sys, time
ROOT = os.environ["FIX2ROOT"]
sys.path.insert(0, os.path.join(ROOT, "src")); sys.path.insert(0, os.path.join(ROOT, "scripts"))
while not os.path.exists(os.environ["GOFILE"]):
    time.sleep(0.005)
from bridge_commands import LedgerSnapshot
snap = LedgerSnapshot()                 # claims dir comes from STEVEN_BRIDGE_CLAIMS_DIR
snap.claim(os.environ["K"], os.environ["R"])
for _ in range(25):                      # persist repeatedly, the way a runner does
    snap.save(os.environ["LEDGER"])
print("saved")
"""

    def test_two_processes_saving_concurrently_keep_every_identifier(self):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        work = tempfile.mkdtemp()
        go = os.path.join(work, "go")
        ledger = os.path.join(work, "command-ledger.json")
        child = tempfile.mktemp(suffix=".py")
        with open(child, "w", encoding="utf-8") as handle:
            handle.write(self.CHILD_SAVE)

        base = dict(os.environ)
        base.update({"FIX2ROOT": root, "GOFILE": go, "LEDGER": ledger,
                     "STEVEN_BRIDGE_CLAIMS_DIR": os.path.join(work, "claims"),
                     "PYTHONPATH": os.path.join(root, "src") + os.pathsep + os.path.join(root, "scripts")})
        procs = []
        for index in (1, 2):
            procs.append(subprocess.Popen([sys.executable, child],
                                          env=dict(base, K="idem-%d" % index, R="req-%d" % index),
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        time.sleep(0.2)
        open(go, "w").close()                 # release both writers together
        for proc in procs:
            out, err = proc.communicate(timeout=90)
            self.assertIn("saved", out.decode(), err.decode()[-300:])

        # after a "restart": a fresh read of the persisted ledger
        restored = LedgerSnapshot.from_file(ledger)
        for index in (1, 2):
            self.assertEqual(restored.seen("idem-%d" % index), "idem-%d" % index,
                             "a concurrently persisted idempotency key was lost")
            self.assertEqual(restored.seen_request("req-%d" % index), "req-%d" % index,
                             "a concurrently persisted request id was lost")

    def test_save_never_overwrites_identifiers_it_does_not_know(self):
        work = tempfile.mkdtemp()
        ledger = os.path.join(work, "ledger.json")
        first = LedgerSnapshot(claims_dir=os.path.join(work, "c1"))
        first.claim("idem-a", "req-a")
        first.save(ledger)
        second = LedgerSnapshot(claims_dir=os.path.join(work, "c2"))
        second.claim("idem-b", "req-b")
        second.save(ledger)                    # must merge, not replace
        restored = LedgerSnapshot.from_file(ledger)
        for value in ("idem-a", "req-a", "idem-b", "req-b"):
            self.assertEqual(restored.seen(value) or restored.seen_request(value), value)


if __name__ == "__main__":
    unittest.main()


class PersistenceFailureTests(unittest.TestCase):
    """Phase 1.5: a failed save must be visible and must never be swallowed."""

    def test_unwritable_ledger_raises_instead_of_silently_dropping_state(self):
        from bridge_commands import LedgerPersistenceError
        work = tempfile.mkdtemp()
        blocker = os.path.join(work, "not-a-directory")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        snapshot = LedgerSnapshot(claims_dir=os.path.join(work, "claims"))
        snapshot.claim("idem-p", "req-p")
        with self.assertRaises(LedgerPersistenceError):
            snapshot.save(os.path.join(blocker, "command-ledger.json"))

    def test_successful_save_is_durable_for_the_next_reader(self):
        work = tempfile.mkdtemp()
        ledger = os.path.join(work, "command-ledger.json")
        snapshot = LedgerSnapshot(claims_dir=os.path.join(work, "claims"))
        snapshot.claim("idem-d", "req-d")
        snapshot.save(ledger)
        restored = LedgerSnapshot.from_file(ledger)
        self.assertEqual(restored.seen("idem-d"), "idem-d")


if __name__ == "__main__":
    unittest.main()


class CrashRecoveryV2Tests(unittest.TestCase):
    """Phase 2: durable PENDING/CONFIRMED/FAILED/UNKNOWN, reconciliation, no auto-retry.

    Every case reuses the SAME envelope object (the earlier attempt's oracle built a new
    envelope per call, which is why it reported a mutation while a claim was pending).
    A spy on ``gbp.bridge.live_queue.evaluate_live_queue`` proves the real gate path runs.
    """

    def setUp(self):
        self.claims = tempfile.mkdtemp()
        self.activation = write_activation()
        self.addCleanup(os.unlink, self.activation)
        self.calls = []

    def _snapshot(self):
        return LedgerSnapshot(claims_dir=self.claims)

    def _submit(self, snapshot, gh, payload):
        """Run the REAL path: live_gate_factory -> evaluate_live_queue -> mutation."""
        from unittest import mock
        from gbp.bridge import live_queue as lq
        from gbp.bridge.executor import CommandExecutor

        original = lq.evaluate_live_queue

        def spying(**kwargs):
            self.calls.append(kwargs.get("validated", {}).get("request_id"))
            return original(**kwargs)

        gate = live_gate_factory(gh, "tok", REPO, processed={}, ledger=snapshot,
                                 budget_guard=lambda op, i=None: {"allowed": True},
                                 activation_path=self.activation)
        validated = validate_command(payload, now=time.time(), ledger=CommandLedger(),
                                     budget_guard=lambda op, i=None: {"allowed": True},
                                     live_queue_allowed=True)
        executor = CommandExecutor(trace_path=tempfile.mktemp(), live_gate=gate)
        with mock.patch.object(lq, "evaluate_live_queue", spying):
            return executor.execute(validated, trusted())

    def test_oracle_uses_the_real_gate_path(self):
        snapshot, gh, payload = self._snapshot(), FakeGitHub(), envelope()
        execution = self._submit(snapshot, gh, payload)
        self.assertEqual(self.calls, [payload["request_id"]])      # the real gate really ran
        self.assertTrue(execution["result"]["mutated"])
        self.assertEqual(len(gh.label_posts), 1)
        self.assertEqual(snapshot.pending_state(payload["idempotency_key"]), "pending")

    def test_pending_is_refused_without_any_post(self):
        snapshot, gh, payload = self._snapshot(), FakeGitHub(), envelope()
        self.assertTrue(snapshot.claim(payload["idempotency_key"], payload["request_id"]))
        execution = self._submit(snapshot, gh, payload)            # the SAME command
        self.assertIn("mutation_pending_unknown", json.dumps(execution))
        self.assertEqual(gh.label_posts, [])
        self.assertEqual(snapshot.pending_state(payload["idempotency_key"]), "pending")

    def test_malformed_marker_is_unknown_and_kept(self):
        snapshot, gh, payload = self._snapshot(), FakeGitHub(), envelope()
        path = snapshot._claim_path(payload["idempotency_key"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        execution = self._submit(snapshot, gh, payload)
        self.assertIn("mutation_pending_unknown", json.dumps(execution))
        self.assertEqual(gh.label_posts, [])
        self.assertTrue(os.path.exists(path), "a marker is never deleted automatically")
        self.assertEqual(snapshot.pending_state(payload["idempotency_key"]), "unknown")

    def test_label_present_confirms_and_a_replay_is_refused(self):
        snapshot, gh, payload = self._snapshot(), FakeGitHub(), envelope()
        snapshot.claim(payload["idempotency_key"], payload["request_id"])
        decision = snapshot.reconcile(payload["idempotency_key"], label_present=True)
        self.assertEqual(decision["state"], "confirmed")
        self.assertTrue(decision["attribution_uncertain"])
        execution = self._submit(snapshot, gh, payload)
        self.assertIn("replay_duplicate", json.dumps(execution))
        self.assertEqual(gh.label_posts, [])

    def test_label_absent_is_unknown_not_failed_even_when_old(self):
        snapshot, payload = self._snapshot(), envelope()
        snapshot.claim(payload["idempotency_key"], payload["request_id"])
        decision = snapshot.reconcile(payload["idempotency_key"], label_present=False,
                                      now=time.time() + 10 * 86400)
        self.assertEqual(decision["state"], "unknown")
        self.assertTrue(decision["requires_manual_review"])
        gh = FakeGitHub()
        self.assertIn("mutation_pending_unknown", json.dumps(self._submit(snapshot, gh, payload)))
        self.assertEqual(gh.label_posts, [])

    def test_api_unavailable_keeps_unknown_and_manual_review(self):
        snapshot, payload = self._snapshot(), envelope()
        snapshot.claim(payload["idempotency_key"], payload["request_id"])
        decision = snapshot.reconcile(payload["idempotency_key"], label_present=None, api_error=True)
        self.assertEqual(decision["state"], "unknown")
        self.assertTrue(decision["requires_manual_review"])
        self.assertEqual(snapshot.pending_state(payload["idempotency_key"]), "unknown")

    def test_authoritative_failure_settles_failed_and_a_new_command_succeeds(self):
        snapshot, gh, old = self._snapshot(), FakeGitHub(), envelope()
        snapshot.claim(old["idempotency_key"], old["request_id"])
        decision = snapshot.reconcile(old["idempotency_key"], label_present=False,
                                      authoritative_failure=True)
        self.assertEqual(decision["state"], "failed")
        fresh = envelope()
        execution = self._submit(snapshot, gh, fresh)
        self.assertTrue(execution["result"]["mutated"])
        self.assertEqual(len(gh.label_posts), 1)

    def test_state_survives_a_restart(self):
        payload = envelope()
        first = self._snapshot()
        first.claim(payload["idempotency_key"], payload["request_id"])
        first.settle(payload["idempotency_key"], payload["request_id"], "confirmed", "label_confirmed")
        restarted = self._snapshot()                     # a brand-new reader of the same dir
        self.assertEqual(restarted.pending_state(payload["idempotency_key"]), "confirmed")
        gh = FakeGitHub()
        self.assertIn("replay_duplicate", json.dumps(self._submit(restarted, gh, payload)))
        self.assertEqual(gh.label_posts, [])

    def test_stale_pending_is_still_refused(self):
        snapshot, gh, payload = self._snapshot(), FakeGitHub(), envelope()
        snapshot.claim(payload["idempotency_key"], payload["request_id"])
        path = snapshot._claim_path(payload["idempotency_key"])
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"state": "pending", "value": payload["idempotency_key"], "created_at": 1}, handle)
        self.assertIn("mutation_pending_unknown", json.dumps(self._submit(snapshot, gh, payload)))
        self.assertEqual(gh.label_posts, [])             # age never triggers an automatic retry

    def test_transport_failure_settles_unknown_by_the_runner(self):
        snapshot, payload = self._snapshot(), envelope()
        snapshot.claim(payload["idempotency_key"], payload["request_id"])
        from bridge_commands import _settle_ledger_for_execution
        state = _settle_ledger_for_execution(snapshot, payload, {
            "result": {"mutated": False,
                       "mutation": {"attempted": True, "mutated": False,
                                    "error": "mutation_transport_error: 502"}}})
        self.assertEqual(state, "unknown")

    def test_settle_does_not_wait_on_the_ledger_lock(self):
        import fcntl
        work = tempfile.mkdtemp()
        ledger = os.path.join(work, "command-ledger.json")
        snapshot = LedgerSnapshot(claims_dir=os.path.join(work, "claims"))
        snapshot.claim("idem-lock", "req-lock")
        with open(ledger + ".lock", "a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            snapshot.settle("idem-lock", "req-lock", "confirmed", "test")   # must not block
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(snapshot.pending_state("idem-lock"), "confirmed")

    def test_concurrent_claims_have_exactly_one_winner(self):
        payload = envelope()
        first, second = self._snapshot(), self._snapshot()
        results = [first.claim(payload["idempotency_key"], payload["request_id"]),
                   second.claim(payload["idempotency_key"], payload["request_id"])]
        self.assertEqual(sorted(results), [False, True])


if __name__ == "__main__":
    unittest.main()
