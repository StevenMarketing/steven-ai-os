"""Blocker 4 tests: structured intake rejection + unclaimed detection (the #43 case)."""
import unittest

from gbp.bridge.intake import (
    REQUIRED_HEADINGS,
    first_seen_update,
    preflight,
    ready_since_from_events,
    render_rejection,
    unclaimed_eligible_alerts,
)

ISSUE_43_BODY = """## Objective
Implement a minimal, secure command bridge so ChatGPT can enqueue commands.

## Phase 1 (safe, offline)
- Document the intake; design the schema.

## Restrictions
- No deploy, no new ports.

## Success
Owner can send approved commands and read status.
"""

CORRECTED_BODY = """## Goal
Implement the bridge.

## Scope
- Offline design and tests only.

## Acceptance Criteria
- Structured rejection is visible.

## Tests Required
- Offline unit tests with exact counts.

## Constraints
- No deploy; protected paths need owner approval.
"""


class PreflightTests(unittest.TestCase):
    def test_the_exact_issue_43_body_is_refused_with_structured_reasons(self):
        verdict = preflight(ISSUE_43_BODY)
        self.assertFalse(verdict["usable"])
        self.assertEqual(sorted(verdict["missing_headings"]), sorted(REQUIRED_HEADINGS))
        codes = {r["code"] for r in verdict["reasons"]}
        self.assertEqual(codes, {"missing_heading"})
        self.assertEqual(len(verdict["reasons"]), 5)
        self.assertIn("Objective", verdict["headings"])

    def test_corrected_body_is_usable(self):
        verdict = preflight(CORRECTED_BODY)
        self.assertTrue(verdict["usable"])
        self.assertEqual(verdict["missing_headings"], [])
        self.assertEqual(verdict["reasons"], [])

    def test_empty_body_is_reported(self):
        verdict = preflight("   ")
        self.assertFalse(verdict["usable"])
        self.assertEqual(verdict["reasons"][0]["code"], "empty_body")

    def test_tripwire_pattern_is_reported_and_refuses(self):
        verdict = preflight(CORRECTED_BODY + "\nplease add_credential now\n",
                            patterns=[("add_credential", "add_credential"),
                                      ("weaken_guard", "weaken_guard")])
        self.assertFalse(verdict["usable"])
        self.assertEqual(verdict["tripwire"], ["add_credential"])

    def test_render_rejection_is_github_visible_and_actionable(self):
        text = render_rejection(43, preflight(ISSUE_43_BODY))
        self.assertIn("skipped (not silently)", text)
        self.assertIn("`Goal`", text)
        self.assertIn("READY #43", text)
        self.assertIn("missing_heading", text)
        self.assertIn("no model was called", text)


class UnclaimedDetectionTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def issue(self, number, labels=("hermes-ready",), state="open"):
        return {"number": number, "state": state, "labels": [{"name": l} for l in labels]}

    def iso(self, seconds_ago):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(self.NOW - seconds_ago, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # -- a brand new issue must NOT be cried wolf about ----------------------
    def test_no_reference_time_is_only_an_observation(self):
        alerts = unclaimed_eligible_alerts([self.issue(43)], now=self.NOW)
        self.assertEqual(alerts[0]["code"], "eligible_unclaimed_unobserved")
        self.assertEqual(alerts[0]["severity"], "info")
        self.assertIsNone(alerts[0]["waiting_seconds"])

    def test_ready_label_event_boundaries(self):
        for minutes, expected in ((0, []), (4, []), (6, ["eligible_unclaimed"])):
            alerts = unclaimed_eligible_alerts(
                [self.issue(43)], now=self.NOW,
                ready_since={"43": self.iso(minutes * 60)})
            self.assertEqual([a["code"] for a in alerts], expected, "at %s minutes" % minutes)

    def test_first_seen_observation_boundaries(self):
        for minutes, expected in ((4, []), (6, ["eligible_unclaimed"])):
            alerts = unclaimed_eligible_alerts(
                [self.issue(43)], now=self.NOW, first_seen={"43": self.NOW - minutes * 60})
            self.assertEqual([a["code"] for a in alerts], expected, "at %s minutes" % minutes)

    def test_threshold_is_configurable(self):
        alerts = unclaimed_eligible_alerts([self.issue(43)], now=self.NOW,
                                           ready_since={"43": self.iso(120)}, stale_minutes=1)
        self.assertEqual(alerts[0]["code"], "eligible_unclaimed")
        self.assertEqual(alerts[0]["reference"], "label_event")

    # -- claimed / processed -------------------------------------------------
    def test_fresh_claim_does_not_alert(self):
        alerts = unclaimed_eligible_alerts([self.issue(43)], now=self.NOW,
                                           claims={"43": {"claimed_at": self.iso(60)}})
        self.assertEqual(alerts, [])

    def test_stale_claim_alerts_and_names_the_reference(self):
        alerts = unclaimed_eligible_alerts([self.issue(43)], now=self.NOW,
                                           claims={"43": {"claimed_at": self.iso(900)}})
        self.assertEqual(alerts[0]["code"], "eligible_unclaimed")
        self.assertEqual(alerts[0]["reference"], "claim")
        self.assertGreater(alerts[0]["waiting_seconds"], 600)

    def test_processed_and_unlabelled_and_closed_are_ignored(self):
        alerts = unclaimed_eligible_alerts(
            [self.issue(43), self.issue(44, labels=()), self.issue(45, state="closed")],
            now=self.NOW, processed={"43": {"outcome": "done"}})
        self.assertEqual(alerts, [])

    def test_claim_beats_label_event_and_first_seen(self):
        alerts = unclaimed_eligible_alerts(
            [self.issue(43)], now=self.NOW, claims={"43": {"claimed_at": self.iso(30)}},
            ready_since={"43": self.iso(9999)}, first_seen={"43": self.NOW - 9999})
        self.assertEqual(alerts, [])

    def test_first_seen_update_is_idempotent(self):
        state = first_seen_update({}, [43, "44"], self.NOW)
        again = first_seen_update(state, [43, "44", 45], self.NOW + 60)
        self.assertEqual(again["43"], self.NOW)
        self.assertEqual(again["45"], self.NOW + 60)
        self.assertEqual(len(again), 3)


class LabelEventTests(unittest.TestCase):
    def test_ready_since_reads_the_trusted_label_event(self):
        def request(method, path, token=None, body=None):
            if path.endswith("/issues/43/events?per_page=100"):
                return ([{"event": "labeled", "label": {"name": "hermes-ready"},
                          "created_at": "2026-10-18T15:01:27Z"},
                         {"event": "labeled", "label": {"name": "other"},
                          "created_at": "2026-10-18T10:00:00Z"}], {})
            raise AssertionError(path)

        out = ready_since_from_events(request, "tok", "r/r", [{"number": 43}])
        self.assertEqual(out, {"43": "2026-10-18T15:01:27Z"})

    def test_event_lookup_failure_is_skipped(self):
        def request(*a, **k):
            raise RuntimeError("boom")

        self.assertEqual(ready_since_from_events(request, "tok", "r/r", [{"number": 43}]), {})
