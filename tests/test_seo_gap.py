"""Issue #88 -- offline competitor-gap analysis on synthetic fixtures.

Standard library only, MOCK fixtures only: no network, no Google, no scraping,
no OAuth, no publish. Every fixture here is synthetic and lives in memory or in
a temporary file. Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_seo_gap
"""

from __future__ import annotations

import copy
import json
import os
import re
import socket
import sys
import tempfile
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.seo import gap as gap_mod  # noqa: E402
from gbp.seo.audit import audit_location  # noqa: E402
from gbp.seo.gap import (  # noqa: E402
    SMALL_N_MINIMUM,
    analyze_gap,
    load_gap_fixture,
)

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")

_DESCRIPTION = (
    "Synthetic fixture shop used only for offline tests: manicures, pedicures, "
    "gel nails and careful nail health, with enough prose for the length "
    "checks to have something realistic to measure without any real customer "
    "or business data appearing anywhere in this file or in a gap report."
)


def _hours() -> dict:
    return {day: {"open": "09:00", "close": "19:00"} for day in _WEEKDAYS}


def _profile(**overrides):
    """A complete synthetic profile; tests override only what they care about."""
    base = {
        "location_name": "accounts/1001/locations/loc-900",
        "title": "Fixture Nails",
        "city": "Garden Grove", "state": "CA", "postal_code": "92841",
        "primary_category": "Nail Salon",
        "secondary_categories": ["Manicure", "Pedicure"],
        "services": ["Manicure", "Pedicure", "Gel Nails"],
        "description": _DESCRIPTION,
        "hours": _hours(),
        "phone": "+1 714-555-0100",
        "website": "https://example.invalid/fixture",
    }
    base.update(overrides)
    return base


def _competitor(index: int, **overrides):
    """A direct competitor: complete profile, distinct resource id."""
    defaults = {
        "location_name": f"accounts/2002/locations/cmp-{index:03d}",
        "title": f"Fixture Competitor {index}",
    }
    defaults.update(overrides)
    return _profile(**defaults)


def _target(**overrides):
    defaults = {
        "location_name": "accounts/1001/locations/loc-001",
        "title": "Fixture Target",
    }
    defaults.update(overrides)
    return _profile(**defaults)


def _three_direct():
    """Three direct competitors (the small-N boundary) with distinct scores."""
    return [
        _competitor(101, services=["Manicure", "Pedicure", "Gel Nails",
                                   "Dip Powder", "Acrylic Nails"],
                    secondary_categories=["Manicure", "Pedicure", "Nail Art"]),
        _competitor(102, services=["Manicure", "Gel Nails", "Dip Powder",
                                   "Shellac"],
                    secondary_categories=["Manicure", "Pedicure", "Waxing"],
                    description="Short."),
        _competitor(103, services=["Manicure", "Pedicure", "Dip Powder"],
                    secondary_categories=["Manicure", "Pedicure", "Nail Art",
                                          "Waxing"],
                    hours={day: {"open": "10:00", "close": "18:00"}
                           for day in _WEEKDAYS[:5]}),
    ]


