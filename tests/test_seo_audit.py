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
                        "tests. It lists manicures, pedicures and gel nails and "
                        "a short paragraph so the length checks have something "
                        "realistic to measure without any real customer or "
                        "business data appearing anywhere in this file or in the "
                        "report."),
        "hours": {day: {"open": "09:00", "close": "19:00"} for day in seo._WEEKDAYS},
        "phone": "+1 714-555-0100",
        "website": "https://example.invalid/fixture",
        # A complete shop carries photos (issue #107); tests override as needed.
        "photos": {"total": 24, "exterior": 8, "interior": 8, "work": 8},
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


class DescriptionServicesCoherenceTests(unittest.TestCase):
    """Issue #86: deterministic description <-> services cross-reference."""

    def test_listed_service_absent_from_description_is_flagged(self):
        loc = audit_location(_profile(
            services=["Manicure", "Pedicure", "Acrylic Nails"]))
        findings = [f for f in loc.findings
                    if f.code == "SEO-SERVICES-NOT-IN-DESCRIPTION"]
        self.assertEqual(len(findings), 1, _codes(loc))
        self.assertEqual(findings[0].severity, "low")
        self.assertIn("'Acrylic Nails'", findings[0].evidence)

    def test_described_service_absent_from_list_is_flagged(self):
        loc = audit_location(_profile(
            services=["Manicure"],
            description=_profile()["description"] + " We also mention nail art."))
        findings = [f for f in loc.findings
                    if f.code == "SEO-DESCRIPTION-SERVICE-NOT-LISTED"]
        self.assertTrue(findings, _codes(loc))
        # every finding quotes the matched vocabulary term it is about
        for f in findings:
            self.assertIn("description term=", f.evidence)
        self.assertTrue(any("'nail art'" in f.evidence for f in findings),
                        [f.evidence for f in findings])

    def test_coherent_profile_produces_no_coherence_findings(self):
        codes = set(_codes(audit_location(_profile())))
        self.assertNotIn("SEO-SERVICES-NOT-IN-DESCRIPTION", codes)
        self.assertNotIn("SEO-DESCRIPTION-SERVICE-NOT-LISTED", codes)

    def test_check_is_skipped_when_one_side_is_missing(self):
        # A missing services list or description belongs to the completeness
        # check, not to the cross-reference.
        for overrides in ({"services": []}, {"description": ""}):
            with self.subTest(overrides=overrides):
                codes = _codes(audit_location(_profile(**overrides)))
                self.assertNotIn("SEO-SERVICES-NOT-IN-DESCRIPTION", codes)
                self.assertNotIn("SEO-DESCRIPTION-SERVICE-NOT-LISTED", codes)

    def test_plural_forms_match(self):
        # "manicures" in the description matches the listed "Manicure".
        loc = audit_location(_profile(services=["Manicure"],
                                      description="We do manicures all week."))
        self.assertNotIn("SEO-SERVICES-NOT-IN-DESCRIPTION", _codes(loc))

    def test_coherence_is_order_independent(self):
        services = ["Acrylic Nails", "Dip Powder", "Shellac"]
        loc_a = audit_location(_profile(services=list(services),
                                        description="manicures and pedicures"))
        loc_b = audit_location(_profile(services=list(reversed(services)),
                                        description="manicures and pedicures"))
        self.assertEqual(loc_a.findings, loc_b.findings)

    def test_wording_never_asserts_what_the_business_offers(self):
        loc = audit_location(_profile(
            services=["Manicure"],
            description="We mention nail art and shellac."))
        coherence = [f for f in loc.findings
                     if f.code in ("SEO-SERVICES-NOT-IN-DESCRIPTION",
                                   "SEO-DESCRIPTION-SERVICE-NOT-LISTED")]
        self.assertTrue(coherence, _codes(loc))
        # The finding reports the cross-reference only; it never claims the shop
        # offers/provides/serves the term.
        for f in coherence:
            text = f"{f.message} {f.evidence} {f.recommendation}".lower()
            for phrase in (" offers", " provides", " serves", " our shop has",
                           " the shop offers"):
                self.assertNotIn(phrase, text, f.code)

    def test_only_the_builtin_vocabulary_is_used(self):
        # An unrelated word is not a known nail service and is never invented.
        loc = audit_location(_profile(services=["Manicure"],
                                      description="We sell gift cards and shampoo."))
        self.assertNotIn("SEO-DESCRIPTION-SERVICE-NOT-LISTED", _codes(loc))


