"""Blocker 2 end-to-end: the external checker reads a published document and alerts."""
import json
import unittest
from datetime import datetime, timezone

from bridge_watchdog_check import collect_and_evaluate, newest_status_document

REPO = "StevenMarketing/steven-ai-os"
EPOCH = datetime(2027, 1, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp()


def comment(document, age_seconds=0):
    return {"body": "header\n```json\n%s\n```\n" % json.dumps(document)}


def document(name="idle"):
    return {"schema": "steven-ai-os/worker-status@1", "repository": REPO,
            "generated_at": "2027-01-15T08:00:00Z", "worker": {"state": name},
            "cron": {"state": "scheduled"}, "budget": {"spend_today": 0.1}}


class WatchdogCheckTests(unittest.TestCase):
    def _request(self, comments, issues=None, label_events=None):
        def request(method, path, token=None, body=None):
            if "/comments" in path:
                return (comments, {})
            if "labels=hermes-ready" in path:
                return (issues or [], {})
            if "/events" in path:
                return (label_events or [], {})
            raise AssertionError(path)

        return request

    def test_fresh_status_with_no_eligible_issue_is_ok(self):
        out = collect_and_evaluate(self._request([comment(document())]), "tok", REPO, 77, now=EPOCH + 60)
        self.assertTrue(out["ok"])
        self.assertTrue(out["have_status"])
        self.assertFalse(out["independence"]["uses_local_cron"])

    def test_no_status_at_all_alerts(self):
        out = collect_and_evaluate(self._request([]), "tok", REPO, 77, now=EPOCH)
        self.assertFalse(out["ok"])
        self.assertIn("status_missing", [a["code"] for a in out["alerts"]])

    def test_stale_status_alerts_using_the_external_clock(self):
        out = collect_and_evaluate(self._request([comment(document())]), "tok", REPO, 77,
                                   now=EPOCH + 4000, max_age_seconds=900)
        self.assertFalse(out["ok"])
        self.assertIn("status_stale", [a["code"] for a in out["alerts"]])

    def test_newest_comment_wins(self):
        older = dict(document("older"))
        older["generated_at"] = "2027-01-15T07:00:00Z"
        found = newest_status_document(self._request([comment(older), comment(document())]), "tok", REPO, 77)
        self.assertEqual(found["worker"]["state"], "idle")

    def test_unclaimed_eligible_issue_with_an_old_label_is_reported(self):
        issues = [{"number": 43, "state": "open", "labels": [{"name": "hermes-ready"}]}]
        events = [{"event": "labeled", "label": {"name": "hermes-ready"},
                   "created_at": "2027-01-15T07:00:00Z"}]          # an hour before EPOCH
        out = collect_and_evaluate(self._request([comment(document())], issues, events),
                                   "tok", REPO, 77, now=EPOCH + 60)
        self.assertFalse(out["ok"])
        codes = [a["code"] for a in out["alerts"]]
        self.assertIn("eligible_unclaimed", codes)
        self.assertGreaterEqual(out["blocking"], 1)

    def test_brand_new_ready_issue_is_only_an_observation(self):
        """A freshly labelled issue must not raise a false alarm."""
        issues = [{"number": 50, "state": "open", "labels": [{"name": "hermes-ready"}]}]
        events = [{"event": "labeled", "label": {"name": "hermes-ready"},
                   "created_at": "2027-01-15T08:00:00Z"}]          # the same minute as EPOCH
        out = collect_and_evaluate(self._request([comment(document())], issues, events),
                                   "tok", REPO, 77, now=EPOCH + 60)
        self.assertTrue(out["ok"])
        self.assertEqual(out["blocking"], 0)
        for alert in out["alerts"]:
            if alert["code"].startswith("eligible_unclaimed"):
                self.assertEqual(alert["severity"], "info")
