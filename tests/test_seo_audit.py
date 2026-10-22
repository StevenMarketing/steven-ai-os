"""Issue #39 -- offline GMap SEO audit for multi-location nail salons.

Standard library only, MOCK fixtures only: no network, no Google, no OAuth, no
publish. Every fixture here is synthetic and lives in memory or in a temporary
file. Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_seo_audit
"""

from __future__ import annotations

import copy
import json
import os
import socket
import sys
import tempfile
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.seo import audit as seo  # noqa: E402
from gbp.seo import audit_location, audit_profiles, load_profiles  # noqa: E402


def _codes(location_audit):
    return [f.code for f in location_audit.findings]


def _profile(**overrides):
    """A complete synthetic profile; every test overrides what it cares about."""
    base = {
        "location_name": "accounts/1001/locations/loc-900",
        "title": "Fixture Nails",
        "city": "Garden Grove", "state": "CA", "postal_code": "92841",
        "primary_category": "Nail Salon",
        "secondary_categories": ["Manicure", "Pedicure"],
        "services": ["Manicure", "Pedicure", "Gel Nails"],
        "description": ("Fixture Nails is a synthetic shop used only for offline "
                        "tests. It lists manicures and pedicures and a short "
                        "paragraph so the length checks have something realistic "
                        "to measure without any real customer or business data "
                        "appearing anywhere in this file or in the report."),
        "hours": {day: {"open": "09:00", "close": "19:00"} for day in seo._WEEKDAYS},
        "phone": "+1 714-555-0100",
        "website": "https://example.invalid/fixture",
    }
    base.update(overrides)
    return base


class CompletenessTests(unittest.TestCase):
    def test_complete_profile_has_no_findings(self):
        loc = audit_location(_profile())
        self.assertEqual(loc.findings, [], _codes(loc))
        self.assertEqual(loc.score, 100)
        self.assertIsNone(loc.highest_severity)

    def test_missing_fields_are_flagged_high(self):
        loc = audit_location({})
        missing = [f.field for f in loc.findings if f.code == "SEO-MISSING-FIELD"]
        for required in seo.REQUIRED_FIELDS:
            self.assertIn(required, missing)
        self.assertTrue(all(f.severity == "high"
                            for f in loc.findings if f.code == "SEO-MISSING-FIELD"))

    def test_findings_carry_evidence_and_recommendation(self):
        loc = audit_location({})
        for finding in loc.findings:
            self.assertTrue(finding.message, finding.code)
            self.assertTrue(finding.recommendation, finding.code)

    def test_empty_mapping_field_is_flagged_like_missing(self):
        # An empty {} is falsy but is neither None, str nor list: it must still
        # be flagged as an empty required field (regression for issue #39).
        loc = audit_location(_profile(hours={}))
        missing = [f.field for f in loc.findings if f.code == "SEO-MISSING-FIELD"]
        self.assertIn("hours", missing, _codes(loc))
        self.assertTrue(all(f.severity == "high"
                            for f in loc.findings if f.code == "SEO-MISSING-FIELD"))
        self.assertEqual(loc.highest_severity, "high")

    def test_whitespace_only_list_field_is_flagged_like_missing(self):
        # Regression for issue #39: a required list field whose items are all
        # blank is empty once normalized and must be flagged exactly like [].
        for services in (["   "], [""], ["", "  "], ["\t"]):
            with self.subTest(services=services):
                loc = audit_location(_profile(services=services))
                missing = [f.field for f in loc.findings
                           if f.code == "SEO-MISSING-FIELD"]
                self.assertIn("services", missing, _codes(loc))
                self.assertTrue(all(f.severity == "high"
                                    for f in loc.findings
                                    if f.code == "SEO-MISSING-FIELD"))
                self.assertEqual(loc.highest_severity, "high")

    def test_blank_only_services_match_empty_list_behaviour(self):
        # Before the fix services=['   '] produced ZERO findings and score 100,
        # while services=[] was flagged. The two must now be indistinguishable.
        empty = audit_location(_profile(services=[]))
        for services in (["   "], [""]):
            with self.subTest(services=services):
                blank = audit_location(_profile(services=services))
                self.assertEqual(_codes(empty), _codes(blank))
                self.assertEqual(empty.score, blank.score)
                self.assertNotEqual(blank.score, 100)


