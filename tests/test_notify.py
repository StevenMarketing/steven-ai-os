"""Test cho module thông báo Telegram (Phần D).

Kiểm tra: nội dung tin theo sự kiện, không lộ credential, không gửi khi polling
không có việc, mốc 80%/100% chỉ gửi một lần mỗi ngày, lỗi gửi không làm sập caller.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import budget      # noqa: E402
import notify      # noqa: E402

NOW = 1_800_000_000.0
DAY = 86_400.0


def provider(usage_daily=0.0):
    return {"ok": True, "label": "test-key", "limit_usd": 10.0, "remaining_usd": 10.0,
            "usage_usd": 0.0, "usage_daily_usd": usage_daily, "usage_weekly_usd": 0.0,
            "usage_monthly_usd": 0.0, "budgets": [], "expires_at": None,
            "is_free_tier": False, "source": "test", "error": None}


class MessageTests(unittest.TestCase):
    def test_task_done_message_carries_issue_pr_and_tests(self):
        text = notify.build_message("task_done", issue=2, pr="https://x/pull/4",
                                    tests="101 passed")
        self.assertIn("#2", text)
        self.assertIn("https://x/pull/4", text)
        self.assertIn("101 passed", text)
        self.assertIn("không tự merge", text)

    def test_task_failed_and_blocked_name_the_reason(self):
        failed = notify.build_message("task_failed", issue=7, detail="tests failed")
        blocked = notify.build_message("task_blocked", issue=7, detail="approval denied")
        self.assertIn("THẤT BẠI", failed)
        self.assertIn("tests failed", failed)
        self.assertIn("BLOCKED", blocked)
        self.assertIn("approval denied", blocked)

    def test_budget_message_shows_the_percentage(self):
        text = notify.build_message("budget", spend=2.4, cap=3.0, detail="mốc 80% đã vượt.")
        self.assertIn("80%", text)
        self.assertIn("$2.4000", text)
        self.assertIn("$3.00", text)

    def test_worker_error_message(self):
        text = notify.build_message("worker_error", detail="cron tick failed")
        self.assertIn("lỗi worker/cron", text)

    def test_no_message_shape_looks_like_a_credential(self):
        for event in ("task_done", "task_failed", "task_blocked", "worker_error"):
            text = notify.build_message(event, issue=1, pr="https://x/pull/1",
                                        tests="1 passed", detail="ok",
                                        spend=1.0, cap=3.0)
            self.assertIs(text, notify.redact_safe(text))

    def test_a_message_that_looks_like_a_credential_is_blocked(self):
        blocked = notify.redact_safe("gửi kèm sk-or-v1-deadbeef")
        self.assertNotIn("sk-or-v1-deadbeef", blocked)
        self.assertIn("credential", blocked)
        for marker in ("github_pat_", "ghp_", "Bearer ", "OPENROUTER_API_KEY"):
            self.assertNotIn(marker, notify.redact_safe(f"x {marker} y"))

    def test_dry_run_never_calls_the_runner(self):
        calls = []
        result = notify.send("xin chào", dry_run=True,
                             runner=lambda args: calls.append(args) or {"ok": True})
        self.assertEqual(calls, [])
        self.assertTrue(result["dry_run"])

    def test_send_targets_the_configured_telegram_bot_without_json(self):
        seen = []

        def runner(args):
            seen.append(list(args))
            return {"ok": True}

        notify.send("tin nhắn", runner=runner)
        self.assertEqual(seen[0][0:2], ["-t", "telegram"])
        self.assertEqual(seen[0][2], "tin nhắn")
        self.assertNotIn("--json", seen[0])     # --json sẽ in chat id

    def test_a_failing_runner_is_reported_not_raised(self):
        result = notify.send("x", runner=lambda args: {"ok": False, "returncode": 2})
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 2)

    def test_main_dry_run_prints_the_message(self):
        code = notify.main(["--event", "task_blocked", "--issue", "9",
                            "--detail", "no approval", "--dry-run"])
        self.assertEqual(code, 0)


class ThresholdTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bg-notify-")
        self.guard = budget.BudgetGuard(self.dir, now=NOW)

    def test_nothing_is_sent_when_polling_finds_no_work(self):
        result = notify.crossed_thresholds(self.guard, provider(usage_daily=0.4),
                                           state_dir=self.dir, now=NOW)
        self.assertEqual(result["pending"], [])
        self.assertEqual(notify.notified_today(self.dir, NOW)["sent"], [])

    def test_80_and_100_percent_are_each_sent_once(self):
        result = notify.crossed_thresholds(self.guard, provider(usage_daily=2.5),
                                           state_dir=self.dir, now=NOW)
        self.assertEqual([p["label"] for p in result["pending"]], ["budget-80"])
        notify.mark_notified("budget-80", self.dir, NOW)
        result = notify.crossed_thresholds(self.guard, provider(usage_daily=2.6),
                                           state_dir=self.dir, now=NOW)
        self.assertEqual(result["pending"], [])                 # không gửi lại 80%
        result = notify.crossed_thresholds(self.guard, provider(usage_daily=3.0),
                                           state_dir=self.dir, now=NOW)
        self.assertEqual([p["label"] for p in result["pending"]], ["budget-100"])
        notify.mark_notified("budget-100", self.dir, NOW)
        result = notify.crossed_thresholds(self.guard, provider(usage_daily=3.4),
                                           state_dir=self.dir, now=NOW)
        self.assertEqual(result["pending"], [])

    def test_both_thresholds_fire_at_once_when_spend_jumps(self):
        result = notify.crossed_thresholds(self.guard, provider(usage_daily=3.2),
                                           state_dir=self.dir, now=NOW)
        self.assertEqual([p["label"] for p in result["pending"]],
                         ["budget-80", "budget-100"])

    def test_the_daily_notification_state_resets_with_a_new_day(self):
        notify.mark_notified("budget-80", self.dir, NOW)
        self.assertIn("budget-80", notify.notified_today(self.dir, NOW)["sent"])
        self.assertEqual(notify.notified_today(self.dir, NOW + DAY)["sent"], [])
        result = notify.crossed_thresholds(self.guard, provider(usage_daily=2.5),
                                           state_dir=self.dir, now=NOW + DAY)
        self.assertEqual([p["label"] for p in result["pending"]], ["budget-80"])

    def test_the_threshold_check_uses_the_provider_figure(self):
        result = notify.crossed_thresholds(self.guard, provider(usage_daily=2.9),
                                           state_dir=self.dir, now=NOW)
        self.assertEqual(result["spend_usd"], 2.9)
        self.assertIn("usage_daily", result["basis"])

    def test_main_check_thresholds_sends_through_the_injected_runner(self):
        sent = []
        original = notify.send
        notify.send = lambda text, **kw: sent.append(text) or {"ok": True, "message": text}
        original_provider = budget.provider_usage
        budget.provider_usage = lambda *a, **k: provider(usage_daily=2.6)
        try:
            code = notify.main(["--event", "budget", "--check-thresholds",
                                "--state-dir", self.dir, "--json"])
        finally:
            notify.send = original
            budget.provider_usage = original_provider
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("80%", sent[0])
        self.assertIn("budget-80", notify.notified_today(self.dir)["sent"])


if __name__ == "__main__":
    unittest.main()
