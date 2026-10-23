"""Issue #42 — GMap SEO dashboard: offline per-salon audit preview.

Covers the three layers the preview is built from:

* ``src/gbp/seo/preview.py`` — the deterministic view model that reuses the
  issue #39 audit engine (no audit logic is re-implemented);
* ``GET /api/seo/audit`` — the read-only server endpoint;
* ``web/`` — the dashboard surface (markup, routing, escaping, offline banner).

Standard library only, synthetic fixtures only: no network, no Google API, no
OAuth, no publish, no paid service, no Local Rank Grid. The server binds
127.0.0.1 on an ephemeral port for the HTTP checks. Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_seo_dashboard -v
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import create_server  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.seo.audit import (  # noqa: E402
    ALLOWED_PRIMARY_CATEGORIES,
    REQUIRED_FIELDS,
    audit_profiles,
    load_profiles,
    severity_rank,
)
from gbp.seo.preview import (  # noqa: E402
    BANNER,
    CATEGORY_FIELDS,
    DATASET_NAMES,
    DemoError,
    PreviewError,
    SCORE_LABEL,
    SERVICE_FIELDS,
    build_preview,
)
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
WEB_HTML = os.path.join(_ROOT, "web", "index.html")
WEB_JS = os.path.join(_ROOT, "web", "app.js")
WEB_CSS = os.path.join(_ROOT, "web", "styles.css")

#: The exact score label every surface must use.
EXPECTED_SCORE_LABEL = "completeness score (heuristic)"

#: app.js module boundaries for the SEO Audit block.
JS_SEO_START = "// --- SEO Audit (READ-ONLY offline audit preview, issue #42)"
JS_SEO_END = "var selected = {};"


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _seo_module(js: str) -> str:
    start = js.index(JS_SEO_START)
    return js[start:js.index(JS_SEO_END, start)]


def _complete_profile(index: int, zipcode: str) -> dict:
    """A 100%-complete synthetic profile (mirrors tests/test_seo_audit.py)."""
    return {
        "location_name": f"accounts/1001/locations/loc-{index:03d}",
        "title": "Chain Nails",
        "city": "Garden Grove", "state": "CA", "postal_code": zipcode,
        "primary_category": "Nail Salon",
        "secondary_categories": ["Manicure", "Pedicure"],
        "services": ["Manicure", "Pedicure", "Gel Nails"],
        "description": ("Chain Nails is a synthetic shop in Garden Grove used "
                        "only by offline tests. It lists manicures, pedicures "
                        "and gel nails in a short paragraph so the length checks "
                        "have realistic text without any real business or "
                        "customer data, and it is long enough to satisfy the "
                        "recommended minimum description length that the audit "
                        "engine measures."),
        "hours": {day: {"open": "09:00", "close": "19:00"}
                  for day in ("monday", "tuesday", "wednesday", "thursday",
                              "friday", "saturday", "sunday")},
        "phone": "+1 714-555-0100",
        "website": "https://example.invalid/chain",
        # A complete shop carries photos (issue #107).
        "photos": {"total": 24, "exterior": 8, "interior": 8, "work": 8},
        # Tier 2 (issue #108): a complete shop also sets attributes, a booking
        # link and (valid) special hours, so the new checks stay silent.
        "attributes": ["Walk-ins welcome", "Women-owned"],
        "booking_url": "https://example.invalid/chain/book",
        "special_hours": {"2026-12-25": {"open": "10:00", "close": "16:00"}},
    }


# --------------------------------------------------------------------------
# view model
# --------------------------------------------------------------------------
class PreviewViewModelTests(unittest.TestCase):
    def setUp(self):
        self.view = build_preview("default")
        self.engine = audit_profiles(load_profiles())

    def test_top_level_shape_and_offline_labelling(self):
        self.assertTrue(self.view["fixture_only"])
        self.assertEqual(self.view["source"], "synthetic-fixture")
        self.assertEqual(self.view["mode"], "offline-demo")
        self.assertEqual(self.view["score_label"], EXPECTED_SCORE_LABEL)
        self.assertEqual(self.view["banner"], BANNER)
        self.assertIn("no live google connectivity", self.view["banner"].lower())
        self.assertIn("no live google",
                      self.view["disclaimer"].lower())
        self.assertEqual(self.view["dataset"], "default")
        self.assertEqual(self.view["location_count"], len(self.view["salons"]))

    def test_score_is_labelled_a_heuristic_completeness_score(self):
        for salon in self.view["salons"]:
            self.assertEqual(salon["score_label"], EXPECTED_SCORE_LABEL)
            self.assertIsInstance(salon["score"], int)
            self.assertTrue(0 <= salon["score"] <= 100)
        # The label never calls itself a ranking score.
        lowered = json.dumps(self.view).lower()
        self.assertNotIn("ranking score", lowered.replace(
            "not a google ranking score", ""))

    def test_reused_name_is_informational_and_does_not_lower_the_score(self):
        # Three complete shops that share a name but have distinct ZIPs: the
        # reused-name note appears, yet every completeness score stays 100.
        view = build_preview(
            profiles=[_complete_profile(i, z)
                      for i, z in enumerate(("92841", "92683", "92703"), start=1)])
        self.assertEqual(len(view["salons"]), 3)
        for salon in view["salons"]:
            codes = [f["code"] for f in salon["findings"]]
            self.assertIn("SEO-IDENTITY-REUSED-NAME", codes)
            self.assertNotIn("SEO-IDENTITY-DUPLICATE-NAME-ZIP", codes)
            self.assertEqual(salon["score"], 100, codes)

    def test_preview_score_matches_the_engine_for_the_default_fixture(self):
        by_resource = {loc.location_name: loc for loc in self.engine.locations}
        for salon in self.view["salons"]:
            engine_loc = by_resource[salon["location_name"]]
            self.assertEqual(salon["score"], engine_loc.score)
            self.assertEqual(salon["highest_severity"], engine_loc.highest_severity)
            self.assertEqual(salon["finding_count"], len(engine_loc.findings))

    def test_categories_are_clearly_separated_from_services(self):
        for salon in self.view["salons"]:
            self.assertTrue(
                all(f["field"] in CATEGORY_FIELDS
                    for f in salon["category_findings"]), salon["location_name"])
            self.assertTrue(
                all(f["field"] in SERVICE_FIELDS
                    for f in salon["service_findings"]), salon["location_name"])
            # Every finding lands in exactly one bucket, and none is dropped.
            total = (len(salon["category_findings"])
                     + len(salon["service_findings"])
                     + len(salon["other_findings"]))
            self.assertEqual(total, len(salon["findings"]), salon["location_name"])

    def test_findings_are_prioritised_by_severity_then_code_then_field(self):
        for salon in self.view["salons"]:
            ranks = [severity_rank(f["severity"]) for f in salon["findings"]]
            self.assertEqual(ranks, sorted(ranks), salon["location_name"])
            keys = [(severity_rank(f["severity"]), f["code"], f["field"])
                    for f in salon["findings"]]
            self.assertEqual(keys, sorted(keys), salon["location_name"])
            # The engine's own ordering is preserved, not re-derived.
            engine_loc = next(loc for loc in self.engine.locations
                              if loc.location_name == salon["location_name"])
            self.assertEqual([f["code"] for f in salon["findings"]],
                             [f.code for f in engine_loc.actions])

    def test_missing_fields_are_listed_per_salon(self):
        view = build_preview("incomplete")
        self.assertEqual(len(view["salons"]), 1)
        salon = view["salons"][0]
        self.assertTrue(salon["missing_fields"])
        for name in salon["missing_fields"]:
            self.assertIn(name, REQUIRED_FIELDS)
        # A missing field is shown as a high-severity finding with an action.
        for finding in salon["findings"]:
            if finding["code"] == "SEO-MISSING-FIELD":
                self.assertEqual(finding["severity"], "high")
                self.assertTrue(finding["recommendation"])
                self.assertIn(finding["field"], salon["missing_fields"])
        self.assertLess(salon["score"], 100)

    def test_complete_fixture_reports_no_missing_field(self):
        for salon in self.view["salons"]:
            missing_as_findings = [f["field"] for f in salon["findings"]
                                   if f["code"] == "SEO-MISSING-FIELD"]
            self.assertEqual(sorted(salon["missing_fields"]),
                             sorted(missing_as_findings))

    def test_no_category_finding_is_invented_by_the_preview(self):
        # The preview must pass the engine's category findings through verbatim
        # (same codes/messages). Nothing new is suggested here.
        engine_by_resource = {loc.location_name: loc for loc in self.engine.locations}
        for salon in self.view["salons"]:
            engine_cat = [f.as_dict() for f
                          in engine_by_resource[salon["location_name"]].actions
                          if f.field in CATEGORY_FIELDS]
            self.assertEqual(salon["category_findings"], engine_cat)

    def test_category_recommendations_only_name_verified_categories(self):
        # Any category quoted in a recommendation must be one the engine's
        # verified allow-list already contains.
        allowed = {c.lower() for c in ALLOWED_PRIMARY_CATEGORIES}
        for salon in self.view["salons"]:
            for finding in salon["category_findings"]:
                text = finding["recommendation"]
                quoted = [part for part in text.split("'")[1::2]]
                for name in quoted:
                    self.assertIn(name.strip().lower(), allowed, text)

    def test_empty_dataset_reports_no_salons(self):
        view = build_preview("empty")
        self.assertEqual(view["location_count"], 0)
        self.assertEqual(view["salons"], [])
        self.assertEqual(view["total_findings"], 0)

    def test_error_dataset_raises_a_deliberate_demo_error(self):
        with self.assertRaises(DemoError):
            build_preview("error")

    def test_unknown_dataset_is_rejected(self):
        with self.assertRaises(PreviewError):
            build_preview("no-such-dataset")

    def test_datasets_are_named_and_stable(self):
        self.assertEqual(DATASET_NAMES,
                         ["default", "empty", "error", "hostile", "incomplete"])

    def test_build_is_deterministic(self):
        first = json.dumps(build_preview("default"), sort_keys=True)
        second = json.dumps(build_preview("default"), sort_keys=True)
        self.assertEqual(first, second)


class MultiLocationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.view = build_preview("default")

    def test_same_name_shops_are_disambiguated_by_zip_and_resource_id(self):
        by_name = {}
        for salon in self.view["salons"]:
            by_name.setdefault(salon["display_name"], []).append(salon)
        twins = [group for group in by_name.values() if len(group) > 1]
        self.assertTrue(twins, "the default fixture must contain a same-name pair")
        for group in twins:
            zips = {s["postal_code"] for s in group}
            resources = {s["location_name"] for s in group}
            labels = {s["identity_label"] for s in group}
            self.assertEqual(len(zips), len(group))
            self.assertEqual(len(resources), len(group))
            self.assertEqual(len(labels), len(group))
            for salon in group:
                # name + ZIP + resource id, all three, in the selector label.
                self.assertIn(salon["display_name"], salon["identity_label"])
                self.assertIn(salon["postal_code"], salon["identity_label"])
                self.assertIn(salon["location_name"], salon["identity_label"])

    def test_reused_name_is_reported_for_the_default_fixture(self):
        self.assertIn("demo nails", self.view["duplicate_names"])
        self.assertEqual(len(self.view["duplicate_names"]["demo nails"]), 2)
        for salon in self.view["salons"]:
            self.assertIn("SEO-IDENTITY-REUSED-NAME",
                          [f["code"] for f in salon["findings"]])

    def test_duplicate_name_and_zip_is_still_high_and_lowers_the_score(self):
        view = build_preview(profiles=[_complete_profile(1, "92841"),
                                       _complete_profile(2, "92841")])
        for salon in view["salons"]:
            codes = [f["code"] for f in salon["findings"]]
            self.assertIn("SEO-IDENTITY-DUPLICATE-NAME-ZIP", codes)
            self.assertEqual(salon["highest_severity"], "high")
            self.assertLess(salon["score"], 100)

    def test_identity_labels_are_unique_across_the_fixture(self):
        labels = [s["identity_label"] for s in self.view["salons"]]
        self.assertEqual(len(labels), len(set(labels)))


class SafeRenderingTests(unittest.TestCase):
    """Fixture text must be escaped: it is data, never markup."""

    def setUp(self):
        self.js = _read(WEB_JS)
        self.module = _seo_module(self.js)

    def test_hostile_fixture_carries_literal_markup_as_data(self):
        view = build_preview("hostile")
        salon = view["salons"][0]
        self.assertIn("<script>alert(1)</script>", salon["display_name"])
        blob = json.dumps(view)
        # The raw characters survive into the served JSON — the client is
        # responsible for escaping them, and never gets to treat them as markup.
        self.assertIn("<img src=x onerror=alert(2)>", blob)
        self.assertIn("<script>alert(3)</script>", blob)
        self.assertIn("<", blob)

    def test_js_module_never_uses_html_injection_sinks(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                     "document.write", "eval(", "new Function"):
            self.assertNotIn(sink, self.module, f"unsafe sink: {sink}")

    def test_js_module_renders_every_dynamic_value_as_text(self):
        # textContent (via el's `text:` option) is the only way values enter
        # the DOM here, so a `<` in fixture text can never become an element.
        for marker in ("text: finding.message",
                       "text: finding.code",
                       'text: "Evidence: " + finding.evidence',
                       'text: "Suggested action: " + finding.recommendation',
                       "text: salon.display_name",
                       "text: salon.location_name",
                       "text: seoScoreText(salon)"):
            self.assertIn(marker, self.module, marker)

    def test_no_inline_script_or_style_attributes_in_the_seo_markup(self):
        html = _read(WEB_HTML)
        start = html.index('id="area-seo"')
        section = html[start:html.index("</section>", start)]
        self.assertNotIn("<script", section.lower())
        self.assertNotIn("onclick", section.lower())
        self.assertNotIn("style=", section.lower())


class OfflineGuardTests(unittest.TestCase):
    def test_building_a_preview_never_opens_a_socket(self):
        original = socket.socket

        def _boom(*args, **kwargs):
            raise AssertionError("the SEO preview must never open a socket")

        socket.socket = _boom  # type: ignore[assignment]
        try:
            view = build_preview("default")
            self.assertEqual(len(view["salons"]), 2)
        finally:
            socket.socket = original

    def test_preview_module_has_no_network_imports(self):
        source = _read(os.path.join(_ROOT, "src", "gbp", "seo", "preview.py"))
        for forbidden in ("import socket", "import urllib", "import requests",
                          "import http", "http.client", "import ssl",
                          "googleapiclient", "import subprocess"):
            self.assertNotIn(forbidden, source)

    def test_js_module_calls_only_the_local_audit_endpoint(self):
        module = _seo_module(_read(WEB_JS))
        self.assertIn('api("GET", "/api/seo/audit', module)
        for forbidden in ("googleapis", "https://", "http://", "oauth",
                          "publish", "local rank", "rank grid"):
            self.assertNotIn(forbidden, module.lower())

    def test_preview_makes_no_ranking_claim(self):
        view = build_preview("default")
        text = (view["banner"] + " " + view["score_label"] + " "
                + view["disclaimer"]).lower()
        self.assertIn("no live google", text)
        self.assertIn("no ranking improvement", text)
        for salon in view["salons"]:
            for finding in salon["findings"]:
                blob = (finding["message"] + " " + finding["recommendation"]).lower()
                self.assertNotIn("#1", blob)
                self.assertNotIn("guarantee", blob.replace(
                    "no ranking outcome is guaranteed", ""))


# --------------------------------------------------------------------------
# web surface (static contract)
# --------------------------------------------------------------------------
class WebSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.html = _read(WEB_HTML)
        self.js = _read(WEB_JS)
        self.css = _read(WEB_CSS)

    def test_area_nav_and_routing_are_wired(self):
        self.assertIn('id="area-seo"', self.html)
        self.assertIn('data-area="seo"', self.html)
        self.assertIn("SEO Audit", self.html)
        self.assertIn('seo:         { section: "seo" }', self.js)
        self.assertIn('"seo-audit": "seo"', self.js)
        self.assertIn('cfg.section === "seo"', self.js)
        self.assertIn("renderSeoAudit", self.js)
        self.assertIn('$("area-seo").hidden', self.js)

    def test_primary_nav_still_has_exactly_eight_tabs(self):
        # SEO Audit is a nav-sub panel, so the 8 primary tabs are unchanged.
        self.assertEqual(self.html.count('class="tab"'), 8)

    def test_demo_offline_banner_is_visible_and_says_no_live_google(self):
        self.assertIn('id="seo-banner"', self.html)
        self.assertIn("DEMO / OFFLINE PREVIEW — no live Google connectivity",
                      self.html)
        self.assertIn("no live Google connectivity", self.js)
        self.assertIn(".offline-banner", self.css)
        lowered = self.html.lower()
        self.assertNotIn("connected to google", lowered)
        self.assertNotIn("gbp is connected", lowered)

    def test_business_selector_and_identity_columns_present(self):
        self.assertIn('id="seo-salon"', self.html)
        self.assertIn('id="seo-salons"', self.html)
        self.assertIn('id="seo-dataset"', self.html)
        self.assertIn('id="seo-detail-body"', self.html)
        self.assertIn("seo.colId", self.js)          # location resource id
        self.assertIn("seo.colZip", self.js)         # ZIP
        self.assertIn("seo.colName", self.js)        # business name
        self.assertIn("populateSeoSalonPicker", self.js)
        self.assertIn("data-seo-select", self.js)
        self.assertIn("selectSeoSalon", self.js)

    def test_dataset_picker_offers_the_offline_states(self):
        for value in DATASET_NAMES:
            self.assertIn('value="%s"' % value, self.html, value)
        # the score is labelled a heuristic completeness score
        self.assertIn("completeness score (heuristic)", self.js)
        self.assertIn("It is NOT a Google ranking score", self.js)

    def test_missing_fields_and_category_service_sections_are_rendered(self):
        self.assertIn("seo.missingFields", self.js)
        self.assertIn("missing-chips", self.js)
        self.assertIn("seo.categoriesTitle", self.js)
        self.assertIn("seo.servicesTitle", self.js)
        self.assertIn("category_findings", self.js)
        self.assertIn("service_findings", self.js)

    def test_empty_and_error_states_are_wired(self):
        self.assertIn("renderEmpty(wrap, t(\"seo.empty\"))", self.js)
        self.assertIn("renderErrorState($(\"seo-salons\"), err)", self.js)
        self.assertIn("renderErrorState($(\"seo-detail-body\"), err)", self.js)
        self.assertIn("renderLoading", self.js)
        self.assertIn("seo.scoreCaptionNone", self.js)

    def test_css_is_mobile_friendly(self):
        self.assertIn(".finding-list", self.css)
        self.assertIn(".sev-high", self.css)
        self.assertIn("max-width: 700px", self.css)

    def test_previous_surfaces_are_untouched(self):
        self.assertIn('id="area-business"', self.html)
        self.assertIn('id="area-google"', self.html)
        self.assertIn("BUSINESS_FIXTURE", self.js)
        self.assertIn("GOOGLE_PREVIEW_FIXTURE", self.js)


# --------------------------------------------------------------------------
# API endpoint
# --------------------------------------------------------------------------
class SeoAuditApiTests(unittest.TestCase):
    """Loopback server in mock (auth=None) mode — no external network."""

    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        self.service = ApprovalService(self.repo)
        self.server = create_server(self.service, port=0)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repo.close()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_default_report_is_offline_and_labelled(self):
        status, payload = self._get("/api/seo/audit")
        self.assertEqual(status, 200)
        self.assertTrue(payload["fixture_only"])
        self.assertEqual(payload["source"], "synthetic-fixture")
        self.assertEqual(payload["score_label"], EXPECTED_SCORE_LABEL)
        self.assertEqual(payload["score_label"], SCORE_LABEL)
        self.assertIn("no live Google connectivity", payload["banner"])
        self.assertEqual(payload["mode"], "offline-demo")
        self.assertEqual(payload["location_count"], 2)
        self.assertEqual(len(payload["salons"]), 2)
        for salon in payload["salons"]:
            self.assertEqual(salon["score_label"], EXPECTED_SCORE_LABEL)
            self.assertIn("location_name", salon)
            self.assertIn("postal_code", salon)
            self.assertIn("display_name", salon)
            self.assertIn("identity_label", salon)
            self.assertIsInstance(salon["findings"], list)
            self.assertIsInstance(salon["missing_fields"], list)

    def test_empty_state_dataset(self):
        status, payload = self._get("/api/seo/audit?dataset=empty")
        self.assertEqual(status, 200)
        self.assertEqual(payload["location_count"], 0)
        self.assertEqual(payload["salons"], [])

    def test_incomplete_state_reports_missing_fields(self):
        status, payload = self._get("/api/seo/audit?dataset=incomplete")
        self.assertEqual(status, 200)
        salon = payload["salons"][0]
        self.assertTrue(salon["missing_fields"])
        self.assertLess(salon["score"], 100)

    def test_hostile_dataset_carries_literal_markup(self):
        url = f"http://127.0.0.1:{self.port}/api/seo/audit?dataset=hostile"
        with urllib.request.urlopen(url, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))
        self.assertIn("<script>alert(1)</script>", raw)
        payload = json.loads(raw)
        self.assertEqual(payload["location_count"], 1)

    def test_error_state_dataset_returns_a_controlled_demo_error(self):
        status, payload = self._get("/api/seo/audit?dataset=error")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "demo_error")
        self.assertIn("detail", payload)

    def test_unknown_dataset_is_a_400(self):
        status, payload = self._get("/api/seo/audit?dataset=bogus")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_request")

    def test_endpoint_is_read_only(self):
        # POST is not routed: the preview can never write anything.
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/seo/audit",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