class HoursTests(unittest.TestCase):
    def test_overnight_and_missing_days(self):
        # Issue #105: 19:00-09:00 crosses midnight, so it is the low-severity
        # SEO-HOURS-OVERNIGHT note, not an invalid-range error.
        loc = audit_location(_profile(hours={"monday": {"open": "19:00",
                                                        "close": "09:00"}}))
        codes = _codes(loc)
        self.assertIn("SEO-HOURS-OVERNIGHT", codes)
        self.assertNotIn("SEO-HOURS-INVALID-RANGE", codes)
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


class MidnightCloseTests(unittest.TestCase):
    """Issue #98: a 00:00 close is midnight at the end of the day, not 00:00."""

    def test_one_day_closing_at_midnight_scores_100(self):
        # A perfect profile where a single day closes at midnight must not be
        # flagged SEO-HOURS-INVALID-RANGE (previously scored 93, not 100).
        hours = {day: {"open": "09:00", "close": "19:00"} for day in seo._WEEKDAYS}
        hours["saturday"] = {"open": "10:00", "close": "00:00"}
        loc = audit_location(_profile(hours=hours))
        self.assertEqual(loc.findings, [], _codes(loc))
        self.assertNotIn("SEO-HOURS-INVALID-RANGE", _codes(loc))
        self.assertEqual(loc.score, 100)

    def test_all_seven_days_closing_at_midnight_score_100(self):
        hours = {day: {"open": "09:00", "close": "00:00"} for day in seo._WEEKDAYS}
        loc = audit_location(_profile(hours=hours))
        self.assertEqual(loc.findings, [], _codes(loc))
        self.assertEqual(loc.score, 100)

    def test_open_at_0001_and_close_at_midnight_is_valid(self):
        # start > 0 is all the guard needs: 00:01-00:00 is a 23h59m window.
        hours = {day: {"open": "09:00", "close": "00:00"} for day in seo._WEEKDAYS}
        hours["wednesday"] = {"open": "00:01", "close": "00:00"}
        loc = audit_location(_profile(hours=hours))
        self.assertEqual(loc.findings, [], _codes(loc))
        self.assertEqual(loc.score, 100)

    def test_inverted_range_is_now_an_overnight_note(self):
        # Issue #105: a start > end span is overnight, no longer invalid-range.
        hours = {day: {"open": "09:00", "close": "19:00"} for day in seo._WEEKDAYS}
        hours["monday"] = {"open": "19:00", "close": "09:00"}
        loc = audit_location(_profile(hours=hours))
        codes = _codes(loc)
        self.assertIn("SEO-HOURS-OVERNIGHT", codes)
        self.assertNotIn("SEO-HOURS-INVALID-RANGE", codes)

    def test_open_equals_close_still_flagged(self):
        hours = {day: {"open": "09:00", "close": "19:00"} for day in seo._WEEKDAYS}
        hours["monday"] = {"open": "09:00", "close": "09:00"}
        loc = audit_location(_profile(hours=hours))
        self.assertIn("SEO-HOURS-INVALID-RANGE", _codes(loc))

    def test_midnight_to_midnight_still_flagged(self):
        # open == 00:00 is NOT "after midnight": the rewrite must not fire.
        hours = {day: {"open": "09:00", "close": "19:00"} for day in seo._WEEKDAYS}
        hours["monday"] = {"open": "00:00", "close": "00:00"}
        loc = audit_location(_profile(hours=hours))
        self.assertIn("SEO-HOURS-INVALID-RANGE", _codes(loc))

    def test_general_overnight_window_is_a_note(self):
        # Issue #105 (owner decision): a 22:00-02:00 span crosses midnight and is
        # now a low-severity note to verify, not an invalid-range error.
        hours = {day: {"open": "09:00", "close": "19:00"} for day in seo._WEEKDAYS}
        hours["monday"] = {"open": "22:00", "close": "02:00"}
        loc = audit_location(_profile(hours=hours))
        codes = _codes(loc)
        self.assertIn("SEO-HOURS-OVERNIGHT", codes)
        self.assertNotIn("SEO-HOURS-INVALID-RANGE", codes)

    def test_malformed_close_is_unchanged(self):
        hours = {day: {"open": "09:00", "close": "19:00"} for day in seo._WEEKDAYS}
        hours["monday"] = {"open": "09:00", "close": "00:0"}
        loc = audit_location(_profile(hours=hours))
        codes = _codes(loc)
        self.assertIn("SEO-HOURS-MALFORMED", codes)
        self.assertNotIn("SEO-HOURS-INVALID-RANGE", codes)


