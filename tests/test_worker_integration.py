"""Integration test cho toàn bộ đường chạy:

    GitHub Issue → Budget Guard (provider) → worker gate → claim → Draft PR → Telegram

Khác với unit test: ở đây đi qua **nhiều lớp thật** cùng lúc — `worker_gate.Gate`
(ngân sách provider-aware + gate issue), `issue_worker_lib.StateStore` (claim nguyên tử,
idempotency, sổ cái), `budget_watchdog` (pause worker) và `notify` (Telegram, được tiêm
để test không gửi tin thật). Không gọi mạng: số provider đọc từ file do
`STEVEN_BUDGET_PROVIDER_FILE` chỉ định.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import budget                 # noqa: E402
import budget_watchdog        # noqa: E402
import issue_worker_lib as lib  # noqa: E402
import notify                 # noqa: E402
import worker_gate            # noqa: E402


def provider(daily=0.0, remaining=10.0, ok=True, error=None, limit=10.0):
    return {"ok": ok, "label": None, "limit_usd": limit, "remaining_usd": remaining,
            "usage_usd": 0.0, "usage_daily_usd": daily, "usage_weekly_usd": 0.0,
            "usage_monthly_usd": 0.0, "budgets": [], "expires_at": None,
            "is_free_tier": False, "source": "test", "error": error}


class IntegrationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wg-")
        self.state_path = os.path.join(self.tmp, "worker-state.json")
        self.budget_dir = os.path.join(self.tmp, "budget")
        os.makedirs(self.budget_dir, exist_ok=True)
        self.sent = []
        self._real_send = notify.send
        notify.send = self._capture
        self.addCleanup(setattr, notify, "send", self._real_send)

    def _capture(self, text, **kwargs):
        self.sent.append(text)
        return {"ok": True, "message": text}

    def gate(self):
        return worker_gate.Gate(self.state_path, self.budget_dir)

    def messages(self):
        return list(self.sent)


class FullPathTests(IntegrationBase):
    def test_issue_to_budget_to_claim_to_pr_to_telegram(self):
        gate = self.gate()
        ok = {"issue": "42", "estimate": 0.05}

        # 1. Gate: cả hai lớp phải cho phép
        verdict = gate.can_start("42", 0.05, provider=provider(daily=0.2))
        self.assertTrue(verdict["allowed"], verdict)
        self.assertTrue(verdict["budget_layer"]["allowed"])
        self.assertTrue(verdict["issue_layer"]["allowed"])
        self.assertIn("max(provider", verdict["budget_layer"]["basis"])

        # 2. Claim: giữ chỗ ngân sách TRƯỚC, rồi claim nguyên tử
        claimed = gate.claim("42", 0.05, provider=provider(daily=0.2))
        self.assertTrue(claimed["claimed"], claimed)
        self.assertEqual(claimed["hold_usd"], 0.05)
        self.assertTrue(claimed["run_id"])
        held = budget.BudgetGuard(self.budget_dir).spend_today(provider(daily=0.0))
        self.assertEqual(held["spend"], 0.05)                    # chỗ đã giữ được tính

        # 3. Worker làm việc: ở đây chỉ mô phỏng payload draft PR mà worker mở
        pr_body = {"title": "docs: add a Quickstart section to README",
                   "head": "hermes/issue-42-docs", "base": "main", "draft": True,
                   "body": "Closes #42"}
        self.assertIn("Closes #42", pr_body["body"])

        # 4. Finish: nhả chỗ, ghi chi phí thật, đóng issue, thông báo
        done = gate.finish("42", "done", 0.0086, pr="https://example.invalid/pull/9",
                           tests="101 passed", notify=True)
        self.assertEqual(done["hold_released_usd"], 0.05)
        self.assertEqual(done["attempts"], 1)
        self.assertEqual(done["retry_cap"], 3)
        self.assertEqual(done["usage_today"]["issues"], 1)
        self.assertTrue(done["notified"]["ok"])
        self.assertEqual(len(self.messages()), 1)
        msg = self.messages()[0]
        self.assertIn("#42", msg)
        self.assertIn("https://example.invalid/pull/9", msg)
        self.assertIn("101 passed", msg)
        self.assertIn("không tự merge", msg)

        # 5. Không đếm trùng: chỗ giữ đã nhả, chỉ còn chi phí thật
        after = budget.BudgetGuard(self.budget_dir).spend_today(provider(daily=0.0))
        self.assertAlmostEqual(after["spend"], 0.0086, places=6)

        # 6. Issue đã xử lý: không chạy lại
        again = gate.can_start("42", 0.05, provider=provider(daily=0.0))
        self.assertFalse(again["allowed"])
        self.assertIn("already processed", again["reason"])
        self.assertTrue(again["budget_layer"]["allowed"])        # ngân sách vẫn đủ

    def test_other_hermes_sessions_near_the_cap_block_the_worker(self):
        verdict = self.gate().can_start("43", 0.05, provider=provider(daily=2.99))
        self.assertFalse(verdict["allowed"])
        self.assertIn("trần ngày", verdict["reason"])
        self.assertFalse(self.gate().claim("43", 0.05, provider=provider(daily=2.99))["claimed"])

    def test_a_provider_failure_is_fail_closed_at_the_gate(self):
        verdict = self.gate().can_start("44", 0.05,
                                        provider=provider(ok=False, error="URLError"))
        self.assertFalse(verdict["allowed"])
        self.assertIn("fail-closed", verdict["reason"])

    def test_a_second_worker_is_stopped_before_it_takes_any_budget(self):
        gate = self.gate()
        first = gate.claim("45", 0.05, provider=provider(daily=0.0))
        self.assertTrue(first["claimed"], first)
        spend_after_first = budget.BudgetGuard(self.budget_dir).spend_today(
            provider(daily=0.0))["spend"]
        second = gate.claim("45", 0.05, provider=provider(daily=0.0))
        self.assertFalse(second["claimed"])
        self.assertIn("claimed by run", second["reason"])
        # bị chặn ở cửa can-start ⇒ KHÔNG giữ thêm chỗ nào (không có gì để nhả)
        self.assertNotIn("hold_released_usd", second)
        spend_after_second = budget.BudgetGuard(self.budget_dir).spend_today(
            provider(daily=0.0))["spend"]
        self.assertAlmostEqual(spend_after_first, spend_after_second, places=6)

    def test_a_failed_claim_gives_the_budget_hold_back(self):
        """Nhánh rollback: đã giữ chỗ rồi claim hỏng ⇒ phải nhả chỗ ngay."""
        gate = self.gate()
        before = budget.BudgetGuard(self.budget_dir).spend_today(
            provider(daily=0.0))["spend"]
        real_claim = lib.StateStore.claim

        def refuse(self, *args, **kwargs):
            raise lib.ClaimError("issue 45 is claimed by run somebody-else")

        lib.StateStore.claim = refuse
        try:
            result = gate.claim("45", 0.05, provider=provider(daily=0.0))
        finally:
            lib.StateStore.claim = real_claim
        self.assertFalse(result["claimed"])
        self.assertIn("ClaimError", result["reason"])
        self.assertEqual(result["hold_released_usd"], 0.05)
        after = budget.BudgetGuard(self.budget_dir).spend_today(
            provider(daily=0.0))["spend"]
        self.assertAlmostEqual(before, after, places=6)      # không rò ngân sách

    def test_the_retry_cap_stops_a_looping_worker(self):
        gate = self.gate()
        for attempt in range(1, 4):
            gate.claim("46", 0.05, provider=provider(daily=0.0))
            gate.finish("46", "failed", 0.01, detail="tests failed", notify=False)
            self.assertEqual(gate.attempts("46")["attempts"], attempt)
        self.assertEqual(gate.attempts("46")["retries_left"], 0)
        blocked = gate.can_start("46", 0.05, provider=provider(daily=0.0))
        self.assertFalse(blocked["allowed"])
        self.assertIn("trần retry", blocked["reason"])

    def test_blocked_and_failed_outcomes_send_their_own_event(self):
        gate = self.gate()
        gate.claim("47", 0.05, provider=provider(daily=0.0))
        gate.finish("47", "blocked", 0.002, detail="protected path needs a human")
        self.assertIn("BLOCKED", self.messages()[-1])
        self.assertIn("protected path", self.messages()[-1])
        gate.claim("48", 0.05, provider=provider(daily=0.0))
        gate.finish("48", "failed", 0.002, detail="PyflakesError")
        self.assertIn("THẤT BẠI", self.messages()[-1])

    def test_a_broken_notifier_does_not_break_finish(self):
        def boom(text, **kwargs):
            raise OSError("telegram unreachable")
        # notify.send được thay ở tầng module; hỏng ngay khi gọi
        notify.send = boom
        gate = self.gate()
        gate.claim("49", 0.05, provider=provider(daily=0.0))
        result = gate.finish("49", "done", 0.001, pr="https://example.invalid/pull/10")
        self.assertFalse(result["notified"]["ok"])
        self.assertEqual(result["notified"]["error"], "OSError")
        self.assertEqual(result["usage_today"]["issues"], 1)     # vẫn đóng issue đúng

    def test_a_credential_shaped_notification_is_blocked_before_sending(self):
        notify.send = self._real_send                        # dùng hàm thật, chỉ dry-run
        blocked = notify.redact_safe("PR xong, key sk-or-v1-deadbeef")
        self.assertNotIn("sk-or-v1-deadbeef", blocked)


class WatchdogIntegrationTests(IntegrationBase):
    def test_enforce_pauses_the_guard_when_the_cap_is_hit(self):
        guard = budget.BudgetGuard(self.budget_dir)
        guard.record("issue-1", 3.2)
        state = budget_watchdog.evaluate(guard, provider(daily=0.0))
        self.assertTrue(state["over_cap"])
        action = budget_watchdog.enforce(guard, "")           # job_id rỗng: không chạm cron
        self.assertTrue(action["state_changed"])
        self.assertTrue(guard.is_paused())                     # kill switch đã bật
        again = budget_watchdog.enforce(guard, "")
        self.assertFalse(again["state_changed"])               # không thông báo lặp

    def test_the_paused_budget_guard_blocks_the_worker_gate(self):
        budget.BudgetGuard(self.budget_dir).set_paused(True, "test")
        verdict = self.gate().can_start("50", 0.05, provider=provider(daily=0.0))
        self.assertFalse(verdict["allowed"])
        self.assertIn("PAUSED", verdict["reason"])

    def test_the_watchdog_detects_the_cap_from_the_ledger_when_the_provider_fails(self):
        guard = budget.BudgetGuard(self.budget_dir)
        guard.record("issue-1", 3.05)
        state = budget_watchdog.evaluate(guard, provider(ok=False, error="URLError"))
        self.assertTrue(state["over_cap"])
        self.assertTrue(state["counts_local_only"])

    def test_the_unreadable_provider_notice_is_sent_at_most_once_a_day(self):
        first = budget_watchdog._notify_once("provider-unreadable", "worker_error",
                                             detail="provider down", state_dir=self.budget_dir)
        self.assertTrue(first["ok"])
        second = budget_watchdog._notify_once("provider-unreadable", "worker_error",
                                              detail="provider down", state_dir=self.budget_dir)
        self.assertEqual(second.get("skipped"), "already notified today")
        self.assertEqual(len(self.messages()), 1)

    def test_the_threshold_check_is_quiet_when_there_is_no_work(self):
        guard = budget.BudgetGuard(self.budget_dir)
        result = notify.crossed_thresholds(guard, provider(daily=0.1),
                                           state_dir=self.budget_dir)
        self.assertEqual(result["pending"], [])
        self.assertEqual(self.messages(), [])


class GateAuditTests(IntegrationBase):
    """Bộ phát hiện bypass: issue đã đóng mà không có dấu vết ngân sách."""

    def test_a_settled_run_is_reported_clean(self):
        gate = self.gate()
        gate.claim("60", 0.05, provider=provider(daily=0.0))
        gate.finish("60", "done", 0.002, pr="https://example.invalid/pull/11", notify=False)
        out = gate.audit()
        self.assertTrue(out["ok"], out)
        self.assertIn("issue-60", out["settled_tasks"])
        self.assertIn("60", out["processed_today"])
        self.assertEqual(out["missing_settlement"], [])

    def test_a_run_that_skipped_the_gate_is_detected(self):
        """Mô phỏng worker đi đường cũ (issue_guard finish) — không có hold/không có attempt."""
        store = lib.StateStore(self.state_path)
        with store.transaction():
            store.mark_processed("61", "done", pr="https://example.invalid/pull/12", cost=0.001)
            store.record_usage(0.001)
        out = self.gate().audit()
        self.assertFalse(out["ok"])
        self.assertEqual(out["missing_settlement"], ["61"])
        self.assertIn("61", out["processed_today"])

    def test_the_audit_is_silent_on_a_quiet_day(self):
        out = self.gate().audit()
        self.assertTrue(out["ok"])
        self.assertEqual(out["processed_today"], {})
        self.assertEqual(out["settled_tasks"], {})

    def test_the_audit_only_looks_at_today(self):
        store = lib.StateStore(self.state_path)
        with store.transaction():
            store.mark_processed("62", "done", cost=0.001)
        out = self.gate().audit(day="1999-01-01")
        self.assertTrue(out["ok"])
        self.assertEqual(out["processed_today"], {})


if __name__ == "__main__":
    unittest.main()
