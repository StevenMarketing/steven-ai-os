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

# Cô lập sổ gửi cho TOÀN BỘ phiên test: không đường gọi nào (kể cả đường không truyền
# state_dir) được chạm vào sổ vận hành thật.
os.environ.setdefault("STEVEN_NOTIFY_LEDGER_FILE",
                      os.path.join(tempfile.mkdtemp(prefix="notify-ledger-"),
                                   "notify-delivery.jsonl"))

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
        verdict = self.gate().can_start("43", 0.05, provider=provider(daily=budget.DEFAULT_DAILY_USD - 0.01))
        self.assertFalse(verdict["allowed"])
        self.assertIn("trần ngày", verdict["reason"])
        self.assertFalse(self.gate().claim("43", 0.05, provider=provider(daily=budget.DEFAULT_DAILY_USD - 0.01))["claimed"])

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
            gate.finish("46", "failed", 0.01, detail="tests failed", notify=False,
                        force=True)      # force: mô phỏng ghi lại sau khi gỡ chặn thủ công
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
        guard.record("issue-1", budget.DEFAULT_DAILY_USD + 0.2)
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
        guard.record("issue-1", budget.DEFAULT_DAILY_USD + 0.05)
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


    def test_the_bypass_alert_is_sent_at_most_once_a_day(self):
        store = lib.StateStore(self.state_path)
        with store.transaction():
            store.mark_processed("70", "done", cost=0.001)
        gate = self.gate()
        out = gate.audit()
        self.assertFalse(out["ok"])
        first = gate.notify_once_for_audit(out)
        self.assertTrue(first["ok"], first)
        second = gate.notify_once_for_audit(out)
        self.assertEqual(second.get("skipped"), "already notified today")
        self.assertEqual(len(self.messages()), 1)