class CategoryAndServiceTests(unittest.TestCase):
    def test_primary_category_off_and_suboptimal(self):
        off = audit_location(_profile(primary_category="Car Wash"))
        self.assertIn("SEO-PRIMARY-CATEGORY-OFF", _codes(off))
        sub = audit_location(_profile(primary_category="Beauty Salon"))
        self.assertIn("SEO-PRIMARY-CATEGORY-SUBOPTIMAL", _codes(sub))
        ok = audit_location(_profile(primary_category="Nail Salon"))
        self.assertNotIn("SEO-PRIMARY-CATEGORY-OFF", _codes(ok))

    def test_secondary_categories_duplicate_overlap_too_many(self):
        loc = audit_location(_profile(
            primary_category="Nail Salon",
            secondary_categories=["Manicure", "Manicure", "Nail Salon",
                                  "Pedicure", "Nail Art", "Waxing", "Day Spa"]))
        codes = _codes(loc)
        self.assertIn("SEO-SECONDARY-CATEGORIES-DUPLICATE", codes)
        self.assertIn("SEO-SECONDARY-CATEGORIES-OVERLAP-PRIMARY", codes)
        self.assertIn("SEO-SECONDARY-CATEGORIES-TOO-MANY", codes)

    def test_secondary_categories_empty(self):
        loc = audit_location(_profile(secondary_categories=[]))
        self.assertIn("SEO-SECONDARY-CATEGORIES-EMPTY", _codes(loc))

    def test_services_too_few_and_duplicate(self):
        loc = audit_location(_profile(services=["Manicure", "Manicure"]))
        codes = _codes(loc)
        self.assertIn("SEO-SERVICES-DUPLICATE", codes)
        self.assertIn("SEO-SERVICES-TOO-FEW", codes)

    def test_services_non_list_is_malformed_not_crash(self):
        loc = audit_location(_profile(services=1234))
        self.assertIn("SEO-PROFILE-MALFORMED", _codes(loc))


class DescriptionTests(unittest.TestCase):
    def test_too_short(self):
        loc = audit_location(_profile(description="Great nails."))
        self.assertIn("SEO-DESCRIPTION-TOO-SHORT", _codes(loc))

    def test_too_long(self):
        loc = audit_location(_profile(description="Nails. " * 200))
        self.assertIn("SEO-DESCRIPTION-TOO-LONG", _codes(loc))

    def test_phone_and_url_are_flagged(self):
        loc = audit_location(_profile(
            description=("Come to our shop today and call 714-555-0100 or see "
                         "https://example.invalid for details and prices. " * 3)))
        codes = _codes(loc)
        self.assertIn("SEO-DESCRIPTION-PHONE", codes)
        self.assertIn("SEO-DESCRIPTION-URL", codes)

    def test_all_caps(self):
        loc = audit_location(_profile(
            description="WELCOME TO OUR NAIL SALON WE DO GREAT NAILS ALL DAY "
                        "FOR EVERYONE AND EVERY OCCASION COME VISIT US SOON"))
        self.assertIn("SEO-DESCRIPTION-ALL-CAPS", _codes(loc))

    def test_keyword_stuffing(self):
        loc = audit_location(_profile(
            description=("manicure " * 40) + "and pedicure for everyone in town"))
        self.assertIn("SEO-DESCRIPTION-KEYWORD-STUFFING", _codes(loc))

    def test_natural_prose_is_not_stuffing(self):
        loc = audit_location(_profile())
        self.assertNotIn("SEO-DESCRIPTION-KEYWORD-STUFFING", _codes(loc))


class HoursTests(unittest.TestCase):
    def test_invalid_range_and_missing_days(self):
        loc = audit_location(_profile(hours={"monday": {"open": "19:00",
                                                        "close": "09:00"}}))
        codes = _codes(loc)
        self.assertIn("SEO-HOURS-INVALID-RANGE", codes)
        self.assertIn("SEO-HOURS-DAY-MISSING", codes)

    def test_malformed_time_and_shape(self):
        loc = audit_location(_profile(hours={
            "monday": {"open": "25:99", "close": "18:00"},
            "tuesday": "9 to 5"}))
        codes = _codes(loc)
        self.assertIn("SEO-HOURS-MALFORMED", codes)

    def test_hours_not_a_mapping(self):
        loc = audit_location(_profile(hours=["09:00-19:00"]))
        self.assertIn("SEO-HOURS-MALFORMED", _codes(loc))

    def test_all_closed(self):
        loc = audit_location(_profile(
            hours={day: {"open": "09:00", "close": "19:00"} for day in seo._WEEKDAYS}
            | {"monday": "closed", "tuesday": "closed", "wednesday": "closed",
               "thursday": "closed", "friday": "closed", "saturday": "closed",
               "sunday": "closed"}))
        self.assertIn("SEO-HOURS-ALL-CLOSED", _codes(loc))


