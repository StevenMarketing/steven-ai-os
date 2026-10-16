"""Regression tests cho Budget Guard (Phần B): số provider xác nhận, không đếm
trùng, fail-closed, chuyển ngày, race condition và watchdog.

Các ca bắt buộc theo yêu cầu chủ sở hữu:
1. chi phí từ phiên Hermes khác đã gần chạm $3;
2. sổ cái nội bộ chưa cập nhật nhưng provider đã vượt trần;
3. provider API không khả dụng ⇒ fail-closed;
4. task mới khiến tổng vượt $3;
5. không cộng trùng provider + sổ cái;
6. chuyển ngày và reset bộ đếm;
7. race condition khi nhiều task cùng xin ngân sách;
8. watchdog phải dừng worker đúng lúc (kể cả khi provider không đọc được).
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import budget              # noqa: E402
import budget_watchdog     # noqa: E402

NOW = 1_800_000_000.0      # mốc cố định (UTC) cho test
DAY = 86_400.0


def provider(usage_daily=None, remaining=10.0, ok=True, error=None, limit=10.0):
    return {"ok": ok, "label": "test-key", "limit_usd": limit,
            "remaining_usd": remaining, "usage_usd": 0.0,
            "usage_daily_usd": usage_daily, "usage_weekly_usd": 0.0,
            "usage_monthly_usd": 0.0, "budgets": [], "expires_at": None,
            "is_free_tier": False, "source": "test", "error": error}


def reserve_in_child(state_dir, queue, provider_info, now):
    """Chạy trong tiến trình riêng để khoá flock thực sự có tác dụng."""
    import budget as child_budget
    guard = child_budget.BudgetGuard(state_dir, daily_usd=0.10, task_usd=0.10,
                                     now=now)
    allowed, reason = guard.reserve(f"task-{os.getpid()}", 0.05,
                                    provider=provider_info)
    queue.put((allowed, reason))


class ProviderAwareDailyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bg-")
        self.guard = budget.BudgetGuard(self.dir, now=NOW)

    # 1 — chi tiêu của các phiên Hermes khác
    def test_other_hermes_sessions_near_the_cap_block_new_work(self):
        self.guard.record("my-task", 0.01)          # sổ cái của ta rất nhỏ
        allowed, reason = self.guard.can_spend(0.10, "new-task",
                                               provider(usage_daily=2.95))
        self.assertFalse(allowed)
        self.assertIn("trần ngày", reason)
        self.assertIn("usage_daily", reason)        # nói rõ nguồn số

    # 2 — sổ cái chưa cập nhật, provider đã vượt trần
    def test_stale_ledger_with_provider_over_the_cap_is_refused(self):
        allowed, reason = self.guard.can_spend(0.001, "new-task",
                                               provider(usage_daily=3.05))
        self.assertFalse(allowed)
        self.assertIn("trần ngày", reason)

    # 3 — provider không khả dụng ⇒ fail-closed
    def test_an_unavailable_provider_fails_closed(self):
        allowed, reason = self.guard.can_spend(0.01, "t",
                                               provider(ok=False, error="URLError"))
        self.assertFalse(allowed)
        self.assertIn("fail-closed", reason)

    def test_fail_open_happens_only_when_explicitly_requested(self):
        allowed, _ = self.guard.can_spend(0.01, "t", provider(ok=False, error="x"),
                                          fail_open=True)
        self.assertTrue(allowed)

    # 4 — task mới làm tổng vượt trần
    def test_a_task_that_pushes_the_total_over_the_cap_is_refused(self):
        allowed, reason = self.guard.can_spend(0.05, "t", provider(usage_daily=2.98))
        self.assertFalse(allowed, reason)
        allowed, reason = self.guard.can_spend(0.01, "t", provider(usage_daily=2.98))
        self.assertTrue(allowed, reason)

    # 5 — không đếm trùng
    def test_provider_and_ledger_are_not_double_counted(self):
        self.guard.record("my-task", 0.0086)
        spend = self.guard.spend_today(provider(usage_daily=1.44))
        self.assertEqual(spend["spend"], 1.44)              # KHÔNG phải 1.4486
        self.assertEqual(spend["ledger"], 0.0086)
        self.assertIn("max(", spend["basis"])

    def test_the_ledger_is_the_only_source_when_the_provider_is_down(self):
        self.guard.record("my-task", 0.25)
        spend = self.guard.spend_today(provider(ok=False, error="URLError"))
        self.assertEqual(spend["spend"], 0.25)
        self.assertFalse(spend["ok"])                       # để người gọi fail-closed

    def test_the_provider_daily_figure_wins_when_it_is_larger(self):
        self.guard.record("my-task", 0.10)
        spend = self.guard.spend_today(provider(usage_daily=2.0))
        self.assertEqual(spend["spend"], 2.0)
        self.assertEqual(spend["provider_daily"], 2.0)

    # 6 — chuyển ngày và reset bộ đếm
    def test_the_counter_resets_when_the_day_rolls_over(self):
        allowed, _ = self.guard.can_spend(0.05, "t", provider(usage_daily=2.99))
        self.assertFalse(allowed)
        tomorrow = budget.BudgetGuard(self.dir, now=NOW + DAY)
        allowed, reason = tomorrow.can_spend(0.05, "t", provider(usage_daily=0.02))
        self.assertTrue(allowed, reason)
        self.assertEqual(tomorrow.spent_today(), 0.0)       # sổ cái reset theo ngày UTC
        self.assertEqual(tomorrow.spend_today(provider(usage_daily=0.02))["spend"], 0.02)

    def test_the_day_key_is_utc(self):
        self.assertEqual(self.guard._today(),
                         budget.time.strftime("%Y-%m-%d", budget.time.gmtime(NOW)))

    # trạng thái phải nói rõ nguồn số
    def test_status_exposes_the_spend_basis(self):
        status = self.guard.status(provider(usage_daily=1.1))
        self.assertEqual(status["spent_today_usd"], 1.1)
        self.assertEqual(status["spend_basis"], "max(provider usage_daily, local ledger)")
        self.assertEqual(status["ledger_today_usd"], 0.0)
        self.assertEqual(status["remaining_today_usd"], 1.9)

    def test_the_status_reports_an_absent_usage_daily(self):
        status = self.guard.status(provider(usage_daily=None))
        # không có usage_daily ⇒ dùng mốc nối tiếp theo ngày (baseline = 0 hôm nay)
        self.assertEqual(status["spent_today_usd"], 0.0)
        self.assertEqual(status["basis"] if "basis" in status else status["spend_basis"],
                         "max(provider usage_daily, local ledger)")


class ReservationTests(unittest.TestCase):
    """Chống TOCTOU: kiểm tra và giữ chỗ nằm trong cùng một khoá."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bg-race-")

    def guard(self, now=NOW):
        return budget.BudgetGuard(self.dir, daily_usd=0.10, task_usd=0.10, now=now)

    def test_reserve_holds_budget_and_blocks_a_second_holder(self):
        guard = self.guard()
        allowed, reason = guard.reserve("a", 0.05, provider=provider(usage_daily=0.0))
        self.assertTrue(allowed, reason)
        self.assertEqual(guard.spend_today(provider(usage_daily=0.0))["spend"], 0.05)
        # chạm đúng trần thì vẫn được phép (điều kiện là ">", không phải ">=")
        allowed, reason = guard.reserve("b", 0.05, provider=provider(usage_daily=0.0))
        self.assertTrue(allowed, reason)
        # nhưng không thể vượt trần
        allowed, reason = guard.reserve("c", 0.01, provider=provider(usage_daily=0.0))
        self.assertFalse(allowed)
        self.assertIn("trần ngày", reason)

    def test_a_reservation_does_not_consume_the_retry_budget(self):
        guard = self.guard()
        guard.reserve("a", 0.01, provider=provider(usage_daily=0.0))
        guard.reserve("a", 0.01, provider=provider(usage_daily=0.0))
        guard.reserve("a", 0.01, provider=provider(usage_daily=0.0))
        self.assertEqual(guard.attempts("a"), 0)

    def test_release_frees_the_hold(self):
        guard = self.guard()
        guard.reserve("a", 0.05, provider=provider(usage_daily=0.0))
        self.assertEqual(guard.release("a"), 0.05)
        self.assertEqual(guard.spend_today(provider(usage_daily=0.0))["spend"], 0.0)
        allowed, reason = guard.reserve("b", 0.05, provider=provider(usage_daily=0.0))
        self.assertTrue(allowed, reason)

    def test_concurrent_reservations_cannot_exceed_the_cap(self):
        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        info = provider(usage_daily=0.0)
        procs = [ctx.Process(target=reserve_in_child,
                             args=(self.dir, queue, info, NOW)) for _ in range(6)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(30)
        results = [queue.get(timeout=5) for _ in procs]
        winners = [r for r in results if r[0]]
        self.assertEqual(len(winners), 2, results)      # 0.10 / 0.05 = đúng 2 chỗ
        guard = self.guard()
        spend = guard.spend_today(provider(usage_daily=0.0))["spend"]
        self.assertLessEqual(spend, 0.10 + 1e-9)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bg-wd-")

    def test_the_watchdog_detects_the_cap_from_the_ledger_when_the_provider_is_down(self):
        guard = budget.BudgetGuard(self.dir)            # giờ thật
        guard.record("x", 3.01)
        state = budget_watchdog.evaluate(guard, provider(ok=False, error="URLError"))
        self.assertTrue(state["over_cap"])
        self.assertTrue(state["unreadable_provider"])
        self.assertEqual(state["local_ledger_usd"], 3.01)

    def test_the_watchdog_sees_other_sessions_through_the_provider_figure(self):
        guard = budget.BudgetGuard(self.dir)
        state = budget_watchdog.evaluate(guard, provider(usage_daily=2.99))
        self.assertFalse(state["over_cap"])
        self.assertEqual(state["remaining_usd"], 0.01)
        state = budget_watchdog.evaluate(guard, provider(usage_daily=3.5))
        self.assertTrue(state["over_cap"])

    def test_main_returns_3_when_over_cap_even_with_an_unreadable_provider(self):
        guard = budget.BudgetGuard(self.dir)
        guard.record("x", 3.2)
        original = budget.provider_usage
        budget.provider_usage = lambda *a, **k: provider(ok=False, error="URLError")
        try:
            code = budget_watchdog.main(["--state-dir", self.dir, "--json"])
        finally:
            budget.provider_usage = original
        self.assertEqual(code, 3)      # trước đây nhánh này trả 1 ⇒ worker không bị pause

    def test_main_returns_1_when_only_the_provider_is_unreadable(self):
        original = budget.provider_usage
        budget.provider_usage = lambda *a, **k: provider(ok=False, error="URLError")
        try:
            code = budget_watchdog.main(["--state-dir", self.dir, "--json"])
        finally:
            budget.provider_usage = original
        self.assertEqual(code, 1)

    def test_main_returns_0_within_budget(self):
        original = budget.provider_usage
        budget.provider_usage = lambda *a, **k: provider(usage_daily=0.5)
        try:
            code = budget_watchdog.main(["--state-dir", self.dir, "--json"])
        finally:
            budget.provider_usage = original
        self.assertEqual(code, 0)


class MultiKeyTests(unittest.TestCase):
    """Khoá riêng cho worker: cộng số để giữ trần hệ thống, không lộ giá trị khoá."""

    def setUp(self):
        self.previous_env = budget.read_env_value
        self.previous_http = budget.http_get_json
        budget.read_env_value = lambda name, *a, **k: {
            "WORKER_OPENROUTER_API_KEY": "worker-secret-value",
            "OPENROUTER_API_KEY": "chat-secret-value"}.get(name)

        def transport(url, token):
            if token == "worker-secret-value":
                return {"data": {"limit": 3, "usage": 0.4, "usage_daily": 0.25,
                                 "usage_weekly": 0.4, "limit_remaining": 2.6,
                                 "budgets": [{"scope": "key_limit", "limit_usd": 3,
                                              "spend_usd": 0.4, "remaining_usd": 2.6,
                                              "reset_interval": "daily"}]}}
            return {"data": {"limit": 10, "usage": 5.0, "usage_daily": 2.0,
                             "usage_weekly": 5.0, "budgets": []}}

        budget.http_get_json = transport

        def restore():
            budget.read_env_value = self.previous_env
            budget.http_get_json = self.previous_http
        self.addCleanup(restore)

    def test_both_keys_are_read_and_their_daily_usage_is_summed(self):
        info = budget.provider_usage()
        self.assertTrue(info["ok"])
        self.assertEqual(info["keys_read"], ["worker", "chat"])
        self.assertAlmostEqual(info["usage_daily_usd"], 2.25, places=6)
        # worker 3 − 0.4 = 2.6 · chat 10 − 5.0 = 5.0 ⇒ tổng 7.6
        self.assertAlmostEqual(info["remaining_usd"], 7.6, places=6)

    def test_no_key_value_appears_anywhere_in_the_result(self):
        blob = json.dumps(budget.provider_usage())
        for leaked in ("worker-secret-value", "chat-secret-value"):
            self.assertNotIn(leaked, blob)

    def test_the_worker_keys_own_hard_limit_is_visible_separately(self):
        info = budget.provider_usage()
        self.assertEqual([b["role"] for b in info["budgets"]], ["worker"])
        self.assertEqual(info["budgets"][0]["reset_interval"], "daily")
        self.assertEqual(info["budgets"][0]["limit_usd"], 3)
        roles = {p["role"]: p for p in info["per_key"]}
        self.assertEqual(roles["worker"]["limit_usd"], 3)
        self.assertEqual(roles["worker"]["usage_daily_usd"], 0.25)
        self.assertEqual(roles["chat"]["limit_usd"], 10)

    def test_a_missing_worker_key_falls_back_to_the_single_chat_key(self):
        budget.read_env_value = lambda name, *a, **k: (
            {"OPENROUTER_API_KEY": "chat-secret-value"}.get(name))
        info = budget.provider_usage()
        self.assertEqual(info["keys_read"], ["chat"])
        self.assertAlmostEqual(info["usage_daily_usd"], 2.0, places=6)

    def test_the_worker_key_usage_helper_is_explicit_when_not_configured(self):
        budget.read_env_value = lambda name, *a, **k: None
        usage = budget.worker_key_usage()
        self.assertFalse(usage["ok"])
        self.assertFalse(usage["configured"])
        self.assertIn("chưa cấu hình", usage["error"])

    def test_no_key_configured_at_all_is_reported_not_guessed(self):
        budget.read_env_value = lambda name, *a, **k: None
        info = budget.provider_usage()
        self.assertFalse(info["ok"])
        self.assertIn("WORKER_OPENROUTER_API_KEY", info["error"])
        self.assertIn("OPENROUTER_API_KEY", info["error"])


    def test_the_worker_key_can_be_read_from_the_worker_profiles_env_file(self):
        """Khoá worker nằm trong .env của profile worker ⇒ vẫn cộng được vào trần hệ thống."""
        os.environ["STEVEN_WORKER_ENV_FILE"] = "/opt/data/profiles/worker-agent/.env"
        self.addCleanup(os.environ.pop, "STEVEN_WORKER_ENV_FILE", None)
        budget.read_env_value = lambda name, path=None, *a, **k: (
            {"OPENROUTER_API_KEY": "chat-secret-value"}.get(name) if path is None
            else ("worker-secret-value" if name == "OPENROUTER_API_KEY" else None))
        info = budget.provider_usage()
        self.assertEqual(info["keys_read"], ["worker", "chat"])
        self.assertAlmostEqual(info["usage_daily_usd"], 2.25, places=6)
        blob = json.dumps(info)
        self.assertNotIn("worker-secret-value", blob)
        self.assertNotIn("chat-secret-value", blob)

    def test_a_worker_profile_without_a_key_falls_back_to_the_chat_key_only(self):
        os.environ["STEVEN_WORKER_ENV_FILE"] = "/opt/data/profiles/worker-agent/.env"
        self.addCleanup(os.environ.pop, "STEVEN_WORKER_ENV_FILE", None)
        budget.read_env_value = lambda name, path=None, *a, **k: (
            "chat-secret-value" if name == "OPENROUTER_API_KEY" and path is None else None)
        info = budget.provider_usage()
        self.assertEqual(info["keys_read"], ["chat"])
        self.assertAlmostEqual(info["usage_daily_usd"], 2.0, places=6)

    def test_worker_key_usage_names_the_worker_profile_env_file(self):
        budget.read_env_value = lambda name, path=None, *a, **k: None
        usage = budget.worker_key_usage()
        self.assertFalse(usage["configured"])
        self.assertIn("worker-agent", usage["worker_env_file"])
        self.assertIn("OPENROUTER_API_KEY", usage["error"])

    def test_the_whole_report_carries_no_key_values(self):
        blob = json.dumps({"usage": budget.provider_usage(),
                           "worker": budget.worker_key_usage()})
        for leaked in ("worker-secret-value", "chat-secret-value", "sk-or-v1"):
            self.assertNotIn(leaked, blob)


if __name__ == "__main__":
    unittest.main()