class CoverageTests(unittest.TestCase):
    def test_common_service_missing_from_target_is_the_top_gap(self):
        report = analyze_gap(_target(), _three_direct())
        gaps = report.services["gaps"]
        self.assertTrue(gaps, report.services)
        self.assertEqual(gaps[0]["name"], "Dip Powder")
        self.assertEqual(gaps[0]["competitor_count"], 3)
        self.assertEqual(gaps[0]["competitor_share"], 100)
        self.assertFalse(gaps[0]["target_lists"])

    def test_target_unique_item_is_not_a_gap(self):
        target = _target(services=["Manicure", "Pedicure", "French Tips"])
        report = analyze_gap(target, _three_direct())
        names = [item["name"] for item in report.services["items"]]
        self.assertIn("French Tips", names)
        unique = [item for item in report.services["items"]
                  if item["name"] == "French Tips"][0]
        self.assertEqual(unique["competitor_count"], 0)
        self.assertEqual(unique["competitor_share"], 0)
        self.assertTrue(unique["target_lists"])
        self.assertNotIn("French Tips",
                         [gap["name"] for gap in report.services["gaps"]])

    def test_share_is_a_rounded_percentage_next_to_the_raw_count(self):
        report = analyze_gap(_target(), _three_direct())
        gel = [item for item in report.services["items"]
               if item["name"] == "Gel Nails"][0]
        self.assertEqual(gel["competitor_count"], 2)
        self.assertEqual(gel["competitor_share"], 67)

    def test_gap_list_is_sorted_by_share_then_name(self):
        report = analyze_gap(_target(), _three_direct())
        gaps = report.services["gaps"]
        # 3 of 3 first, then the 1-of-3 items in ascending name order.
        self.assertEqual([gap["name"] for gap in gaps],
                         ["Dip Powder", "Acrylic Nails", "Shellac"])
        shares = [gap["competitor_share"] for gap in gaps]
        self.assertEqual(shares, sorted(shares, reverse=True))

    def test_secondary_categories_use_the_same_union_rule(self):
        report = analyze_gap(_target(), _three_direct())
        categories = report.secondary_categories
        self.assertEqual(categories["total_direct"], 3)
        self.assertEqual([gap["name"] for gap in categories["gaps"]],
                         ["Nail Art", "Waxing"])
        self.assertTrue(all(gap["target_lists"] is False
                            for gap in categories["gaps"]))

    def test_a_competitor_with_no_services_contributes_nothing(self):
        competitor = _competitor(104, services=[])
        report = analyze_gap(_target(), _three_direct() + [competitor])
        self.assertEqual(report.direct_competitor_count, 4)
        # The empty competitor cannot raise any share because it lists nothing.
        dip = [item for item in report.services["items"]
               if item["name"] == "Dip Powder"][0]
        self.assertEqual(dip["competitor_count"], 3)
        self.assertEqual(dip["competitor_share"], 75)


