"""Issue #45 activation guards: the wiring that makes monitoring observable.

These are text-level guards on purpose: they fail loudly if someone removes the
external watchdog, the status publisher cadence or the structured rejection wiring,
without needing any network access.
"""
import os
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


if __name__ == "__main__":
    unittest.main()