class OvernightHoursTests(unittest.TestCase):
    """Issue #105: a span that crosses midnight is a low-severity note, not an
    invalid-range error, and never lowers the completeness score."""

    def _overnight_profile(self, open_, close):
        hours = {day: {"open": "09:00", "close": "19:00"} for day in seo._WEEKDAYS}
        hours["monday"] = {"open": open_, "close": close}
        return _profile(hours=hours)

    def test_22_00_to_02_00_is_a_low_severity_note(self):
        loc = audit_location(self._overnight_profile("22:00", "02:00"))
        overnight = [f for f in loc.findings if f.code == "SEO-HOURS-OVERNIGHT"]
        self.assertEqual(len(overnight), 1, _codes(loc))
        self.assertEqual(overnight[0].severity, "low")
        self.assertNotIn("SEO-HOURS-INVALID-RANGE", _codes(loc))

    def test_overnight_note_does_not_change_the_score(self):
        # Same profile, one day overnight vs a normal day: identical score,
        # because SEO-HOURS-OVERNIGHT is informational (non-scoring).
        baseline = audit_location(_profile())
        overnight = audit_location(self._overnight_profile("22:00", "02:00"))
        self.assertEqual(baseline.score, overnight.score)
        self.assertEqual(overnight.score, 100)

    def test_19_00_to_09_00_is_overnight(self):
        codes = _codes(audit_location(self._overnight_profile("19:00", "09:00")))
        self.assertIn("SEO-HOURS-OVERNIGHT", codes)
        self.assertNotIn("SEO-HOURS-INVALID-RANGE", codes)

    def test_09_00_to_midnight_close_has_no_finding(self):
        # Issue #98 stays untouched: a 00:00 close is 24:00, so this is valid.
        loc = audit_location(self._overnight_profile("09:00", "00:00"))
        self.assertEqual(loc.findings, [], _codes(loc))
        self.assertEqual(loc.score, 100)

    def test_00_00_to_00_00_stays_invalid_range(self):
        # start == end is not overnight: it is a zero-length range.
        loc = audit_location(self._overnight_profile("00:00", "00:00"))
        codes = _codes(loc)
        self.assertIn("SEO-HOURS-INVALID-RANGE", codes)
        self.assertNotIn("SEO-HOURS-OVERNIGHT", codes)

    def test_garbage_time_stays_malformed(self):
        loc = audit_location(self._overnight_profile("22:00", "not-a-time"))
        codes = _codes(loc)
        self.assertIn("SEO-HOURS-MALFORMED", codes)
        self.assertNotIn("SEO-HOURS-OVERNIGHT", codes)
        self.assertNotIn("SEO-HOURS-INVALID-RANGE", codes)


