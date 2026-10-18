"""Issue #45 activation guards: the wiring that makes monitoring observable.

These are text-level guards on purpose: they fail loudly if someone removes the
external watchdog, the status publisher cadence or the structured rejection wiring,
without needing any network access.
"""
import os
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as handle:
        return handle.read()


class WatchdogWorkflowTests(unittest.TestCase):
    def test_workflow_exists_with_a_schedule_and_dispatch(self):
        workflow = read(".github/workflows/worker-watchdog.yml")
        self.assertIn("schedule:", workflow)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn('cron: "*/15 * * * *"', workflow)

    def test_workflow_is_least_privilege(self):
        workflow = read(".github/workflows/worker-watchdog.yml")
        self.assertIn("permissions:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("issues: read", workflow)
        self.assertNotIn("issues: write", workflow)
        self.assertNotIn("contents: write", workflow)

    def test_workflow_runs_both_independent_checks_and_fails_loudly(self):
        workflow = read(".github/workflows/worker-watchdog.yml")
        self.assertIn("scripts/bridge_watchdog_check.py", workflow)
        self.assertIn("scripts/bridge_intake_preflight.py", workflow)
        self.assertIn("--fail-on-alert", workflow)          # a stale status turns the run red
        self.assertIn("--max-age-seconds 900", workflow)
        self.assertIn("STATUS_ISSUE_NUMBER", workflow)


class PublisherCadenceTests(unittest.TestCase):
    def test_cron_shim_publishes_without_touching_stdout(self):
        shim = read("scripts/cron/github_worker_poll.sh")
        self.assertIn("scripts/bridge_status_publish.py --live", shim)
        self.assertIn('>"$STATUS_DIR/publish.log" 2>&1', shim)
        self.assertIn("last-publish", shim)                  # bounded cadence, not every minute
        self.assertIn("|| true", shim)

    def test_cron_shim_enables_structured_rejections(self):
        shim = read("scripts/cron/github_worker_poll.sh")
        self.assertIn("STEVEN_INTAKE_REJECT_COMMENTS", shim)


class PollerWiringTests(unittest.TestCase):
    def test_poller_reports_rejections_through_the_shared_module(self):
        poller = read("scripts/issue_poller.py")
        self.assertIn("_maybe_post_rejection", poller)
        self.assertIn("gbp.bridge.intake", poller)
        self.assertIn("should_post_rejection", poller)       # deduplicated alerts

    def test_rejection_state_path_is_outside_protected_paths(self):
        poller = read("scripts/issue_poller.py")
        self.assertIn("/opt/data/ops/bridge/intake-rejections.json", poller)


class DedupeTests(unittest.TestCase):
    def test_same_body_is_reported_once_and_again_after_the_cooldown(self):
        from gbp.bridge.intake import (
            record_rejection,
            rejection_key,
            should_post_rejection,
        )

        key = rejection_key(45, "## Objective\ntext")
        self.assertTrue(should_post_rejection({}, key, 1000.0))
        state = record_rejection({}, key, 1000.0)
        self.assertFalse(should_post_rejection(state, key, 1000.0 + 60))
        self.assertTrue(should_post_rejection(state, key, 1000.0 + 21600))

    def test_a_corrected_body_is_reported_again(self):
        from gbp.bridge.intake import rejection_key

        self.assertNotEqual(rejection_key(45, "## Objective"), rejection_key(45, "## Goal"))


class IntakeCaseTests(unittest.TestCase):
    def test_the_exact_issue_43_and_45_bodies_are_diagnosed(self):
        """#43 (wrong headings) and #45 (guard wording) must both be explainable."""
        from gbp.bridge.intake import REQUIRED_HEADINGS, preflight
        import issue_worker_lib

        patterns = list(issue_worker_lib.INJECTION_PATTERNS)
        broken = "## Objective\n## Phase 1\n## Restrictions\n## Success"
        verdict = preflight(broken, patterns=patterns)
        self.assertFalse(verdict["usable"])
        self.assertEqual(sorted(verdict["missing_headings"]), sorted(REQUIRED_HEADINGS))

        guarded = "## Goal\n## Scope\n## Acceptance Criteria\n## Tests Required\n## Constraints\n" \
                  "Do not bypass protected-path guards."
        verdict = preflight(guarded, patterns=patterns)
        self.assertFalse(verdict["usable"])
        self.assertIn("weaken_guard", verdict["tripwire"])


class IntakeAlertTests(unittest.TestCase):
    """The intake watchdog job must FAIL, not merely log, on a blocking finding."""

    NOW = 1_800_000_000.0

    def _main(self, issues, events, ledger=None, extra_argv=()):
        import bridge_intake_preflight as cli

        def request(method, path, token=None, body=None):
            if "labels=hermes-ready" in path:
                return (issues, {})
            if "/events" in path:
                number = path.split("/issues/")[1].split("/")[0]
                return (events.get(number, []), {})
            raise AssertionError(path)

        argv = ["--repo", "StevenMarketing/steven-ai-os", "--stale-minutes", "5",
                "--no-record", *extra_argv]
        original = cli._ledger
        cli._ledger = lambda: (ledger or {})
        try:
            return cli.main(argv, request=request, token="tok", patterns=[])
        finally:
            cli._ledger = original

    def _issue(self, number, body, labels=("hermes-ready",)):
        return {"number": number, "state": "open", "body": body,
                "labels": [{"name": l} for l in labels]}

    GOOD = ("## Goal\nx\n## Scope\nx\n## Acceptance Criteria\nx\n"
            "## Tests Required\nx\n## Constraints\nx")

    def _label(self, stamp):
        return [{"event": "labeled", "label": {"name": "hermes-ready"}, "created_at": stamp}]

    @staticmethod
    def _ago(seconds):
        """ISO timestamp `seconds` before the real clock that main() will use."""
        import datetime

        return datetime.datetime.fromtimestamp(
            time.time() - seconds, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_invalid_issue_fails_the_run_only_with_fail_on_alert(self):
        broken = self._issue(45, "## Objective\n## Phase 1\n## Restrictions\n## Success")
        events = {"45": self._label(self._ago(3600))}
        self.assertEqual(self._main([broken], events), 0)                     # report only
        self.assertEqual(
            self._main([broken], events, extra_argv=("--fail-on-alert",)), 1)  # watchdog red

    def test_freshly_labelled_issue_does_not_fail(self):
        issues = [self._issue(50, self.GOOD)]
        self.assertEqual(
            self._main(issues, {"50": self._label(self._ago(30))},
                       extra_argv=("--fail-on-alert",)), 0)

    def test_unclaimed_after_threshold_fails_using_the_label_event_alone(self):
        """No state file, no observation: the trusted label time drives the alert."""
        issues = [self._issue(51, self.GOOD)]
        events = {"51": self._label(self._ago(3600))}                        # an hour ago
        self.assertEqual(
            self._main(issues, events, extra_argv=("--fail-on-alert",)), 1)

    def test_processed_issue_is_ignored(self):
        issues = [self._issue(52, self.GOOD)]
        events = {"52": self._label(self._ago(3600))}
        ledger = {"processed": {"52": {"outcome": "done"}}}
        self.assertEqual(
            self._main(issues, events, ledger=ledger, extra_argv=("--fail-on-alert",)), 0)

    def test_report_exposes_blocking_and_structured_reasons(self):
        import bridge_intake_preflight as cli

        issues = [self._issue(45, "## Objective")]

        def request(method, path, token=None, body=None):
            return ((issues, {}) if "labels=hermes-ready" in path else ([], {}))

        report = cli.evaluate_intake(request, "tok", "r/r", now=self.NOW, patterns=[],
                                     observations={}, ledger={})
        self.assertEqual(report["blocking"], 1)
        self.assertEqual(report["reasons"][0]["code"], "missing_heading")
        self.assertEqual(report["reasons"][0]["issue"], 45)

    def test_the_watchdog_job_uses_the_failure_path(self):
        workflow = read(".github/workflows/worker-watchdog.yml")
        self.assertIn("--fail-on-alert", workflow)
        self.assertIn("--no-record", workflow)

    def test_settled_issue_never_blocks_an_external_run(self):
        """#43 today: hermes-ready + hermes-done. Without the VPS ledger the runner must
        still see it as finished, not as 'unclaimed' or 'unusable'."""
        import bridge_intake_preflight as cli

        issue = self._issue(43, "## Objective", labels=("hermes-ready", "hermes-done"))
        events = {"43": self._label(self._ago(7200))}

        def request(method, path, token=None, body=None):
            return (( [issue], {}) if "labels=hermes-ready" in path else (events.get("43", []), {}))

        report = cli.evaluate_intake(request, "tok", "r/r", now=time.time(), patterns=[],
                                     observations={}, ledger={})
        self.assertEqual(report["blocking"], 0)
        self.assertEqual(len(report["settled"]), 1)
        self.assertEqual(report["unclaimed"], [])

    def test_settled_issue_is_accepted_by_main_with_fail_on_alert(self):
        issue = self._issue(43, "## Objective", labels=("hermes-ready", "hermes-done"))
        events = {"43": self._label(self._ago(7200))}
        self.assertEqual(self._main([issue], events, extra_argv=("--fail-on-alert",)), 0)


if __name__ == "__main__":
    unittest.main()
