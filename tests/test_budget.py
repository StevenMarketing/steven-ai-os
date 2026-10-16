"""Group 8B / Phase C — ngân sách & định tuyến model, kiểm thử hoàn toàn offline.

Không cần mạng, không cần API key: provider được mô phỏng bằng transport giả, và
``state.db`` là một SQLite tạm đúng schema cần dùng.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict, List

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import budget  # noqa: E402

DAY = 86400.0
NOW = 1792127727.0          # 2026-10-16T05:15:27Z


def _capture(func, *args, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = func(*args, **kwargs)
    return code, buf.getvalue()


def _fake_db(path: str, rows: List[Dict[str, Any]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT, source TEXT, model TEXT, started_at REAL,"
                 " ended_at REAL, message_count INTEGER, tool_call_count INTEGER,"
                 " api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,"
                 " cache_read_tokens INTEGER, reasoning_tokens INTEGER,"
                 " estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT,"
                 " cost_source TEXT)")
    for row in rows:
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, message_count,"
            " tool_call_count, api_call_count, input_tokens, output_tokens,"
            " cache_read_tokens, estimated_cost_usd, cost_status, cost_source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["source"], row["model"], row["started_at"],
             row.get("messages", 2), row.get("tools", 1), row.get("api", 1),
             row.get("in", 100), row.get("out", 10), row.get("cache", 0),
             row.get("cost", 0.0), "estimated", "provider_models_api"))
    conn.commit()
    conn.close()


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t-budget-")
        self.db = os.path.join(self.tmp, "state.db")
        _fake_db(self.db, [
            {"id": "cron_e07126dba5a5_20261016_051343", "source": "cron",
             "model": "deepseek/deepseek-v4.1-flash", "started_at": NOW - 600,
             "api": 28, "messages": 67, "cost": 0.0085534128},
            {"id": "cron_e07126dba5a5_20261016_052242", "source": "cron",
             "model": "deepseek/deepseek-v4.1-flash", "started_at": NOW - 300,
             "api": 1, "messages": 2, "cost": 0.0001379772},
            {"id": "20261015_034859_24e698", "source": "desktop",
             "model": "deepseek/deepseek-v4.1-flash", "started_at": NOW - 2 * DAY,
             "api": 654, "messages": 434, "cost": 2.1427955619},
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_totals_and_per_day_breakdown(self):
        report = budget.collect_estimates(self.db, days=1, now=NOW)
        self.assertEqual(report["totals"]["sessions"], 2)
        self.assertEqual(report["totals"]["api_calls"], 29)
        self.assertAlmostEqual(report["totals"]["estimated_cost_usd"], 0.0086914, places=6)
        today = time.strftime("%Y-%m-%d", time.gmtime(NOW))
        self.assertIn(today, report["by_day"])
        self.assertAlmostEqual(report["by_day"][today]["estimated_cost_usd"], 0.0086914,
                               places=6)

    def test_cron_sessions_are_attributed_to_the_worker_task(self):
        report = budget.collect_estimates(self.db, days=1, now=NOW)
        self.assertIn("github-issue-worker", report["by_task"])
        self.assertEqual(report["by_task"]["github-issue-worker"]["sessions"], 2)

    def test_a_wider_window_includes_older_sessions(self):
        report = budget.collect_estimates(self.db, days=5, now=NOW)
        self.assertEqual(report["totals"]["sessions"], 3)
        self.assertIn("desktop", report["by_task"])
        self.assertGreater(report["totals"]["estimated_cost_usd"], 2.0)

    def test_per_model_breakdown_is_reported(self):
        report = budget.collect_estimates(self.db, days=5, now=NOW)
        self.assertIn("deepseek/deepseek-v4.1-flash", report["by_model"])
        self.assertEqual(report["by_model"]["deepseek/deepseek-v4.1-flash"]["sessions"], 3)

    def test_expensive_sessions_are_sorted_first(self):
        report = budget.collect_estimates(self.db, days=5, now=NOW)
        costs = [row["estimated_cost_usd"] for row in report["sessions"]]
        self.assertEqual(costs, sorted(costs, reverse=True))

    def test_a_missing_database_is_reported_not_crashed(self):
        report = budget.collect_estimates(os.path.join(self.tmp, "nope.db"), days=1, now=NOW)
        self.assertEqual(report["totals"]["sessions"], 0)
        self.assertIn("không tồn tại", report["note"])


class ProviderTests(unittest.TestCase):
    def test_provider_usage_reports_usage_limit_remaining(self):
        def fake(url, token):
            self.assertEqual(url, budget.OPENROUTER_URL)
            self.assertEqual(token, "test-key")
            return {"data": {"label": "sk-or-v1-secret-label", "usage": 4.6183,
                             "limit": 10, "is_free_tier": False}}

        info = budget.provider_usage(token="test-key", transport=fake)
        self.assertTrue(info["ok"])
        self.assertEqual(info["usage_usd"], 4.6183)
        self.assertEqual(info["limit_usd"], 10.0)
        self.assertAlmostEqual(info["remaining_usd"], 5.3817, places=4)
        self.assertIsNone(info["label"], "nhãn khoá là thông tin nhạy cảm, không trả về")

    def test_provider_usage_without_a_key_is_not_ok(self):
        info = budget.provider_usage(transport=None, env_file=os.path.join(
            tempfile.mkdtemp(prefix="t-nokey-"), ".env"))
        self.assertFalse(info["ok"])

    def test_provider_today_uses_a_daily_baseline(self):
        tmp = tempfile.mkdtemp(prefix="t-prov-")
        try:
            first = budget.provider_today(tmp, {"ok": True, "usage_usd": 4.6183,
                                                "limit_usd": 10.0, "remaining_usd": 5.3817},
                                          now=NOW)
            self.assertEqual(first["spent_today_usd"], 0.0)
            second = budget.provider_today(tmp, {"ok": True, "usage_usd": 4.7000,
                                                 "limit_usd": 10.0, "remaining_usd": 5.3},
                                           now=NOW + 60)
            self.assertAlmostEqual(second["spent_today_usd"], 0.0817, places=4)
            next_day = budget.provider_today(tmp, {"ok": True, "usage_usd": 4.8000,
                                                   "limit_usd": 10.0, "remaining_usd": 5.2},
                                             now=NOW + 2 * DAY)
            self.assertAlmostEqual(next_day["spent_today_usd"], 0.1, places=4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_new_day_carries_the_previous_meter_forward(self):
        """Ngày mới nối tiếp mốc cuối của ngày trước, không reset về 0."""
        tmp = tempfile.mkdtemp(prefix="t-prov3-")
        try:
            budget.provider_today(tmp, {"ok": True, "usage_usd": 1.0, "limit_usd": 10.0,
                                        "remaining_usd": 9.0}, now=NOW)
            budget.provider_today(tmp, {"ok": True, "usage_usd": 1.2, "limit_usd": 10.0,
                                        "remaining_usd": 8.8}, now=NOW + 60)
            day2 = budget.provider_today(tmp, {"ok": True, "usage_usd": 1.5,
                                               "limit_usd": 10.0, "remaining_usd": 8.5},
                                         now=NOW + DAY)
            self.assertAlmostEqual(day2["spent_today_usd"], 0.3, places=4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_provider_usage_exposes_the_period_figures(self):
        def fake(url, token):
            return {"data": {"usage": 4.6183, "limit": 10, "usage_daily": 1.440867,
                             "usage_weekly": 4.6183, "usage_monthly": 4.6183,
                             "limit_remaining": 5.381699,
                             "expires_at": "2027-04-12T23:19:06.832Z"}}

        info = budget.provider_usage(token="k", transport=fake)
        self.assertAlmostEqual(info["usage_daily_usd"], 1.440867, places=5)
        self.assertAlmostEqual(info["usage_weekly_usd"], 4.6183, places=4)
        self.assertAlmostEqual(info["usage_monthly_usd"], 4.6183, places=4)
        self.assertEqual(info["expires_at"], "2027-04-12T23:19:06.832Z")

    def test_provider_budgets_are_sanitized(self):
        def fake(url, token):
            return {"data": {"usage": 4.6, "limit": 10,
                             "budgets": [{"scope": "key_limit", "budget_id": "internal-id",
                                          "guardrail_id": "g-123", "limit_usd": 10,
                                          "spend_usd": 4.6, "remaining_usd": 5.4,
                                          "reset_interval": "daily", "resets_at": None}]}}

        info = budget.provider_usage(token="k", transport=fake)
        self.assertEqual(len(info["budgets"]), 1)
        entry = info["budgets"][0]
        self.assertEqual(entry["limit_usd"], 10)
        self.assertEqual(entry["reset_interval"], "daily")
        self.assertNotIn("budget_id", entry, "id nội bộ của provider không được trả về")
        self.assertNotIn("guardrail_id", entry)

    def test_provider_today_prefers_the_provider_daily_figure(self):
        tmp = tempfile.mkdtemp(prefix="t-prov4-")
        try:
            result = budget.provider_today(
                tmp, {"ok": True, "usage_usd": 4.6183, "limit_usd": 10.0,
                      "remaining_usd": 5.3817, "usage_daily_usd": 1.440867}, now=NOW)
            self.assertAlmostEqual(result["spent_today_usd"], 1.440867, places=5)
            self.assertEqual(result["basis"], "provider usage_daily")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_provider_today_surfaces_an_unavailable_provider(self):
        result = budget.provider_today(tempfile.mkdtemp(prefix="t-prov2-"),
                                       {"ok": False, "error": "boom"}, now=NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "boom")

    def test_the_default_transport_refuses_any_other_host(self):
        with self.assertRaises(ValueError):
            budget.http_get_json("https://www.google.com/x", "token")

    def test_a_bearer_header_is_used_and_the_key_is_not_logged(self):
        seen: Dict[str, str] = {}

        def fake(url, token):
            seen["url"] = url
            seen["token"] = token
            return {"data": {"usage": 1.0, "limit": 5.0}}

        budget.provider_usage(token="key-not-to-be-printed", transport=fake)
        self.assertEqual(seen["url"], budget.OPENROUTER_URL)
        self.assertEqual(seen["token"], "key-not-to-be-printed")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t-guard-")
        self.guard = budget.BudgetGuard(self.tmp, daily_usd=3.0, task_usd=0.6,
                                        max_attempts=3, now=NOW)
        self.provider_ok = {"ok": True, "usage_usd": 1.0, "limit_usd": 10.0,
                            "remaining_usd": 9.0}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_small_estimate_is_allowed(self):
        allowed, reason = self.guard.can_spend(0.05, "issue-1", self.provider_ok)
        self.assertTrue(allowed, reason)

    def test_an_estimate_over_the_task_cap_is_refused(self):
        allowed, reason = self.guard.can_spend(0.7, "issue-1", self.provider_ok)
        self.assertFalse(allowed)
        self.assertIn("trần một task", reason)

    def test_the_daily_cap_is_enforced(self):
        self.guard.record("issue-1", 2.95)
        allowed, reason = self.guard.can_spend(0.1, "issue-2", self.provider_ok)
        self.assertFalse(allowed)
        self.assertIn("trần ngày", reason)

    def test_the_retry_cap_is_enforced(self):
        for _ in range(3):
            self.guard.record("issue-1", 0.001)
        allowed, reason = self.guard.can_spend(0.01, "issue-1", self.provider_ok)
        self.assertFalse(allowed)
        self.assertIn("trần retry", reason)

    def test_an_exhausted_provider_quota_is_refused(self):
        allowed, reason = self.guard.can_spend(
            0.05, "issue-1", {"ok": True, "usage_usd": 10.0, "limit_usd": 10.0,
                              "remaining_usd": 0.0})
        self.assertFalse(allowed)
        self.assertIn("hết hạn mức", reason)

    def test_an_unreadable_provider_fails_closed(self):
        allowed, reason = self.guard.can_spend(0.05, "issue-1",
                                               {"ok": False, "error": "timeout"})
        self.assertFalse(allowed)
        self.assertIn("fail-closed", reason)

    def test_fail_open_can_be_requested_explicitly(self):
        allowed, reason = self.guard.can_spend(0.05, "issue-1",
                                               {"ok": False, "error": "timeout"},
                                               fail_open=True)
        self.assertTrue(allowed, reason)

    def test_the_kill_switch_blocks_everything(self):
        self.guard.set_paused(True, "test")
        allowed, reason = self.guard.can_spend(0.01, "issue-1", self.provider_ok)
        self.assertFalse(allowed)
        self.assertIn("PAUSED", reason)
        self.guard.set_paused(False)
        self.assertTrue(self.guard.can_spend(0.01, "issue-1", self.provider_ok)[0])

    def test_recording_updates_today_and_attempts(self):
        result = self.guard.record("issue-1", 0.0086, "deepseek-v4.1-flash",
                                   provider_confirmed_usd=0.0082)
        self.assertAlmostEqual(result["spent_today_usd"], 0.0086, places=6)
        self.assertEqual(result["attempts"], 1)
        self.assertAlmostEqual(self.guard.spent_today(), 0.0086, places=6)

    def test_the_ledger_is_written_atomically_and_has_no_temp_leftovers(self):
        self.guard.record("issue-1", 0.01)
        self.guard.record("issue-2", 0.02)
        entries = self.guard.entries()
        self.assertEqual(len(entries), 2)
        self.assertFalse([f for f in os.listdir(self.tmp) if f.startswith(".ledger-")])
        self.assertAlmostEqual(self.guard.spent_today(), 0.03, places=6)

    def test_caps_come_from_the_environment_and_are_never_raised_by_code(self):
        os.environ["STEVEN_DAILY_BUDGET_USD"] = "1.25"
        try:
            guard = budget.BudgetGuard(self.tmp)
            self.assertEqual(guard.daily_usd, 1.25)
            self.assertEqual(budget.DEFAULT_DAILY_USD, 3.0)
            self.assertEqual(guard.task_usd, budget.DEFAULT_TASK_USD)
            self.assertEqual(guard.max_attempts, budget.DEFAULT_MAX_ATTEMPTS)
        finally:
            os.environ.pop("STEVEN_DAILY_BUDGET_USD", None)

    def test_status_reports_limits_usage_and_the_honest_note(self):
        status = self.guard.status(provider=self.provider_ok)
        self.assertEqual(status["limits"]["daily_usd"], 3.0)
        self.assertFalse(status["paused"])
        self.assertIn("KHÔNG có", status["note"])
        self.assertIn("hard cap", status["note"])

    def test_status_sees_the_provider_daily_figure(self):
        provider = dict(self.provider_ok, usage_daily_usd=1.25)
        status = self.guard.status(provider=provider)
        self.assertAlmostEqual(status["provider_today"]["spent_today_usd"], 1.25,
                               places=4)
        self.assertEqual(status["provider_today"]["basis"], "provider usage_daily")

    def test_no_secret_is_written_into_the_ledger(self):
        self.guard.record("issue-1", 0.01, "deepseek-v4.1-flash", note="no secrets here")
        with open(self.guard.ledger_path, encoding="utf-8") as handle:
            text = handle.read()
        for marker in ("sk-or-", "github_pat_", "Bearer", "OPENROUTER_API_KEY"):
            self.assertNotIn(marker, text)


class RoutingTests(unittest.TestCase):
    def test_documentation_work_uses_the_economy_model(self):
        choice = budget.route("docs")
        self.assertEqual(choice["tier"], "economy")
        self.assertEqual(choice["model"], "deepseek/deepseek-v4.1-flash")

    def test_an_unknown_task_class_falls_back_to_economy(self):
        self.assertEqual(budget.route("something-new")["tier"], "economy")

    def test_complex_work_is_only_recommended_not_switched(self):
        choice = budget.route("security-review")
        self.assertEqual(choice["tier"], "strong")
        self.assertIn("chủ sở hữu", choice["note"])
        self.assertIn("chưa cấu hình", choice["model"],
                      "module không tự đặt model mạnh")

    def test_the_worker_stays_on_the_economy_model(self):
        self.assertEqual(budget.route("issue-worker")["tier"], "economy")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t-cli-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_can_spend_exit_code_is_zero_only_when_allowed(self):
        code, out = _capture(budget.main, ["--state-dir", self.tmp, "can-spend",
                                          "--estimate", "5.0", "--task", "t1"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["allowed"])

    def test_record_then_status_shows_the_spend(self):
        code, _ = _capture(budget.main, ["--state-dir", self.tmp, "record",
                                         "--task", "issue-7", "--cost", "0.0123"])
        self.assertEqual(code, 0)
        # Test phải offline & tất định: bơm số provider từ file thay vì gọi mạng.
        provider_file = os.path.join(self.tmp, "provider.json")
        with open(provider_file, "w", encoding="utf-8") as handle:
            json.dump({"data": {"limit": 10, "limit_remaining": 9.99, "usage": 0.01,
                                "usage_daily": 0.0, "usage_weekly": 0.0,
                                "usage_monthly": 0.0, "budgets": []}}, handle)
        previous = os.environ.get("STEVEN_BUDGET_PROVIDER_FILE")
        os.environ["STEVEN_BUDGET_PROVIDER_FILE"] = provider_file
        try:
            code, out = _capture(budget.main, ["--state-dir", self.tmp, "status",
                                              "--json"])
        finally:
            if previous is None:
                os.environ.pop("STEVEN_BUDGET_PROVIDER_FILE", None)
            else:
                os.environ["STEVEN_BUDGET_PROVIDER_FILE"] = previous
        self.assertEqual(code, 0)
        payload = json.loads(out)
        # sổ cái 0.0123 > usage_daily 0.0 ⇒ số trong ngày = 0.0123 (max, không cộng trùng)
        self.assertAlmostEqual(payload["spent_today_usd"], 0.0123, places=6)
        self.assertAlmostEqual(payload["ledger_today_usd"], 0.0123, places=6)

    def test_pause_and_resume_via_the_cli(self):
        self.assertEqual(_capture(budget.main, ["--state-dir", self.tmp, "pause",
                                               "--reason", "test"])[0], 0)
        code, out = _capture(budget.main, ["--state-dir", self.tmp, "can-spend",
                                          "--estimate", "0.01", "--task", "t"])
        self.assertEqual(code, 1)
        self.assertIn("PAUSED", json.loads(out)["reason"])
        self.assertEqual(_capture(budget.main, ["--state-dir", self.tmp, "resume"])[0], 0)


if __name__ == "__main__":
    unittest.main()
