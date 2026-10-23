"""Regression tests for the offline intake harness (Issue #65).

The harness is a SIMULATION (live=False). These tests assert the decisions it routes through the
real functions, and that it cannot reach the network or a live token.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "src"))

import task_intake_harness as harness  # noqa: E402
from gbp.bridge.external_watchdog import evaluate_external  # noqa: E402


class HarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {r["scenario"]: r for r in harness.run_all()}

    def test_every_scenario_is_labelled_as_a_simulation(self):
        self.assertTrue(self.results)
        for name, result in self.results.items():
            self.assertIs(result["live"], False, name)
            self.assertIn("outcome", result)

    def test_the_required_scenarios_are_covered(self):
        for name in ("routine", "approval_required", "forbidden", "malformed_input",
                     "prompt_injection_goal", "prompt_injection_field", "replay",
                     "stale_heartbeat", "stopped_cron", "worker_crash",
                     "partial_github_outage", "denied_permissions"):
            self.assertIn(name, self.results, name)

    def test_routine_manifest_is_accepted_and_its_body_is_usable(self):
        routine = self.results["routine"]
        self.assertEqual(routine["outcome"]["contract_class"], "routine")
        self.assertTrue(routine["outcome"]["body_usable"])

    def test_approval_required_is_escalated_and_never_queued(self):
        escalated = self.results["approval_required"]
        self.assertEqual(escalated["outcome"]["contract_class"], "approval_required")
        self.assertIn("not_queued", escalated["outcome"]["queue_decision"])

    def test_forbidden_is_refused(self):
        refused = self.results["forbidden"]
        self.assertEqual(refused["outcome"]["contract_class"], "forbidden")
        self.assertIn("not_queued", refused["outcome"]["queue_decision"])

    def test_malformed_body_fails_the_body_preflight(self):
        malformed = self.results["malformed_input"]
        self.assertIs(malformed["outcome"]["body_usable"], False)
        steps = {s["step"]: s for s in malformed["steps"]}
        self.assertTrue(steps["body_preflight"]["missing_headings"])

    def test_prompt_injection_is_stopped_by_the_contract(self):
        self.assertEqual(self.results["prompt_injection_goal"]["outcome"]["contract_class"],
                         "forbidden")
        self.assertEqual(self.results["prompt_injection_field"]["outcome"]["contract_class"],
                         "rejected")

    def test_replay_is_refused_and_the_first_submission_passes(self):
        replay = self.results["replay"]
        steps = {s["step"]: s for s in replay["steps"]}
        self.assertTrue(steps["first_envelope"]["accepted"])
        self.assertFalse(steps["second_envelope"]["accepted"])
        self.assertEqual(steps["second_envelope"]["replay"], "duplicate")

    def test_stale_heartbeat_and_stopped_cron_alert(self):
        import time
        stale = {"generated_at": "2026-01-01T00:00:00Z", "worker": {},
                 "cron": {"state": "scheduled"}, "budget": {}}
        self.assertIn("status_stale",
                      [a["code"] for a in evaluate_external(stale, now=time.time())["alerts"]])
        missing_cron = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "worker": {}, "budget": {}}
        alerts = [a["code"] for a in evaluate_external(missing_cron, now=time.time())["alerts"]]
        self.assertTrue(alerts, "a missing cron block must alert")
        self.assertTrue(self.results["stopped_cron"]["outcome"]["cron_stop_flagged"])

    def test_worker_crash_outcome_is_unknown_so_it_is_never_retried(self):
        crash = self.results["worker_crash"]
        steps = {s["step"]: s for s in crash["steps"]}
        self.assertEqual(steps["settlement"]["state"], "unknown")
        self.assertTrue(crash["outcome"]["no_auto_retry"])

    def test_outage_and_denied_permissions_do_not_pass_the_issue_preflight(self):
        for name in ("partial_github_outage", "denied_permissions"):
            outcome = self.results[name]["outcome"]
            self.assertIs(outcome["issue_preflight_ok"], False, name)
            self.assertIn("not_queued", outcome["queue_decision"], name)

    def test_the_harness_cannot_reach_the_network_or_a_live_token(self):
        source = open(os.path.join(ROOT, "scripts", "task_intake_harness.py"), encoding="utf-8").read()
        for forbidden in ("read_token(", "import urllib", "import requests", "import socket",
                          "http.client", "os.environ.get(\"GITHUB", "OPENROUTER_API_KEY",
                          "issue_poller.read_token"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
