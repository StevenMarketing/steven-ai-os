"""Regression tests for two verified production defects (owner-approved P0 fixes).

DEFECT 1: the validator recorded an accepted command in the live ledger, and the activation
gate then saw that same entry and refused every live command as `replay_duplicate`.
DEFECT 2: the watchdog read page 1 of the status issue (oldest-first), so once the issue
passed 100 comments it evaluated an ancient document and raised a false `status_stale`.
"""
import json
import os
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

    def test_malformed_newest_falls_back_to_the_next_parsable(self):
        comments = [{"body": "no fence here"}, {"body": "```json\n{bad json\n```"},
                    {"body": self._status("2026-10-19T15:00:00Z")}]

        def request(*a, **k):
            return (comments, {})

        self.assertEqual(newest_status_document(request, "tok", REPO, 46)["generated_at"],
                         "2026-10-19T15:00:00Z")

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


class LegacyOrderingStubTests(unittest.TestCase):
    """A page handed back in list order with no timestamps keeps the legacy last-wins pick."""

    def test_untimestamped_page_still_returns_the_last_document(self):
        from bridge_watchdog_check import newest_status_document

        def request(*a, **k):
            return ([{"worker": {"state": "older"}}, {"worker": {"state": "idle"}}], {})

        self.assertEqual(newest_status_document(request, "t", REPO, 46)["worker"]["state"], "idle")