class GateStatePathTests(unittest.TestCase):
    """Regression: cổng phải đọc ĐÚNG sổ cái worker (lỗi thật phát hiện 2026-10-16).

    Trước đây `Gate()` dùng thẳng `lib.default_state_dir()` — một **thư mục** — nên
    `_load()` đọc file rỗng và cổng tưởng chưa issue nào được xử lý. Hệ quả: một issue
    `hermes-done` vẫn được `can_start` cho phép chạy lại, và hạn mức issue/ngày của worker
    ($0.15, 3 issue) không bao giờ chạm trần.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="wg-home-")
        self.previous = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self.home

        def restore():
            if self.previous is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = self.previous
        self.addCleanup(restore)
        self.budget_dir = os.path.join(self.home, "budget")

    def test_the_default_state_path_follows_the_guard_convention(self):
        gate = worker_gate.Gate(budget_dir=self.budget_dir)
        self.assertEqual(gate.state_path,
                         os.path.join(lib.default_state_dir(), "state.json"))
        self.assertFalse(os.path.isdir(gate.state_path))

    def test_a_directory_argument_resolves_to_state_json(self):
        directory = os.path.join(self.home, "worker")
        os.makedirs(directory, exist_ok=True)
        gate = worker_gate.Gate(directory, budget_dir=self.budget_dir)
        self.assertEqual(gate.state_path, os.path.join(directory, "state.json"))

    def test_a_done_issue_is_refused_through_the_default_path(self):
        """Đây là test sẽ bắt được lỗi: đóng issue rồi hỏi lại qua đường dẫn mặc định."""
        store = lib.StateStore(os.path.join(lib.default_state_dir(), "state.json"))
        with store.transaction():
            store.mark_processed("2", "done", pr="https://example.invalid/pull/4", cost=0.05)
            store.record_usage(0.05)
        gate = worker_gate.Gate(budget_dir=self.budget_dir)
        verdict = gate.can_start("2", 0.05, provider=provider(daily=0.0))
        self.assertFalse(verdict["allowed"])
        self.assertIn("already processed", verdict["reason"])
        # hạn mức issue/ngày cũng phải thấy 1 issue đã dùng
        self.assertEqual(gate.state.usage()["issues"], 1)


class FinishIdempotencyTests(IntegrationBase):
    """Mục 2: gọi finish lần hai KHÔNG được gửi tin trùng và KHÔNG ghi chi phí trùng."""

    def test_a_second_finish_sends_nothing_and_records_no_cost(self):
        gate = self.gate()
        gate.claim("80", 0.05, provider=provider(daily=0.0))
        first = gate.finish("80", "done", 0.002, pr="https://example.invalid/pull/80",
                            tests="1 passed")
        self.assertTrue(first["notified"]["ok"])
        messages_before = len(self.messages())
        ledger_before = len(budget.BudgetGuard(self.budget_dir).entries())

        second = gate.finish("80", "done", 0.002, pr="https://example.invalid/pull/80",
                             tests="1 passed")
        self.assertTrue(second["already_settled"])
        self.assertEqual(second["cost_recorded_usd"], 0.0)
        self.assertEqual(second["hold_released_usd"], 0.0)
        self.assertEqual(second["settled_as"], "done")
        self.assertIn("already settled", second["notified"]["skipped"])
        self.assertEqual(len(self.messages()), messages_before)          # không tin thứ 2
        self.assertEqual(len(budget.BudgetGuard(self.budget_dir).entries()),
                         ledger_before)                                   # không chi phí thứ 2
        self.assertEqual(second["attempts"], 1)

    def test_the_spend_is_unchanged_after_the_duplicate_attempt(self):
        gate = self.gate()
        gate.claim("82", 0.05, provider=provider(daily=0.0))
        gate.finish("82", "done", 0.03, notify=False)
        before = budget.BudgetGuard(self.budget_dir).spend_today(provider(daily=0.0))["spend"]
        gate.finish("82", "done", 0.03, notify=False)
        after = budget.BudgetGuard(self.budget_dir).spend_today(provider(daily=0.0))["spend"]
        self.assertAlmostEqual(before, after, places=9)

    def test_force_re_settlement_is_an_explicit_opt_in(self):
        gate = self.gate()
        gate.claim("81", 0.05, provider=provider(daily=0.0))
        gate.finish("81", "done", 0.002, notify=False)
        again = gate.finish("81", "done", 0.003, notify=False, force=True)
        self.assertFalse(again.get("already_settled", False))
        self.assertEqual(again["attempts"], 2)          # có ý định thì vẫn ghi được


class LegacyAckTests(IntegrationBase):
    """Mục 4: ngoại lệ legacy phải hiển thị rõ, và KHÔNG được bịt cảnh báo task mới."""

    def _ack(self, payload):
        path = os.path.join(self.tmp, "ack.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def _process(self, issue, cost=0.05):
        store = lib.StateStore(self.state_path)
        with store.transaction():
            store.mark_processed(issue, "done", cost=cost)

    def test_an_acknowledged_legacy_issue_is_listed_but_no_longer_red(self):
        self._process("2")
        gate = worker_gate.Gate(self.state_path, self.budget_dir,
                                ack_file=self._ack({"2": {"reason": "closed before the gate"}}))
        out = gate.audit()
        self.assertTrue(out["ok"])
        self.assertEqual(out["missing_settlement"], [])
        self.assertIn("2", out["acknowledged_legacy"])                  # vẫn hiển thị
        self.assertEqual(out["acknowledged_legacy"]["2"]["reason"], "closed before the gate")
        self.assertIn("acknowledged_legacy", out["note"])
        self.assertIn("2", out["processed_today"])                      # lịch sử còn nguyên

    def test_the_ack_file_never_silences_a_new_issue(self):
        self._process("2")
        self._process("99")                                             # task MỚI, không ack
        gate = worker_gate.Gate(self.state_path, self.budget_dir,
                                ack_file=self._ack({"2": {"reason": "legacy"}}))
        out = gate.audit()
        self.assertFalse(out["ok"])                                     # vẫn ĐỎ
        self.assertEqual(out["missing_settlement"], ["99"])
        self.assertEqual(list(out["acknowledged_legacy"].keys()), ["2"])

    def test_a_missing_or_broken_ack_file_is_not_an_error(self):
        self._process("2")
        for payload in (None, "not json"):
            path = os.path.join(self.tmp, "nope.json")
            if payload is not None:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(payload)
            gate = worker_gate.Gate(self.state_path, self.budget_dir, ack_file=path)
            out = gate.audit()
            self.assertFalse(out["ok"])                                 # không có ack ⇒ đỏ
            self.assertEqual(out["missing_settlement"], ["2"])
            self.assertEqual(out["acknowledged_legacy"], {})

    def test_the_shipped_ack_file_only_names_pre_gate_issues(self):
        """File ngoại lệ đi kèm repo: chỉ chứa issue đóng TRƯỚC khi cổng lên main."""
        shipped = os.path.join(ROOT, "scripts", "legacy_unsettled.json")
        with open(shipped, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(sorted(data.keys()), ["2"])
        entry = data["2"]
        self.assertIn("2026-10-16T05:15:23Z", entry["reason"])
        self.assertIn("gate_landed_on_main_at", entry["evidence"])
        self.assertIn("never matches a new issue", entry["scope"])
        self.assertIn("pre-gate", entry["scope"])


class WorkerCapTests(IntegrationBase):
    """Mục 5: trần worker $0.20/ngày (3 × $0.06 = $0.18 ⇒ trần cũ $0.15 chặn issue thứ ba)."""

    def _use(self, cost, times=1):
        store = lib.StateStore(self.state_path)
        with store.transaction():
            for _ in range(times):
                store.record_usage(cost)

    def test_the_default_caps_allow_three_issues_a_day(self):
        self._use(0.06, times=2)                      # 2 issue, đã dùng $0.12
        caps = worker_gate.issue_caps()
        self.assertEqual(caps["per_issue_usd"], 0.06)
        self.assertEqual(caps["per_day_usd"], 0.20)
        self.assertEqual(caps["issues_per_day"], 3)
        verdict = self.gate().can_start("90", 0.06, provider=provider(daily=0.0))
        self.assertTrue(verdict["allowed"], verdict)

    def test_the_old_cap_would_have_refused_the_third_issue(self):
        self._use(0.06, times=2)
        store = lib.StateStore(self.state_path)
        allowed, reason = store.can_start("90", 0.06, max_per_issue=0.06,
                                          max_per_day=0.15, max_issues_per_day=3)
        self.assertFalse(allowed)
        self.assertIn("daily budget", reason)
        self.assertIn("0.15", reason)

    def test_the_caps_can_be_tuned_without_touching_protected_code(self):
        os.environ["STEVEN_WORKER_PER_DAY_USD"] = "0.05"
        self.addCleanup(os.environ.pop, "STEVEN_WORKER_PER_DAY_USD", None)
        self.assertEqual(worker_gate.issue_caps()["per_day_usd"], 0.05)
        report = worker_gate.Gate(self.state_path, self.budget_dir).report(provider(daily=0.0))
        self.assertEqual(report["worker"]["cost_cap_usd"], 0.05)
        self.assertEqual(report["worker"]["library_defaults_usd"]["per_day"], 0.15)

    def test_the_report_shows_where_the_caps_come_from(self):
        report = self.gate().report(provider(daily=0.0))
        self.assertIn("worker_gate", report["worker"]["caps_source"])
        self.assertEqual(report["worker"]["library_defaults_usd"]["per_issue"], 0.06)


if __name__ == "__main__":
    unittest.main()
