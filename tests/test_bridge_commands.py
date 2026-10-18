"""Issue #49 end-to-end tests for the two-way command bridge (offline transport)."""
import json
import time
import unittest

from gbp.bridge.commands import CommandLedger
from gbp.bridge.executor import CommandExecutor
from gbp.bridge.identity import TrustedContext
from bridge_commands import process_comment, scan

REPO = "StevenMarketing/steven-ai-os"
OWNER = "StevenMarketing"


def envelope(**kw):
    now = time.time()
    base = {"request_id": "req-%d" % int(now * 1000), "idempotency_key": "idem-%d" % int(now * 1000),
            "operation": "status", "actor": OWNER, "origin": REPO, "created_at": now,
            "ttl_seconds": 900, "dry_run": True}
    base.update(kw)
    return base


class FakeGitHub:
    """Minimal transport: comments carry commands, posted bodies are recorded."""

    def __init__(self, comments, *, author=OWNER, permission="admin", issue_body=None,
                 issue_labels=("hermes-ready",)):
        self.comments = comments
        self.author = author
        self.permission = permission
        self.issue_body = issue_body or "## Goal\nx\n## Scope\nx\n## Acceptance Criteria\nx\n## Tests Required\nx\n## Constraints\nx"
        self.issue_labels = list(issue_labels)
        self.posted = []

    def __call__(self, method, path, token=None, body=None):
        if method == "POST" and path.endswith("/comments"):
            self.posted.append(body["body"])
            return ({"html_url": "https://example.test/c%d" % len(self.posted)}, {})
        if "/issues/comments/" in path:
            cid = int(path.rsplit("/", 1)[1])
            return ({"user": {"login": self.author}, "author_association": "OWNER",
                     "id": cid}, {})
        if "/collaborators/" in path and path.endswith("/permission"):
            return ({"permission": self.permission}, {})
        if "/issues/49" in path and path.endswith("/49"):
            return ({"number": 49, "state": "open", "body": self.issue_body,
                     "labels": [{"name": n} for n in self.issue_labels]}, {})
        if "comments?per_page" in path:
            return (self.comments, {})
        raise AssertionError(path)


def executor():
    return CommandExecutor(trace_path="/tmp/issue49-trace.jsonl",
                           status_document=lambda: {"generated_at": "x", "cron": {}, "worker": {}})


class ExecutorTests(unittest.TestCase):
    def test_status_is_read_only_and_completes(self):
        from gbp.bridge.commands import validate_command

        now = time.time()
        env = envelope(created_at=now)
        validated = validate_command(env, now=now, ledger=CommandLedger())
        self.assertTrue(validated["accepted"], validated.get("errors"))
        trusted = TrustedContext(REPO, "issue_comment", 1, OWNER, "OWNER", "admin",
                                 "issue_comment", True)
        out = executor().execute(validated, trusted)
        self.assertEqual(out["state"], "completed")
        self.assertEqual(out["transitions"], ["queued", "claimed", "completed"])
        self.assertTrue(out["result"]["read_only"])

    def test_live_queue_issue_is_rejected_fail_closed(self):
        from gbp.bridge.commands import validate_command

        now = time.time()
        env = envelope(operation="queue_issue", issue=49, dry_run=False, created_at=now)
        validated = validate_command(env, now=now, ledger=CommandLedger(),
                                     budget_guard=lambda op, i=None: {"allowed": True})
        trusted = TrustedContext(REPO, "issue_comment", 1, OWNER, "OWNER", "admin",
                                 "issue_comment", True)
        out = executor().execute(validated, trusted)
        self.assertEqual(out["state"], "rejected")
        self.assertIn("phase1_read_only", out["result"]["reason"])
        self.assertFalse(out["result"]["mutated"])


class RunnerTests(unittest.TestCase):
    def _comment(self, env):
        return {"id": 777, "body": "prose\n```command\n%s\n```\n" % json.dumps(env)}

    def test_authorized_status_is_acknowledged_and_answered_on_github(self):
        gh = FakeGitHub([self._comment(envelope())])
        out = process_comment(gh, "tok", REPO, dict(gh.comments[0], _issue=49),
                              executor=executor(), ledger=CommandLedger(), processed={})
        self.assertEqual(out["action"], "executed")
        self.assertTrue(out["verified"])
        body = "\n".join(gh.posted)
        self.assertIn("Command acknowledged", body)
        self.assertIn("Command result — `claimed -> completed`", body)
        self.assertIn("request_id", body)
        self.assertNotIn("sk-", body)

    def test_a_comment_that_only_claims_to_be_the_owner_executes_nothing(self):
        gh = FakeGitHub([self._comment(envelope())], author="attacker", permission="admin")
        out = process_comment(gh, "tok", REPO, dict(gh.comments[0], _issue=49),
                              executor=executor(), ledger=CommandLedger(), processed={})
        self.assertEqual(out["action"], "rejected")
        self.assertFalse(out["verified"])
        self.assertIn("Command rejected", "\n".join(gh.posted))
        self.assertNotIn("Command result", "\n".join(gh.posted))

    def test_unknown_field_in_the_envelope_is_rejected_with_errors_shown(self):
        bad = envelope()
        bad["repository"] = REPO                      # not in the schema
        gh = FakeGitHub([self._comment(bad)])
        out = process_comment(gh, "tok", REPO, dict(gh.comments[0], _issue=49),
                              executor=executor(), ledger=CommandLedger(), processed={})
        self.assertEqual(out["action"], "rejected")
        self.assertIn("errors", "\n".join(gh.posted))

    def test_the_same_comment_is_never_executed_twice(self):
        gh = FakeGitHub([self._comment(envelope())])
        processed = {}
        first = process_comment(gh, "tok", REPO, dict(gh.comments[0], _issue=49),
                                executor=executor(), ledger=CommandLedger(), processed=processed)
        second = process_comment(gh, "tok", REPO, dict(gh.comments[0], _issue=49),
                                 executor=executor(), ledger=CommandLedger(), processed=processed)
        self.assertEqual(first["action"], "executed")
        self.assertEqual(second["action"], "already-processed")
        self.assertEqual(len([b for b in gh.posted if "Command result" in b]), 1)

    def test_dry_run_queue_issue_reports_would_queue_without_mutating(self):
        env = envelope(operation="queue_issue", issue=49, dry_run=True)
        gh = FakeGitHub([self._comment(env)])
        out = process_comment(gh, "tok", REPO, dict(gh.comments[0], _issue=49),
                              executor=executor(), ledger=CommandLedger(), processed={},
                              budget_guard=lambda op, i=None: {"allowed": True})
        self.assertEqual(out["action"], "executed")
        self.assertEqual(out["execution"]["state"], "completed")
        self.assertFalse(out["execution"]["result"]["mutated"])

    def test_scan_skips_prose_comments(self):
        gh = FakeGitHub([{"id": 1, "body": "just a normal review comment, no fence"}])
        results = scan(gh, "tok", REPO, 49, executor=executor(), ledger=CommandLedger(),
                       processed={})
        self.assertEqual(results[0]["action"], "none")


if __name__ == "__main__":
    unittest.main()