class PhotoTests(unittest.TestCase):
    """Issue #107: photo checks (new check group, field ``photos``)."""

    def test_photos_absent_is_a_medium_finding(self):
        profile = _profile()
        del profile["photos"]
        loc = audit_location(profile)
        finding = [f for f in loc.findings if f.code == "SEO-PHOTOS-NONE"]
        self.assertEqual(len(finding), 1, _codes(loc))
        self.assertEqual(finding[0].severity, "medium")
        self.assertEqual(finding[0].field, "photos")
        self.assertTrue(finding[0].recommendation)
        self.assertEqual(loc.score, 93)          # medium penalty 7, scores normally

    def test_total_zero_is_reported_like_absent(self):
        for value in (0, {"total": 0}):
            with self.subTest(photos=value):
                loc = audit_location(_profile(photos=value))
                self.assertIn("SEO-PHOTOS-NONE", _codes(loc))
                self.assertNotIn("SEO-PHOTOS-TOO-FEW", _codes(loc))

    def test_too_few_boundary(self):
        few = audit_location(_profile(photos=9))
        self.assertIn("SEO-PHOTOS-TOO-FEW", _codes(few))
        self.assertEqual(
            [f.severity for f in few.findings if f.code == "SEO-PHOTOS-TOO-FEW"],
            ["low"])
        at_floor = audit_location(_profile(photos=10))
        self.assertNotIn("SEO-PHOTOS-TOO-FEW", _codes(at_floor))
        self.assertNotIn("SEO-PHOTOS-NONE", _codes(at_floor))

    def test_integer_total_is_accepted(self):
        loc = audit_location(_profile(photos=15))
        for code in ("SEO-PHOTOS-NONE", "SEO-PHOTOS-TOO-FEW",
                     "SEO-PHOTOS-NO-EXTERIOR", "SEO-PHOTOS-NO-INTERIOR",
                     "SEO-PHOTOS-NO-WORK"):
            self.assertNotIn(code, _codes(loc))

    def test_mapping_category_counts_of_zero_are_flagged(self):
        cases = {
            "SEO-PHOTOS-NO-EXTERIOR": {"total": 20, "exterior": 0,
                                       "interior": 5, "work": 5},
            "SEO-PHOTOS-NO-INTERIOR": {"total": 20, "exterior": 5,
                                       "interior": 0, "work": 5},
            "SEO-PHOTOS-NO-WORK": {"total": 20, "exterior": 5,
                                   "interior": 5, "work": 0},
        }
        for code, photos in cases.items():
            with self.subTest(code=code):
                loc = audit_location(_profile(photos=photos))
                self.assertIn(code, _codes(loc))
                finding = [f for f in loc.findings if f.code == code][0]
                self.assertEqual(finding.severity, "low")
                self.assertIn("=0", finding.evidence)

    def test_mapping_total_derives_from_category_counts(self):
        # No explicit ``total``: the checks sum the categories, no invention.
        loc = audit_location(_profile(photos={"exterior": 4, "interior": 4,
                                              "work": 4}))
        for code in ("SEO-PHOTOS-NONE", "SEO-PHOTOS-TOO-FEW",
                     "SEO-PHOTOS-NO-EXTERIOR"):
            self.assertNotIn(code, _codes(loc))

    def test_omitted_category_is_not_judged(self):
        loc = audit_location(_profile(photos={"total": 20, "exterior": 5}))
        self.assertNotIn("SEO-PHOTOS-NO-INTERIOR", _codes(loc))
        self.assertNotIn("SEO-PHOTOS-NO-WORK", _codes(loc))

    def test_empty_mapping_is_read_as_absent(self):
        loc = audit_location(_profile(photos={}))
        self.assertIn("SEO-PHOTOS-NONE", _codes(loc))
        self.assertNotIn("SEO-PROFILE-MALFORMED", _codes(loc))

    def test_malformed_photos_never_crash(self):
        for value in ("lots", -3, [1, 2], 3.5, True,
                      {"total": "x"}, {"exterior": -1}, {"exterior": "x"},
                      {"foo": 1}):
            with self.subTest(photos=value):
                loc = audit_location(_profile(photos=value))
                self.assertIn("SEO-PROFILE-MALFORMED", _codes(loc))
                self.assertNotIn("SEO-PHOTOS-NONE", _codes(loc))

    def test_photos_is_not_a_required_field(self):
        self.assertNotIn("photos", seo.REQUIRED_FIELDS)
        # A profile with no photograph key is not flagged as a missing field.
        profile = _profile()
        del profile["photos"]
        missing = [f.field for f in audit_location(profile).findings
                   if f.code == "SEO-MISSING-FIELD"]
        self.assertNotIn("photos", missing)

    def test_photo_total_helper(self):
        self.assertEqual(seo.photo_total(_profile()), 24)
        self.assertEqual(seo.photo_total({"photos": 7}), 7)
        self.assertEqual(seo.photo_total({"photos": {"exterior": 2,
                                                     "work": 3}}), 5)
        self.assertIsNone(seo.photo_total({}))
        self.assertIsNone(seo.photo_total({"photos": "nope"}))
        self.assertIsNone(seo.photo_total("not-a-mapping"))  # type: ignore[arg-type]


