"""Regression coverage for real evidence, never ledger-read time, as liveness."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from gbp.ops import heartbeat as hb

spec = importlib.util.spec_from_file_location(
    "freshness_worker_cli", Path(__file__).resolve().parents[1] / "scripts/worker_heartbeat.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class FreshnessEvidenceTests(unittest.TestCase):
    def test_live_claim_without_poll_is_not_alive(self):
        self.assertEqual(hb.STALLED, hb.heartbeat_state(now=1000, running_since=990))

    def test_live_claim_cannot_mask_stale_poll(self):
        self.assertEqual(hb.STALLED, hb.heartbeat_state(
            now=2000, last_poll=100, running_since=1990))

    def test_bad_or_future_timestamps_fail_closed(self):
        for value in [True, float("nan"), float("inf"), "990", 1001]:
            with self.subTest(value=value):
                self.assertEqual(hb.STALLED, hb.heartbeat_state(now=1000, last_poll=value))

    def test_bad_running_timestamp_fails_closed(self):
        for value in [True, float("nan"), float("inf"), 1001]:
            self.assertEqual(hb.STALLED, hb.heartbeat_state(
                now=1000, last_poll=990, running_since=value))

    def test_reader_does_not_create_poll_on_each_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"claims": {}, "processed": {}, "usage": {}}))
            before = state.read_bytes()
            for now in [1000, 5000]:
                args = cli.build_parser().parse_args(["--state", str(state), "--now", str(now)])
                result = cli.build(args)
                self.assertEqual(hb.STALLED, result["heartbeat"])
                self.assertIsNone(result["last_poll_at"])
            self.assertEqual(before, state.read_bytes())

    def test_explicit_poll_distinguishes_idle_and_stalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, audit = Path(tmp) / "state.json", Path(tmp) / "audit.jsonl"
            state.write_text('{}')
            audit.write_text('{"ts":990}\n')
            for now, expected in [(1000, hb.IDLE), (2000, hb.STALLED)]:
                args = cli.build_parser().parse_args([
                    "--state", str(state), "--audit", str(audit), "--now", str(now)])
                self.assertEqual(expected, cli.build(args)["heartbeat"])

    def test_invalid_audit_is_not_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            for raw in ['[]', 'null', '{"ts":true}', '{"ts":NaN}', 'bad json']:
                audit.write_text(raw)
                self.assertIsNone(cli._last_audit_ts(str(audit)))

    def test_invalid_state_shape_reports_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text('[]')
            args = cli.build_parser().parse_args(["--state", str(state), "--now", "1000"])
            result = cli.build(args)
            self.assertEqual(hb.DEGRADED, result["heartbeat"])
            self.assertEqual("state-unreadable", result["blocker"])

    def test_oversized_numbers_fail_closed(self):
        for value in [10**400, -(10**400)]:
            for field in ["now", "last_poll", "running_since", "stall_seconds"]:
                args = dict(now=1000, last_poll=990, running_since=990, stall_seconds=900)
                args[field] = value
                with self.subTest(field=field, sign=value > 0):
                    self.assertEqual(hb.STALLED, hb.heartbeat_state(**args))
            with tempfile.TemporaryDirectory() as tmp:
                audit = Path(tmp) / "audit.jsonl"
                audit.write_text(json.dumps({"ts": value}) + "\n")
                self.assertIsNone(cli._last_audit_ts(str(audit)))