class BenchmarkTests(unittest.TestCase):
    def test_median_min_max_against_the_target_score(self):
        competitors = [_competitor(201), _competitor(202),
                       _competitor(203, website="", description="")]  # 70
        report = analyze_gap(_target(), competitors)
        benchmarks = report.benchmarks
        self.assertEqual(benchmarks["label"], "heuristic completeness")
        self.assertEqual(benchmarks["direct_competitors"], 3)
        self.assertEqual(benchmarks["target"], 100)
        self.assertEqual(benchmarks["min"], 70)
        self.assertEqual(benchmarks["max"], 100)
        self.assertEqual(benchmarks["median"], 100)
        self.assertFalse(benchmarks["target_below_median"])

    def test_three_set_benchmarks_agree_with_the_reported_scores(self):
        report = analyze_gap(_target(website=""), _three_direct())
        scores = sorted(entry["score"] for entry in report.direct_set)
        self.assertEqual(report.benchmarks["min"], scores[0])
        self.assertEqual(report.benchmarks["max"], scores[-1])
        self.assertEqual(report.benchmarks["median"], scores[len(scores) // 2])
        self.assertNotEqual(report.benchmarks["min"], report.benchmarks["max"])

    def test_even_sized_set_averages_the_two_middles(self):
        competitors = [
            _competitor(201, website="", description=""),      # 70
            _competitor(202, description=""),                  # 85
            _competitor(203),                                  # 100
            _competitor(204),                                  # 100
        ]
        report = analyze_gap(_target(), competitors)
        scores = sorted(entry["score"] for entry in report.direct_set)
        self.assertEqual(scores, [70, 85, 100, 100])
        self.assertEqual(report.benchmarks["median"], 92.5)
        self.assertEqual(report.benchmarks["min"], 70)
        self.assertEqual(report.benchmarks["max"], 100)

    def test_target_below_median_gets_a_labelled_completeness_note(self):
        target = _target(website="", description="", phone="")
        report = analyze_gap(target, _three_direct())
        self.assertTrue(report.benchmarks["target_below_median"])
        notes = [rec for rec in report.recommendations
                 if rec["kind"] == "completeness"]
        self.assertEqual(len(notes), 1, report.recommendations)
        self.assertIn("heuristic completeness", notes[0]["text"])
        self.assertRegex(notes[0]["evidence"], r"\d+ direct competitor")
        self.assertIn("direct competitors", notes[0]["evidence"])

    def test_benchmarks_are_absent_when_data_is_insufficient(self):
        report = analyze_gap(_target(), _three_direct()[:2])
        self.assertTrue(report.insufficient)
        self.assertIsNone(report.benchmarks)


class FieldPresenceTests(unittest.TestCase):
    def test_presence_rates_versus_the_target(self):
        report = analyze_gap(_target(), _three_direct())
        rates = {row["field"]: row for row in report.field_presence}
        self.assertEqual([row["field"] for row in report.field_presence],
                         [key for key, _label in gap_mod.FIELD_PRESENCE_FIELDS])
        self.assertEqual(rates["website"]["present_count"], 3)
        self.assertEqual(rates["website"]["competitor_share"], 100)
        self.assertEqual(rates["full_week_hours"]["present_count"], 2)
        self.assertEqual(rates["full_week_hours"]["competitor_share"], 67)
        self.assertEqual(rates["description_over_250"]["present_count"], 2)
        self.assertTrue(rates["website"]["target_present"])
        self.assertTrue(rates["description_over_250"]["target_present"])

    def test_missing_target_field_yields_a_conditional_recommendation(self):
        report = analyze_gap(_target(website=""), _three_direct())
        field_recs = [rec for rec in report.recommendations
                      if rec["kind"] == "field"]
        self.assertEqual([rec["subject"] for rec in field_recs], ["website"])
        self.assertTrue(field_recs[0]["text"].startswith(
            "If the salon can add"))
        self.assertIn("3 of 3 direct competitors", field_recs[0]["evidence"])

    def test_present_target_field_gets_no_recommendation(self):
        report = analyze_gap(_target(), _three_direct())
        self.assertEqual([rec for rec in report.recommendations
                          if rec["kind"] == "field"], [])


class SelectionTests(unittest.TestCase):
    def test_self_entry_is_excluded_by_resource_id(self):
        self_copy = _target()
        report = analyze_gap(_target(), _three_direct() + [self_copy])
        self.assertEqual(report.direct_competitor_count, 3)
        reasons = [entry["reason"] for entry in report.skipped]
        self.assertTrue(any("self" in reason for reason in reasons), reasons)
        self.assertNotIn(self_copy["location_name"],
                         [entry["location_name"]
                          for entry in report.direct_set])

    def test_blank_resource_ids_are_not_treated_as_self(self):
        target = _target(location_name="")
        competitor = _competitor(105, location_name="")
        report = analyze_gap(target, _three_direct() + [competitor])
        self.assertEqual(report.direct_competitor_count, 4)
        self.assertEqual(report.skipped, [])

    def test_different_primary_category_is_excluded_with_a_reason(self):
        barber = _competitor(106, primary_category="Barber Shop")
        report = analyze_gap(_target(), _three_direct() + [barber])
        self.assertEqual(report.direct_competitor_count, 3)
        self.assertEqual(len(report.excluded), 1)
        excluded = report.excluded[0]
        self.assertEqual(excluded["primary_category"], "Barber Shop")
        self.assertEqual(excluded["location_name"],
                         barber["location_name"])
        self.assertIn("differs from the target primary category",
                      excluded["reason"])

    def test_malformed_entries_are_skipped_never_invented(self):
        report = analyze_gap(_target(), [None, 42, "not-a-profile", {}])
        # {} is a mapping but carries no primary category -> excluded, never
        # invented into a direct competitor.
        self.assertEqual(report.direct_competitor_count, 0)
        self.assertEqual(len(report.skipped), 3)
        for entry in report.skipped:
            self.assertIn("reason", entry)
            self.assertIn("not a mapping", entry["reason"])
        self.assertTrue(any("int" in entry["reason"] for entry in report.skipped))
        self.assertEqual(len(report.excluded), 1)
        self.assertIn("no primary category", report.excluded[0]["reason"])

    def test_a_competitor_without_a_category_is_excluded(self):
        report = analyze_gap(_target(), _three_direct()
                             + [_competitor(107, primary_category="")])
        self.assertEqual(report.direct_competitor_count, 3)
        self.assertIn("no primary category", report.excluded[0]["reason"])

    def test_malformed_list_field_is_noted_not_invented(self):
        broken = _competitor(108, services=1234)
        report = analyze_gap(_target(), _three_direct() + [broken])
        self.assertEqual(report.direct_competitor_count, 4)
        self.assertEqual([note["field"] for note in report.notes], ["services"])
        self.assertIn("is not a list", report.notes[0]["reason"])
        # It contributes no services rather than a made-up list.
        dip = [item for item in report.services["items"]
               if item["name"] == "Dip Powder"][0]
        self.assertEqual(dip["competitor_count"], 3)
        self.assertEqual(dip["competitor_share"], 75)

    def test_malformed_target_list_field_is_noted(self):
        report = analyze_gap(_target(services=1234), _three_direct())
        self.assertEqual([note["field"] for note in report.notes], ["services"])
        self.assertEqual(report.notes[0]["index"], "target")


class SmallNGuardTests(unittest.TestCase):
    def test_two_direct_competitors_is_insufficient(self):
        report = analyze_gap(_target(), _three_direct()[:2])
        self.assertTrue(report.insufficient)
        self.assertEqual(report.status, "insufficient-competitor-data")
        self.assertIn("2 direct competitor", report.reason)
        self.assertIn("at least 3", report.reason)
        self.assertIsNone(report.services)
        self.assertIsNone(report.secondary_categories)
        self.assertEqual(report.field_presence, [])
        self.assertEqual(report.recommendations, [])

    def test_three_direct_competitors_is_the_boundary_that_reports(self):
        report = analyze_gap(_target(), _three_direct())
        self.assertEqual(SMALL_N_MINIMUM, 3)
        self.assertFalse(report.insufficient)
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.reason, "")
        self.assertTrue(report.recommendations)

    def test_insufficient_report_contains_no_percentages(self):
        report = analyze_gap(_target(), _three_direct()[:1])
        text = report.to_json()
        self.assertNotIn("competitor_share", text)
        self.assertNotIn("percent", text)
        # the resolved set and the reason survive, so the caller can act
        self.assertEqual(report.direct_competitor_count, 1)
        self.assertTrue(report.reason)

    def test_target_without_primary_category_is_insufficient(self):
        report = analyze_gap(_target(primary_category=""), _three_direct())
        self.assertTrue(report.insufficient)
        self.assertIn("no primary category", report.reason)
        self.assertEqual(report.direct_competitor_count, 0)
        self.assertEqual(len(report.excluded), 3)


class ScoringReuseTests(unittest.TestCase):
    def test_every_score_comes_from_the_audit_engine(self):
        target = _target(website="")
        competitors = _three_direct()
        report = analyze_gap(target, competitors)
        self.assertEqual(report.target_score, audit_location(target).score)
        by_id = {entry["location_name"]: entry["score"]
                 for entry in report.direct_set}
        for competitor in competitors:
            self.assertEqual(by_id[competitor["location_name"]],
                             audit_location(competitor).score)

    def test_module_reuses_the_audit_engine_instead_of_re_scoring(self):
        with open(os.path.join(_ROOT, "src", "gbp", "seo", "gap.py"),
                  "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("from .audit import", source)
        self.assertIn("audit_location(", source)
        self.assertNotIn("_SEVERITY_WEIGHT", source)

    def test_inputs_are_never_mutated(self):
        target = _target()
        competitors = _three_direct() + [None]
        before_target = copy.deepcopy(target)
        before_competitors = copy.deepcopy(competitors)
        analyze_gap(target, competitors)
        self.assertEqual(target, before_target)
        self.assertEqual(competitors, before_competitors)


class DeterminismTests(unittest.TestCase):
    def test_identical_input_is_byte_identical(self):
        fixture = load_gap_fixture()
        first = analyze_gap(fixture["target"], fixture["competitors"]).to_json()
        second = analyze_gap(fixture["target"], fixture["competitors"]).to_json()
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), json.loads(second))

    def test_direct_set_order_does_not_change_the_report(self):
        direct = _three_direct()
        others = [None, _competitor(109, primary_category="Barber Shop")]
        forward = analyze_gap(_target(), direct + others).to_json()
        backward = analyze_gap(_target(), list(reversed(direct)) + others).to_json()
        self.assertEqual(forward, backward)

    def test_embedded_fixture_is_a_deep_copy(self):
        first = load_gap_fixture()
        first["target"]["title"] = "mutated"
        first["competitors"].append("mutated")
        self.assertNotEqual(load_gap_fixture()["target"]["title"], "mutated")
        self.assertNotIn("mutated", load_gap_fixture()["competitors"])

    def test_load_gap_fixture_from_a_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gap.json")
            payload = {"target": _target(), "competitors": _three_direct()}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            fixture = load_gap_fixture(path)
        self.assertEqual(fixture["target"]["title"], "Fixture Target")
        self.assertEqual(len(fixture["competitors"]), 3)

    def test_load_gap_fixture_rejects_a_wrong_shape(self):
        for payload in ([1, 2, 3], {"target": []}, {"target": {}, "competitors": {}}):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "gap.json")
                    with open(path, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle)
                    with self.assertRaises(ValueError):
                        load_gap_fixture(path)

    def test_non_mapping_target_raises(self):
        with self.assertRaises(ValueError):
            analyze_gap("nope", _three_direct())  # type: ignore[arg-type]


