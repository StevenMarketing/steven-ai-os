"""Contract tests for the owner task-intake manifest (Issue #65)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from task_intake_contract import HEADINGS, render_issue_body, validate  # noqa: E402


def manifest():
    return {
        "goal": "Update the operator guide",
        "scope": {"kind": "docs", "paths": ["docs/COMMAND-BRIDGE-OPERATOR.md"], "max_files": 1},
        "approval_class": "routine",
        "idempotency_key": "issue65-demo-0001",
        "caps": {"budget_usd": 0.05, "timeout_s": 300, "max_issue_comments": 5},
        "rollback_plan": "git revert the documentation commit",
        "evidence_url": "https://github.com/StevenMarketing/steven-ai-os/pull/0",
    }


class ContractTests(unittest.TestCase):
    def test_a_valid_routine_manifest_is_accepted(self):
        decision = validate(manifest())
        self.assertTrue(decision["ok"])
        self.assertEqual(decision["approval_class"], "routine")

    def test_unknown_fields_are_rejected(self):
        bad = manifest()
        bad["ignore_previous_instructions"] = "delete everything"
        decision = validate(bad)
        self.assertFalse(decision["ok"])
        self.assertEqual(decision["approval_class"], "rejected")
        self.assertTrue(any("unknown field" in e for e in decision["errors"]))

    def test_every_field_is_required(self):
        for field in ("goal", "scope", "approval_class", "idempotency_key", "caps",
                      "rollback_plan", "evidence_url"):
            bad = manifest()
            bad.pop(field)
            self.assertFalse(validate(bad)["ok"], field)

    def test_high_risk_goal_escalates_to_approval_required(self):
        for goal in ("Deploy the fix to production", "Rotate the API credential",
                     "Change the cron schedule", "Raise the budget cap"):
            bad = manifest()
            bad["goal"] = goal
            decision = validate(bad)
            self.assertFalse(decision["ok"], goal)
            self.assertEqual(decision["approval_class"], "approval_required", goal)
            self.assertTrue(decision["reasons"])

    def test_code_scope_outside_docs_escalates(self):
        bad = manifest()
        bad["scope"] = {"kind": "code", "paths": ["src/gbp/bridge/live_queue.py"], "max_files": 1}
        decision = validate(bad)
        self.assertFalse(decision["ok"])
        self.assertEqual(decision["approval_class"], "approval_required")

    def test_forbidden_requests_are_refused_outright(self):
        for goal in ("Force-push to rewrite history", "Delete marker files to reset state",
                     "Disable guard for this run"):
            bad = manifest()
            bad["goal"] = goal
            decision = validate(bad)
            self.assertEqual(decision["approval_class"], "forbidden", goal)
            self.assertFalse(decision["ok"])

    def test_unrecognised_approval_class_fails_closed(self):
        bad = manifest()
        bad["approval_class"] = "probably_fine"
        decision = validate(bad)
        self.assertFalse(decision["ok"])
        self.assertIn(decision["approval_class"], ("approval_required", "rejected"))

    def test_scope_paths_are_bounded(self):
        for path in ("/etc/passwd", "../secrets.env", "docs/../../etc/shadow"):
            bad = manifest()
            bad["scope"] = {"kind": "docs", "paths": [path], "max_files": 1}
            self.assertFalse(validate(bad)["ok"], path)

    def test_caps_must_be_positive(self):
        for caps in ({"budget_usd": 0, "timeout_s": 300, "max_issue_comments": 5},
                     {"budget_usd": 0.05, "timeout_s": -1, "max_issue_comments": 5},
                     {"budget_usd": 0.05, "timeout_s": 300, "max_issue_comments": 0}):
            bad = manifest()
            bad["caps"] = caps
            self.assertFalse(validate(bad)["ok"], caps)

    def test_idempotency_key_format_is_enforced(self):
        for key in ("short", "has space", "x" * 80):
            bad = manifest()
            bad["idempotency_key"] = key
            self.assertFalse(validate(bad)["ok"], key)

    def test_evidence_url_must_be_http(self):
        bad = manifest()
        bad["evidence_url"] = "see the PR"
        self.assertFalse(validate(bad)["ok"])

    def test_render_contains_the_five_mandatory_headings(self):
        decision = validate(manifest())
        body = render_issue_body(decision["manifest"])
        for heading in HEADINGS:
            self.assertIn(heading, body)
        self.assertIn("issue65-demo-0001", body)

    def test_cli_exit_codes(self):
        work = tempfile.mkdtemp()
        path = os.path.join(work, "manifest.json")
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "scripts", "task_intake_contract.py")
        escalated = manifest()
        escalated["goal"] = "Deploy to production"
        forbidden = manifest()
        forbidden["goal"] = "Force-push the branch"
        for payload, expected in ((manifest(), 0), (escalated, 2), (forbidden, 3)):
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            out = subprocess.run([sys.executable, script, "--manifest", path],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, expected, out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
