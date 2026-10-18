"""Blocker 2 tests: external (VPS-independent) staleness evaluation."""
import unittest

from gbp.bridge.external_watchdog import INDEPENDENCE, document_age_seconds, evaluate_external

NOW = 1_800_000_000.0
FRESH = {
    "schema": "steven-ai-os/worker-status@1",
    "repository": "StevenMarketing/steven-ai-os",
    "generated_at": "2027-01-15T08:00:00Z",
    "worker": {"state": "idle"},
    "cron": {"state": "scheduled"},
    "budget": {"spend": 0.1},
}
from datetime import datetime, timezone
FRESH_EPOCH = datetime(2027, 1, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp()


class ExternalWatchdogTests(unittest.TestCase):
    def test_fresh_status_has_no_alerts(self):
        out = evaluate_external(FRESH, now=FRESH_EPOCH + 60)
        self.assertTrue(out["ok"])
        self.assertEqual(out["alerts"], [])
        self.assertFalse(out["independence"]["uses_local_cron"])

    def test_missing_status_is_critical(self):
        out = evaluate_external(None, now=FRESH_EPOCH)
        self.assertFalse(out["ok"])
        self.assertEqual(out["alerts"][0]["code"], "status_missing")
        self.assertEqual(out["alerts"][0]["independence"], INDEPENDENCE)

    def test_stale_status_is_detected_with_independent_clock(self):
        out = evaluate_external(FRESH, now=FRESH_EPOCH + 3600)
        self.assertFalse(out["ok"])
        codes = [a["code"] for a in out["alerts"]]
        self.assertIn("status_stale", codes)
        stale = [a for a in out["alerts"] if a["code"] == "status_stale"][0]
        self.assertGreater(stale["age_seconds"], 900)

    def test_frozen_document_age_is_ignored(self):
        doc = dict(FRESH)
        doc["freshness"] = {"document_age_seconds": 1, "generated_at": FRESH["generated_at"]}
        out = evaluate_external(doc, now=FRESH_EPOCH + 7200)
        codes = [a["code"] for a in out["alerts"]]
        self.assertIn("frozen_age_ignored", codes)
        self.assertIn("status_stale", codes)

    def test_unparsable_timestamp_is_unreadable(self):
        doc = dict(FRESH, generated_at="not-a-time")
        out = evaluate_external(doc, now=FRESH_EPOCH)
        self.assertFalse(out["ok"])
        self.assertIn("status_unreadable", [a["code"] for a in out["alerts"]])

    def test_missing_required_field_is_unreadable(self):
        doc = {k: v for k, v in FRESH.items() if k != "cron"}
        out = evaluate_external(doc, now=FRESH_EPOCH)
        self.assertIn("status_unreadable", [a["code"] for a in out["alerts"]])

    def test_future_timestamp_flags_clock_skew(self):
        out = evaluate_external(FRESH, now=FRESH_EPOCH - 600)
        self.assertIn("clock_skew", [a["code"] for a in out["alerts"]])

    def test_document_age_helper(self):
        self.assertAlmostEqual(document_age_seconds(FRESH, FRESH_EPOCH + 30), 30.0, places=3)

    def test_wrong_repository_is_flagged_but_not_fatal(self):
        doc = dict(FRESH, repository="someone/else")
        out = evaluate_external(doc, now=FRESH_EPOCH + 10,
                                expected_repository="StevenMarketing/steven-ai-os")
        self.assertIn("status_wrong_repository", [a["code"] for a in out["alerts"]])