class IdentityTests(unittest.TestCase):
    def test_same_name_distinct_zip_stays_separate(self):
        profiles = [
            _profile(location_name="accounts/1001/locations/loc-001",
                     title="Demo Nails", postal_code="92841"),
            _profile(location_name="accounts/1001/locations/loc-002",
                     title="Demo Nails", postal_code="92683"),
        ]
        report = audit_profiles(profiles)
        self.assertEqual(len(report.locations), 2)
        by_name = report.duplicate_names["demo nails"]
        self.assertEqual(by_name, ["accounts/1001/locations/loc-001",
                                   "accounts/1001/locations/loc-002"])
        for loc in report.locations:
            self.assertIn("SEO-IDENTITY-REUSED-NAME", _codes(loc))
            self.assertNotIn("SEO-IDENTITY-DUPLICATE-NAME-ZIP", _codes(loc))
        # identities are still distinct -> separate resources
        self.assertEqual(len({loc.location_name for loc in report.locations}), 2)

    def test_same_name_and_zip_is_a_probable_duplicate(self):
        profiles = [
            _profile(location_name="accounts/1001/locations/loc-001",
                     title="Demo Nails", postal_code="92841"),
            _profile(location_name="accounts/1001/locations/loc-003",
                     title="Demo Nails", postal_code="92841"),
        ]
        report = audit_profiles(profiles)
        for loc in report.locations:
            self.assertIn("SEO-IDENTITY-DUPLICATE-NAME-ZIP", _codes(loc))
            self.assertEqual(loc.highest_severity, "high")


class RobustnessTests(unittest.TestCase):
    def test_empty_and_malformed_fixtures(self):
        report = audit_profiles([{}, None, "not-a-profile", 42])
        self.assertEqual(len(report.locations), 1)          # only {} is a mapping
        self.assertEqual(len(report.skipped), 3)
        self.assertEqual(report.locations[0].location_name, "")
        for entry in report.skipped:
            self.assertIn("reason", entry)

    def test_non_mapping_profile_raises_for_single_audit(self):
        with self.assertRaises(ValueError):
            audit_location("nope")  # type: ignore[arg-type]

    def test_deterministic_json(self):
        first = audit_profiles(load_profiles()).to_json()
        second = audit_profiles(load_profiles()).to_json()
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), json.loads(second))

    def test_actions_are_prioritised(self):
        loc = audit_location(_profile(primary_category="Car Wash",
                                      description="short"))
        ranks = [seo.severity_rank(f.severity) for f in loc.actions]
        self.assertEqual(ranks, sorted(ranks))
        self.assertLess(loc.score, 100)

    def test_load_profiles_returns_a_deep_copy(self):
        first = load_profiles()
        first[0]["title"] = "mutated"
        self.assertNotEqual(load_profiles()[0]["title"], "mutated")

    def test_load_profiles_from_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "profiles.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"locations": [_profile()]}, handle)
            profiles = load_profiles(path)
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["title"], "Fixture Nails")


class OfflineGuardTests(unittest.TestCase):
    def test_no_network_is_attempted(self):
        original = socket.socket

        def _boom(*args, **kwargs):
            raise AssertionError("the SEO audit must never open a socket")

        socket.socket = _boom  # type: ignore[assignment]
        try:
            report = audit_profiles(load_profiles())
            self.assertEqual(len(report.locations), 2)
        finally:
            socket.socket = original

    def test_module_source_has_no_network_imports(self):
        with open(os.path.join(_ROOT, "src", "gbp", "seo", "audit.py"),
                  "r", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("import socket", "import urllib", "import requests",
                          "http.client", "import ssl"):
            self.assertNotIn(forbidden, source)

    def test_report_is_labelled_offline_and_makes_no_ranking_claim(self):
        report = json.loads(audit_profiles(load_profiles()).to_json())
        self.assertTrue(report["fixture_only"])
        self.assertEqual(report["source"], "synthetic-fixture")
        disclaimer = report["disclaimer"].lower()
        self.assertIn("no live google", disclaimer)
        self.assertIn("no ranking improvement", disclaimer)
        for loc in report["locations"]:
            for finding in loc["findings"]:
                text = (finding["message"] + " " + finding["recommendation"]).lower()
                self.assertNotIn("guarantee", text.replace("no ranking outcome is guaranteed", ""))
                self.assertNotIn("#1", text)


if __name__ == "__main__":
    unittest.main()