class ReviewHealthTests(unittest.TestCase):
    """Issue #107: review-health checks (new check group)."""

    _REVIEW_CODES = ("SEO-REVIEWS-LOW-RATING", "SEO-REVIEWS-TOO-FEW",
                     "SEO-REVIEWS-UNANSWERED")

    def test_absent_review_fields_are_silent(self):
        loc = audit_location(_profile())
        for code in self._REVIEW_CODES:
            self.assertNotIn(code, _codes(loc))
        self.assertEqual(loc.score, 100)

    def test_explicit_nulls_are_silent(self):
        loc = audit_location(_profile(rating=None, review_count=None,
                                      unanswered_reviews=None))
        for code in self._REVIEW_CODES:
            self.assertNotIn(code, _codes(loc))
        self.assertEqual(loc.score, 100)

    def test_low_rating_is_medium(self):
        finding = [f for f in audit_location(_profile(rating=3.9)).findings
                   if f.code == "SEO-REVIEWS-LOW-RATING"]
        self.assertEqual(len(finding), 1)
        self.assertEqual(finding[0].severity, "medium")
        self.assertEqual(finding[0].field, "rating")

    def test_rating_at_or_above_the_floor_is_not_flagged(self):
        for value in (4.0, 4.6, 5):
            with self.subTest(rating=value):
                loc = audit_location(_profile(rating=value))
                self.assertNotIn("SEO-REVIEWS-LOW-RATING", _codes(loc))

    def test_too_few_reviews_is_low(self):
        finding = [f for f in audit_location(_profile(review_count=19)).findings
                   if f.code == "SEO-REVIEWS-TOO-FEW"]
        self.assertEqual(len(finding), 1)
        self.assertEqual(finding[0].severity, "low")
        self.assertNotIn("SEO-REVIEWS-TOO-FEW",
                         _codes(audit_location(_profile(review_count=20))))

    def test_unanswered_severity_split(self):
        low_codes = {1, 9}
        for value in low_codes:
            finding = [f for f in audit_location(
                _profile(unanswered_reviews=value)).findings
                if f.code == "SEO-REVIEWS-UNANSWERED"]
            self.assertEqual(finding[0].severity, "low", value)
        finding = [f for f in audit_location(
            _profile(unanswered_reviews=10)).findings
            if f.code == "SEO-REVIEWS-UNANSWERED"]
        self.assertEqual(finding[0].severity, "medium")
        self.assertNotIn("SEO-REVIEWS-UNANSWERED",
                         _codes(audit_location(_profile(unanswered_reviews=0))))

    def test_malformed_review_data_never_crashes(self):
        cases = ({"rating": "abc"}, {"rating": {}}, {"rating": [1]},
                 {"review_count": "many"}, {"review_count": 1.5},
                 {"unanswered_reviews": -1}, {"unanswered_reviews": {}})
        for overrides in cases:
            with self.subTest(overrides=overrides):
                loc = audit_location(_profile(**overrides))
                self.assertIn("SEO-PROFILE-MALFORMED", _codes(loc))
                for code in self._REVIEW_CODES:
                    self.assertNotIn(code, _codes(loc))

    def test_review_findings_score_normally(self):
        loc = audit_location(_profile(rating=3.0, review_count=5,
                                      unanswered_reviews=12))
        codes = _codes(loc)
        self.assertIn("SEO-REVIEWS-LOW-RATING", codes)   # medium 7
        self.assertIn("SEO-REVIEWS-TOO-FEW", codes)      # low 3
        self.assertIn("SEO-REVIEWS-UNANSWERED", codes)   # medium 7
        self.assertEqual(loc.score, 100 - 7 - 3 - 7)
        for code in self._REVIEW_CODES:
            self.assertNotIn(code, seo._NON_SCORING_CODES)


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

    def test_reused_name_is_reported_but_does_not_lower_score(self):
        # Repo-owner decision (issue #39): a chain reusing its name across
        # locations with distinct ZIPs is normal. The note stays in the report
        # and in the actions list, but a complete profile still scores 100.
        profiles = [
            _profile(location_name=f"accounts/1001/locations/loc-{i:03d}",
                     title="Demo Nails", postal_code=zipcode)
            for i, zipcode in enumerate(("92841", "92683", "92703"), start=1)
        ]
        report = audit_profiles(profiles)
        self.assertEqual(len(report.locations), 3)
        for loc in report.locations:
            self.assertIn("SEO-IDENTITY-REUSED-NAME", _codes(loc))
            self.assertNotIn("SEO-IDENTITY-DUPLICATE-NAME-ZIP", _codes(loc))
            self.assertEqual(loc.score, 100, _codes(loc))
            # still surfaced as an action, just not a scored one
            self.assertIn("SEO-IDENTITY-REUSED-NAME",
                          [f.code for f in loc.actions])

    def test_duplicate_name_and_zip_still_lowers_score(self):
        # The probable-duplicate finding keeps its high-severity penalty.
        profiles = [
            _profile(location_name="accounts/1001/locations/loc-001",
                     title="Demo Nails", postal_code="92841"),
            _profile(location_name="accounts/1001/locations/loc-003",
                     title="Demo Nails", postal_code="92841"),
        ]
        report = audit_profiles(profiles)
        for loc in report.locations:
            self.assertIn("SEO-IDENTITY-DUPLICATE-NAME-ZIP", _codes(loc))
            self.assertLess(loc.score, 100)


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
