"""Test cho thông báo tiến độ chống trùng (Issue #20).

Kiểm tra: khoá sự kiện ổn định, chống trùng theo khoá, im lặng khi polling rảnh,
sổ chống trùng thiếu/hỏng, che credential, lỗi gửi không làm sập worker, chạy thử
không ghi sổ. Toàn bộ offline — không gọi mạng, không gọi LLM.
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

# Cô lập sổ gửi cho TOÀN BỘ phiên test (xem tests/test_notify.py).
os.environ.setdefault("STEVEN_NOTIFY_LEDGER_FILE",
                      os.path.join(tempfile.mkdtemp(prefix="notify-ledger-"),
                                   "notify-delivery.jsonl"))

import notify  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86_400.0


class FakeRunner:
    """Runner ghi lại lời gọi; có thể trả về lỗi hoặc ném lỗi để kiểm tra biên."""

    def __init__(self, ok: bool = True, raise_exc: Exception | None = None):
        self.ok = ok
        self.raise_exc = raise_exc
        self.calls: list = []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.raise_exc is not None:
            raise self.raise_exc
        return {"ok": self.ok, "returncode": 0 if self.ok else 2, "stdout": ""}


class IdentityTests(unittest.TestCase):
    def test_identity_is_stable_and_excludes_the_clock(self):
        a = notify.event_identity("progress", issue=20, stage="start")
        b = notify.event_identity("progress", issue=20, stage="start")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_identity_changes_per_stage_and_issue(self):
        base = notify.event_identity("progress", issue=20, stage="start")
        self.assertNotEqual(base, notify.event_identity("progress", issue=20, stage="done"))
        self.assertNotEqual(base, notify.event_identity("progress", issue=21, stage="start"))


class EmitTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="notify-dedup-")
        self._old = os.environ.get("STEVEN_NOTIFY_DEDUP_FILE")
        os.environ["STEVEN_NOTIFY_DEDUP_FILE"] = os.path.join(self.dir, "notify-dedup.json")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("STEVEN_NOTIFY_DEDUP_FILE", None)
        else:
            os.environ["STEVEN_NOTIFY_DEDUP_FILE"] = self._old

    def test_start_then_draft_pr_then_done_all_send(self):
        runner = FakeRunner()
        seen = []
        for stage in ("start", "draft-pr", "done"):
            out = notify.emit("progress", issue=20, stage=stage, runner=runner,
                              state_dir=self.dir, now=NOW)
            self.assertTrue(out["sent"], stage)
            seen.append(out["identity"])
        self.assertEqual(len(set(seen)), 3)
        self.assertEqual(len(runner.calls), 3)

    def test_the_same_event_is_deduplicated_and_silent(self):
        runner = FakeRunner()
        first = notify.emit("progress", issue=20, stage="start", runner=runner,
                            state_dir=self.dir, now=NOW)
        second = notify.emit("progress", issue=20, stage="start", runner=runner,
                             state_dir=self.dir, now=NOW + 60)
        self.assertTrue(first["sent"])
        self.assertTrue(second["skipped"])
        self.assertEqual(second["reason"], "duplicate")
        self.assertEqual(len(runner.calls), 1)

    def test_a_new_day_lets_the_event_fire_again(self):
        runner = FakeRunner()
        notify.emit("progress", issue=20, stage="tests", runner=runner,
                    state_dir=self.dir, now=NOW)
        again = notify.emit("progress", issue=20, stage="tests", runner=runner,
                            state_dir=self.dir, now=NOW + 2 * DAY)
        self.assertTrue(again["sent"])
        self.assertEqual(len(runner.calls), 2)

    def test_force_resends(self):
        runner = FakeRunner()
        notify.emit("progress", issue=20, stage="start", runner=runner,
                    state_dir=self.dir, now=NOW)
        out = notify.emit("progress", issue=20, stage="start", runner=runner,
                          state_dir=self.dir, now=NOW, force=True)
        self.assertTrue(out["sent"])
        self.assertEqual(len(runner.calls), 2)

    def test_missing_dedup_state_still_sends(self):
        self.assertFalse(os.path.exists(os.path.join(self.dir, "notify-dedup.json")))
        runner = FakeRunner()
        out = notify.emit("progress", issue=20, stage="start", runner=runner,
                          state_dir=self.dir, now=NOW)
        self.assertTrue(out["sent"])

    def test_corrupt_dedup_state_is_treated_as_empty(self):
        with open(os.path.join(self.dir, "notify-dedup.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        runner = FakeRunner()
        out = notify.emit("progress", issue=20, stage="start", runner=runner,
                          state_dir=self.dir, now=NOW)
        self.assertTrue(out["sent"])

    def test_dry_run_never_calls_the_runner_and_never_records(self):
        runner = FakeRunner()
        out = notify.emit("progress", issue=20, stage="start", runner=runner,
                          state_dir=self.dir, now=NOW, dry_run=True)
        self.assertTrue(out["ok"])
        self.assertFalse(out["sent"])
        self.assertEqual(runner.calls, [])
        # Dry-run không ghi sổ, nên lần gửi thật ngay sau vẫn đi.
        real = notify.emit("progress", issue=20, stage="start", runner=runner,
                           state_dir=self.dir, now=NOW)
        self.assertTrue(real["sent"])

    def test_a_delivery_error_is_bounded_and_not_recorded(self):
        runner = FakeRunner(ok=False)
        out = notify.emit("progress", issue=20, stage="start", runner=runner,
                          state_dir=self.dir, now=NOW)
        self.assertFalse(out["ok"])
        self.assertFalse(out["sent"])
        self.assertFalse(notify.already_emitted(out["identity"], self.dir, NOW))

    def test_a_raising_runner_is_bounded(self):
        runner = FakeRunner(raise_exc=RuntimeError("boom"))
        out = notify.emit("progress", issue=20, stage="start", runner=runner,
                          state_dir=self.dir, now=NOW)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "RuntimeError")

    def test_a_credential_shaped_message_is_delivered_redacted(self):
        runner = FakeRunner()
        notify.emit("custom", issue=20, runner=runner, state_dir=self.dir, now=NOW,
                    text="leak sk-or-abcdefghijklmnop")
        self.assertEqual(len(runner.calls), 1)
        delivered = runner.calls[0][2]
        self.assertNotIn("sk-or-", delivered)
        self.assertIn("credential", delivered)

    def test_the_dedup_ledger_never_stores_the_body(self):
        runner = FakeRunner()
        notify.emit("progress", issue=20, stage="start", detail="chi tiết riêng",
                    runner=runner, state_dir=self.dir, now=NOW)
        with open(os.path.join(self.dir, "notify-dedup.json"), "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("chi tiết riêng", raw)
        json.loads(raw)  # vẫn là JSON hợp lệ


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="notify-cli-")
        self._old = os.environ.get("STEVEN_NOTIFY_DEDUP_FILE")
        os.environ["STEVEN_NOTIFY_DEDUP_FILE"] = os.path.join(self.dir, "notify-dedup.json")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("STEVEN_NOTIFY_DEDUP_FILE", None)
        else:
            os.environ["STEVEN_NOTIFY_DEDUP_FILE"] = self._old

    def test_progress_dry_run_exits_zero(self):
        code = notify.main(["--event", "progress", "--issue", "20", "--stage", "start",
                            "--state-dir", self.dir, "--dry-run"])
        self.assertEqual(code, 0)

    def test_a_second_progress_call_through_the_cli_is_silent(self):
        runner = FakeRunner()
        first = notify.emit("progress", issue=20, stage="start", runner=runner,
                            state_dir=self.dir, now=NOW)
        self.assertTrue(first["sent"])
        # CLI đọc cùng sổ chống trùng ⇒ phải bỏ qua
        code = notify.main(["--event", "progress", "--issue", "20", "--stage", "start",
                            "--state-dir", self.dir, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(notify.already_emitted(first["identity"], self.dir, NOW))


if __name__ == "__main__":
    unittest.main()
