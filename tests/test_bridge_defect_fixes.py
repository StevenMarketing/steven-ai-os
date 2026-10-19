"""Regression tests for two verified production defects (owner-approved P0 fixes).

DEFECT 1: the validator recorded an accepted command in the live ledger, and the activation
gate then saw that same entry and refused every live command as `replay_duplicate`.
DEFECT 2: the watchdog read page 1 of the status issue (oldest-first), so once the issue
passed 100 comments it evaluated an ancient document and raised a false `status_stale`.
"""
import json
import os
import sys
import tempfile
import time
import unittest

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
    return {"operation": "queue_issue", "issue": 57, "request_id": "req-%d" % int(now * 1e3),
            "idempotency_key": "idem-%d" % int(now * 1e3), "actor": OWNER, "origin": REPO,
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


class PartialClaimAndPendingTests(unittest.TestCase):
    """Edge 1: a half-created claim must not persist, and an interrupted attempt must be
    reported as an UNKNOWN outcome rather than as a confirmed failure."""

    def setUp(self):
        self.claims = tempfile.mkdtemp()
        self.snapshot = LedgerSnapshot(claims_dir=self.claims)
        self.activation = write_activation()
        self.addCleanup(os.unlink, self.activation)

    def _gate(self, snapshot):
        return live_gate_factory(FakeGitHub(), "tok", REPO, processed={}, ledger=snapshot,
                                 budget_guard=lambda op, i=None: {"allowed": True},
                                 activation_path=self.activation)

    def _validated(self, payload):
        return validate_command(payload, now=time.time(), ledger=CommandLedger(),
                                budget_guard=lambda op, i=None: {"allowed": True},
                                live_queue_allowed=True)

    def test_partial_claim_rolls_back_and_leaves_no_residue(self):
        self.assertTrue(self.snapshot.claim(None, "req-held"))          # another process holds this
        self.assertFalse(self.snapshot.claim("idem-fresh", "req-held"))  # partial failure
        self.assertFalse(os.path.exists(self.snapshot._claim_path("idem-fresh")),
                         "the key claim created before the failure must be rolled back")
        self.assertEqual(self.snapshot.pending_state("idem-fresh"), None)
        # recovery: the same idempotency key with a free request id is admissible again,
        # so a partial failure cannot wedge a legitimate command forever
        self.assertTrue(self.snapshot.claim("idem-fresh", "req-free"))

    def test_crash_between_claim_and_mutation_is_pending_then_settles(self):
        payload = envelope()
        payload["idempotency_key"] = "idem-crash"
        payload["request_id"] = "req-crash"
        self.assertTrue(self.snapshot.claim("idem-crash", "req-crash"))   # claimed, then "crash"
        self.assertEqual(self.snapshot.pending_state("idem-crash"), "pending")

        receipt = self._gate(self.snapshot)(self._validated(payload), trusted())
        self.assertIn("mutation_pending_unknown", json.dumps(receipt))   # unknown, not failure
        self.assertFalse((receipt.get("result") or {}).get("mutated", False))

        # a settled, confirmed outcome is a genuine replay ...
        self.snapshot.settle("idem-crash", "req-crash", "confirmed")
        self.assertEqual(self.snapshot.pending_state("idem-crash"), "confirmed")
        self.assertIn("replay_duplicate",
                      json.dumps(self._gate(self.snapshot)(self._validated(payload), trusted())))
        # ... and a settled failure is also distinct from pending
        self.snapshot.settle("idem-crash", "req-crash", "failed")
        self.assertEqual(self.snapshot.pending_state("idem-crash"), "failed")

    def test_pending_claim_still_blocks_any_second_mutation(self):
        gh = FakeGitHub()
        gate = live_gate_factory(gh, "tok", REPO, processed={}, ledger=self.snapshot,
                                 budget_guard=lambda op, i=None: {"allowed": True},
                                 activation_path=self.activation)
        payload = envelope()
        self.snapshot.claim(payload["idempotency_key"], payload["request_id"])
        gate(self._validated(payload), trusted())
        self.assertEqual(gh.label_posts, [])


class ConcurrentPersistenceTests(unittest.TestCase):
    """Edge 2: two processes persisting at once must not lose recorded identifiers."""

    CHILD_SAVE = r"""
import json, os, sys, time
ROOT = os.environ["FIX2ROOT"]
sys.path.insert(0, os.path.join(ROOT, "src")); sys.path.insert(0, os.path.join(ROOT, "scripts"))
while not os.path.exists(os.environ["GOFILE"]):
    time.sleep(0.005)
from bridge_commands import LedgerSnapshot
snap = LedgerSnapshot()
snap.claim(os.environ["K"], os.environ["R"])
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
            env = dict(base, K="idem-%d" % index, R="req-%d" % index)
            procs.append(subprocess.Popen([sys.executable, child], env=env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        time.sleep(0.2)
        open(go, "w").close()                      # both release together, then race
        for proc in procs:
            out, err = proc.communicate(timeout=90)
            self.assertIn("saved", out.decode(), err.decode()[-200:])

        # ... and after a "restart" (a fresh read of the persisted file)
        restored = LedgerSnapshot.from_file(ledger)
        for index in (1, 2):
            self.assertEqual(restored.seen("idem-%d" % index), "idem-%d" % index,
                             "a concurrently persisted key was lost")
            self.assertEqual(restored.seen_request("req-%d" % index), "req-%d" % index,
                             "a concurrently persisted request id was lost")


if __name__ == "__main__":
    unittest.main()
