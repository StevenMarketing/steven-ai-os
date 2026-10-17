"""Offline fixture-driven tests for the Business Dashboard (issue #30).

Standard library only, NO network. The "behaviour" tests parse the REAL strict
JSON fixture out of ``web/app.js`` (between the BEGIN/END_BUSINESS_FIXTURE
markers) and apply the same name-or-ZIP matching rule the UI documents, so the
tests fail if the shipped fixture or the search/readiness rule drifts.

Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_business_dashboard -v
"""

from __future__ import annotations

import json
import os
import re
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WEB_HTML = os.path.join(_ROOT, "web", "index.html")
WEB_JS = os.path.join(_ROOT, "web", "app.js")
WEB_CSS = os.path.join(_ROOT, "web", "styles.css")

_FIXTURE_RE = re.compile(
    r"// BEGIN_BUSINESS_FIXTURE(.*?)// END_BUSINESS_FIXTURE", re.S
)

READINESS_VALUES = {"ready", "needs_connection", "not_connected"}


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def load_fixture() -> dict:
    """Parse the strict-JSON fixture literal embedded in ``web/app.js``."""
    js = _read(WEB_JS)
    m = _FIXTURE_RE.search(js)
    if not m:
        raise AssertionError("BEGIN/END_BUSINESS_FIXTURE markers not found in app.js")
    block = m.group(1)
    start = block.index("{")
    text = block[start:].strip()
    text = re.sub(r";\s*$", "", text)
    return json.loads(text)


def business_matches(biz: dict, query: str) -> bool:
    """Mirror of ``businessMatches`` in app.js: search name OR ZIP."""
    needle = (query or "").strip().lower()
    if not needle:
        return True
    return needle in (biz.get("name") or "").lower() or \
        needle in (biz.get("zip") or "").lower()


def filter_businesses(businesses, query="", readiness=""):
    rows = []
    for biz in businesses:
        if readiness and biz.get("readiness") != readiness:
            continue
        if business_matches(biz, query):
            rows.append(biz)
    return rows


class BusinessDashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.html = _read(WEB_HTML)
        self.js = _read(WEB_JS)
        self.css = _read(WEB_CSS)
        self.fixture = load_fixture()
        self.businesses = self.fixture["businesses"]

    # ------------------------------------------------------------------ #
    # navigation + markup
    # ------------------------------------------------------------------ #
    def test_area_and_nav_entry_present(self):
        self.assertIn('id="area-business"', self.html)
        self.assertIn('data-area="business"', self.html)
        self.assertIn("Business Dashboard", self.html)
        # reachable through the AREAS dispatch + the browser history alias
        self.assertIn('business:    { section: "business" }', self.js)
        self.assertIn('"business-dashboard": "business"', self.js)
        self.assertIn('cfg.section === "business"', self.js)

    def test_search_and_readiness_controls_present(self):
        for marker in ('id="biz-q"', 'id="biz-search"', 'id="biz-reset"',
                       'id="biz-readiness"', 'id="business-table"',
                       'id="business-reviews"', 'id="business-reviews-title"'):
            self.assertIn(marker, self.html, msg=marker)
        # the readiness filter offers the three fixture statuses + Any
        self.assertIn('<option value="">Any status</option>', self.html)
        for value in sorted(READINESS_VALUES):
            self.assertIn('value="%s"' % value, self.html, msg=value)
        self.assertIn("Search by business name or ZIP", self.html)

    def test_fixture_badge_present_and_visible(self):
        self.assertIn('id="business-fixture-badge"', self.html)
        self.assertIn("FIXTURE", self.html)
        self.assertIn("FIXTURE DATA — synthetic sample businesses, not read from Google.",
                      self.html)
        self.assertIn(".fixture-badge", self.css)

    # ------------------------------------------------------------------ #
    # fixture data
    # ------------------------------------------------------------------ #
    def test_fixture_carries_required_columns(self):
        self.assertGreaterEqual(len(self.businesses), 2)
        for biz in self.businesses:
            for key in ("location_id", "name", "zip", "address", "readiness", "reviews"):
                self.assertIn(key, biz, msg=f"{biz} missing {key}")
            self.assertRegex(biz["zip"], r"^\d{5}$", msg=biz["zip"])
            self.assertIn(biz["readiness"], READINESS_VALUES, msg=biz["readiness"])
            self.assertTrue(biz["location_id"].startswith("locations/"))
            self.assertIsInstance(biz["reviews"], list)

    def test_fixture_never_exceeds_five_businesses(self):
        self.assertLessEqual(len(self.businesses), 5)
        # ...and the UI slices to the same hard cap.
        self.assertIn("BUSINESS_MAX = 5", self.js)
        self.assertIn(".slice(0, BUSINESS_MAX)", self.js)

    def test_duplicate_name_is_distinguished_by_zip(self):
        by_name = {}
        for biz in self.businesses:
            by_name.setdefault(biz["name"], []).append(biz["zip"])
        dup_names = {n: z for n, z in by_name.items() if len(z) > 1}
        self.assertTrue(dup_names, "fixture must contain a duplicate business name")
        for name, zips in dup_names.items():
            self.assertEqual(len(set(zips)), len(zips),
                             msg=f"{name} duplicates must differ by ZIP")
        # the same-name pair resolves to distinct, selectable location IDs
        name = next(iter(dup_names))
        ids = {b["location_id"] for b in self.businesses if b["name"] == name}
        self.assertEqual(len(ids), len(dup_names[name]))

    # ------------------------------------------------------------------ #
    # search / filter / selection behaviour (fixture-driven)
    # ------------------------------------------------------------------ #
    def test_search_by_name_returns_same_name_twins(self):
        rows = filter_businesses(self.businesses, query="luxe")
        self.assertEqual(len(rows), 2, msg=[r["name"] for r in rows])
        self.assertEqual({r["zip"] for r in rows}, {"92843", "92683"})

    def test_search_by_zip_picks_exactly_one_twin(self):
        rows = filter_businesses(self.businesses, query="92843")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Luxe Nails & Spa")
        self.assertEqual(rows[0]["zip"], "92843")
        other = filter_businesses(self.businesses, query="92683")
        self.assertEqual([r["location_id"] for r in other],
                         ["locations/fx-102"])

    def test_search_is_case_insensitive_and_empty_returns_all(self):
        self.assertEqual(
            [b["location_id"] for b in filter_businesses(self.businesses, query="BLOOM")],
            ["locations/fx-103"],
        )
        self.assertEqual(len(filter_businesses(self.businesses)), len(self.businesses))

    def test_readiness_filter_selects_subset(self):
        ready = filter_businesses(self.businesses, readiness="ready")
        self.assertTrue(ready)
        self.assertTrue(all(b["readiness"] == "ready" for b in ready))
        self.assertEqual(
            filter_businesses(self.businesses, readiness="not_connected"),
            [b for b in self.businesses if b["readiness"] == "not_connected"],
        )
        # an unknown readiness matches nothing -> the no-match state
        self.assertEqual(filter_businesses(self.businesses, readiness="bogus"), [])

    def test_no_match_state_reachable(self):
        self.assertEqual(filter_businesses(self.businesses, query="zzz-no-such-shop"), [])
        self.assertIn('noMatch: "No businesses match this search or filter."', self.js)
        self.assertIn('empty: "No fixture businesses available."', self.js)

    def test_selection_shows_fixture_reviews_with_empty_state(self):
        # every business can be selected by its location id and has a review list
        for biz in self.businesses:
            self.assertEqual(
                [b["location_id"] for b in self.businesses
                 if b["location_id"] == biz["location_id"]],
                [biz["location_id"]],
            )
        # at least one fixture business has reviews and one has none (empty state)
        self.assertTrue(any(b["reviews"] for b in self.businesses))
        self.assertTrue(any(not b["reviews"] for b in self.businesses))
        self.assertIn('noReviews: "No fixture reviews for this business."', self.js)
        self.assertIn("data-biz-select", self.js)
        self.assertIn("selectBusiness", self.js)

    # ------------------------------------------------------------------ #
    # states + safety
    # ------------------------------------------------------------------ #
    def test_standard_states_are_wired(self):
        self.assertIn("renderLoading(table)", self.js)
        self.assertIn('$("business-table")', self.js)
        self.assertIn("renderEmpty(wrap, filtered ? t(\"business.noMatch\")", self.js)

    def test_status_labels_never_claim_a_live_connection(self):
        self.assertIn('not_connected: "Not connected"', self.js)
        self.assertIn('needs_connection: "Needs connection"', self.js)
        self.assertIn('ready: "Ready"', self.js)
        # honest banner text, no fake "connected to Google" claim
        self.assertIn("No Google account is connected from this page", self.html)
        lowered = self.html.lower()
        self.assertNotIn("gbp is connected", lowered)
        self.assertNotIn("connected to google", lowered)

    def test_business_module_is_offline(self):
        start = self.js.index("// --- Business Dashboard (offline FIXTURE data only)")
        end = self.js.index("// --- Google Connection (offline readiness + FIXTURE preview)")
        module = self.js[start:end]
        self.assertNotIn("api(", module)              # no network call at all
        self.assertNotIn("fetch(", module)
        self.assertNotIn("/api/", module)
        self.assertNotIn("publish(", module.lower())
        self.assertNotIn("connect(", module.lower())
        self.assertNotIn("oauth", module.lower())
        self.assertIn("never calls Google", module)

    def test_existing_google_preview_and_nav_preserved(self):
        # the previous Google Connection fixture preview must still be intact
        self.assertIn('id="area-google"', self.html)
        self.assertIn('id="google-preview-locations"', self.html)
        self.assertIn("GOOGLE_PREVIEW_FIXTURE", self.js)
        # primary nav keeps exactly its 8 tabs (Business Dashboard is a nav-sub)
        self.assertEqual(self.html.count('class="tab"'), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
