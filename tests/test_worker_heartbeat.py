"""Offline tests for the worker heartbeat / status snapshot diagnostics.

No network, no clock reads, no LLM: every case passes an explicit ``now``.
"""

import json
import os
import tempfile
import unittest

from gbp.ops import heartbeat as hb


class HeartbeatStateTests(unittest.TestCase):
    def test_healthy_idle_poll(self):
        self.assertEqual(hb.IDLE, hb.heartbeat_state(now=1000, last_poll=990))

    def test_stale_poll_is_stalled(self):
        self.assertEqual(hb.STALLED,
                         hb.heartbeat_state(now=1000, last_poll=50,
                                            stall_seconds=900))

    def test_running_within_window(self):
        self.assertEqual(hb.RUNNING,
                         hb.heartbeat_state(now=1000, last_poll=990,
                                            running_since=980))

    def test_running_beyond_window_is_stalled(self):
        self.assertEqual(hb.STALLED,
                         hb.heartbeat_state(now=2000, last_poll=1990,
                                            running_since=100,
                                            stall_seconds=900))

    def test_missing_poll_is_stalled(self):
        self.assertEqual(hb.STALLED, hb.heartbeat_state(now=1000, last_poll=None))

    def test_blocked_is_degraded(self):
        self.assertEqual(hb.DEGRADED,
                         hb.heartbeat_state(now=1000, last_poll=990, blocked=True))

    def test_age_none_when_never_polled(self):
        self.assertIsNone(hb.heartbeat_age(1000, None))
        self.assertEqual(90.0, hb.heartbeat_age(1000, 910))


class ClassificationTests(unittest.TestCase):
    def test_budget(self):
        self.assertEqual(hb.BUDGET, hb.classify_problem("daily budget cap reached"))

    def test_provider(self):
        self.assertEqual(hb.PROVIDER, hb.classify_problem("provider returned 401"))

    def test_policy(self):
        self.assertEqual(hb.POLICY, hb.classify_problem("approval denied by gate"))

    def test_restricted_file(self):
        self.assertEqual(hb.RESTRICTED_FILE,
                         hb.classify_problem("protected path in diff"))

    def test_scheduling(self):
        self.assertEqual(hb.SCHEDULING, hb.classify_problem("cron tick missed"))

    def test_transient(self):
        self.assertEqual(hb.TRANSIENT, hb.classify_problem("connection reset"))

    def test_unknown_is_not_retryable(self):
        self.assertEqual(hb.UNKNOWN, hb.classify_problem("weird gibberish"))
        self.assertFalse(hb.is_retryable(hb.UNKNOWN))

    def test_provider_is_not_retried(self):
        self.assertFalse(hb.is_retryable(hb.PROVIDER))


class BoundedRetryTests(unittest.TestCase):
    def test_no_retry_for_non_transient(self):
        decision = hb.retry_decision(hb.POLICY, 0)
        self.assertFalse(decision["retry"])

    def test_transient_retries_up_to_cap(self):
        first = hb.retry_decision(hb.TRANSIENT, 0)
        second = hb.retry_decision(hb.TRANSIENT, 1)
        capped = hb.retry_decision(hb.TRANSIENT, hb.MAX_TRANSIENT_RETRIES)
        self.assertTrue(first["retry"])
        self.assertEqual(1, first["attempt"])
        self.assertTrue(second["retry"])
        self.assertFalse(capped["retry"])
        self.assertEqual("retry-cap-reached", capped["reason"])

    def test_cap_is_bounded(self):
        self.assertLessEqual(hb.MAX_TRANSIENT_RETRIES, 2)


class BudgetStatusTests(unittest.TestCase):
    def test_ok(self):
        view = hb.budget_status(0.02, 0.15)
        self.assertEqual("ok", view["state"])

    def test_warning_threshold(self):
        self.assertEqual("warning", hb.budget_status(0.13, 0.15)["state"])

    def test_exhausted(self):
        self.assertEqual("exhausted", hb.budget_status(0.15, 0.15)["state"])

    def test_reset_rolls_the_day_over(self):
        view = hb.budget_status(0.15, 0.15, reset=True)
        self.assertEqual("ok", view["state"])
        self.assertEqual(0.0, view["spend_usd"])
        self.assertEqual(0.15, view["previous_spend_usd"])
        self.assertEqual(0.15, view["remaining_usd"])


class RedactionTests(unittest.TestCase):
    def test_masks_known_shapes(self):
        text = "token sk-abcdefghijklmnopqrstuvwxyz012345 leaked"
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", hb.redact(text))

    def test_masks_key_value(self):
        self.assertNotIn("hunter2", hb.redact("password=hunter2"))

    def test_walks_structures(self):
        cleaned = hb.redact({"a": ["x", "api_key=abc123"]})
        self.assertNotIn("abc123", json.dumps(cleaned))

    def test_plain_text_untouched(self):
        self.assertEqual("hello world", hb.redact("hello world"))


class DedupTests(unittest.TestCase):
    def test_stable_identity(self):
        a = hb.event_identity("task_done", issue=21, detail="ok")
        b = hb.event_identity("task_done", issue=21, detail="OK")
        self.assertEqual(a, b)

    def test_should_emit_once(self):
        deduper = hb.ProgressDeduper()
        ident = hb.event_identity("start", issue=21)
        self.assertTrue(deduper.should_emit(ident))
        self.assertFalse(deduper.should_emit(ident))

    def test_missing_state_file_loads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            deduper = hb.ProgressDeduper.load(os.path.join(tmp, "nope.json"))
            self.assertEqual([], deduper.seen())

    def test_save_and_reload_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dedupe.json")
            deduper = hb.ProgressDeduper()
            deduper.should_emit(hb.event_identity("done", issue=21))
            self.assertTrue(deduper.save(path))
            reloaded = hb.ProgressDeduper.load(path)
            self.assertFalse(reloaded.should_emit(
                hb.event_identity("done", issue=21)))

    def test_save_failure_is_silent(self):
        deduper = hb.ProgressDeduper()
        self.assertFalse(deduper.save("/proc/does/not/exist/dedupe.json"))


class SnapshotTests(unittest.TestCase):
    def test_snapshot_shape_and_redaction(self):
        snapshot = hb.status_snapshot(
            generated_at="2026-10-17T05:00:00Z", heartbeat=hb.IDLE,
            last_poll_at="2026-10-17T04:59:00Z",
            issues=[{"number": 21, "state": "closed", "outcome": "done",
                     "pr": "https://example/pr/1"}],
            budget=hb.budget_status(0.01, 0.15), blocker=None,
            next_step=hb.safe_next_step(hb.IDLE))
        self.assertEqual("idle", snapshot["heartbeat"])
        self.assertEqual(21, snapshot["issues"][0]["number"])
        self.assertIsNone(snapshot["blocker"])
        self.assertIn("none", snapshot["next_step"])

    def test_snapshot_redacts_blocker(self):
        snapshot = hb.status_snapshot(
            generated_at="2026-10-17T05:00:00Z", heartbeat=hb.DEGRADED,
            blocker="failed with token=supersecretvalue123")
        self.assertNotIn("supersecretvalue123", json.dumps(snapshot))

    def test_next_step_for_degraded_transient(self):
        step = hb.safe_next_step(hb.DEGRADED, hb.TRANSIENT)
        self.assertIn("bounded retry", step)

    def test_next_step_for_degraded_non_transient(self):
        step = hb.safe_next_step(hb.DEGRADED, hb.POLICY)
        self.assertIn("human review", step)


if __name__ == "__main__":
    unittest.main()