class WordingSafetyTests(unittest.TestCase):
    _FORBIDDEN = ("guarantee", "#1", "rank higher", "will rank", "top spot",
                  "ranking improvement", "visibility improvement",
                  "ranking impact", "visibility impact")

    def _all_reports(self):
        yield analyze_gap(_target(website="", description="", phone=""),
                          _three_direct())
        yield analyze_gap(load_gap_fixture()["target"],
                          load_gap_fixture()["competitors"])

    def test_recommendations_are_conditional_and_evidence_quoted(self):
        for report in self._all_reports():
            self.assertTrue(report.recommendations)
            for rec in report.recommendations:
                self.assertRegex(rec["evidence"], r"\d+ direct competitor")
                self.assertRegex(rec["text"], r"\d+ direct competitor")
                if rec["kind"] in ("service", "secondary_category", "field"):
                    self.assertTrue(rec["text"].startswith("If the salon"),
                                    rec["text"])
                self.assertIn("consider", rec["text"])

    def test_no_recommendation_claims_a_ranking_or_visibility_outcome(self):
        for report in self._all_reports():
            for rec in report.recommendations:
                text = f"{rec['subject']} {rec['evidence']} {rec['text']}".lower()
                for phrase in self._FORBIDDEN:
                    self.assertNotIn(phrase, text, rec["subject"])
                # the target's own offering is never asserted as fact
                self.assertNotIn("the salon offers it", text)
                self.assertNotIn("the target offers", text)

    def test_report_is_labelled_offline_and_never_ranks(self):
        for report in self._all_reports():
            payload = report.as_dict()
            self.assertTrue(payload["fixture_only"])
            self.assertEqual(payload["source"], "synthetic-fixture")
            self.assertEqual(payload["mode"], "offline-competitor-gap")
            self.assertIn("no ranking or visibility improvement is claimed",
                          payload["disclaimer"])
            self.assertIn("not a ranking score", payload["score_label"])
            self.assertEqual(payload["benchmark_label"], "heuristic completeness")
            self.assertEqual(payload["target"]["score_label"],
                             payload["score_label"])
            for entry in payload["direct_set"]:
                self.assertEqual(entry["score_label"], payload["score_label"])


class OfflineGuardTests(unittest.TestCase):
    def test_no_network_is_attempted(self):
        original = socket.socket

        def _boom(*args, **kwargs):
            raise AssertionError("the gap analysis must never open a socket")

        socket.socket = _boom  # type: ignore[assignment]
        try:
            fixture = load_gap_fixture()
            report = analyze_gap(fixture["target"], fixture["competitors"])
            self.assertEqual(report.direct_competitor_count, 3)
        finally:
            socket.socket = original

    def test_module_source_has_no_network_imports(self):
        with open(os.path.join(_ROOT, "src", "gbp", "seo", "gap.py"),
                  "r", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("import socket", "import urllib", "import requests",
                          "import http", "http.client", "import ssl",
                          "googleapiclient", "import subprocess", "import os"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
