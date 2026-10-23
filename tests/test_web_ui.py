"""Offline tests for the web management UI + static file serving (Group 4C).

Standard library only, MOCK data only -- nothing here touches the network
(the server binds 127.0.0.1 on an ephemeral port). Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import WEB_DIR, create_server  # noqa: E402
from gbp.brand import BrandLoader  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
WEB_HTML = os.path.join(_ROOT, "web", "index.html")
WEB_JS = os.path.join(_ROOT, "web", "app.js")
WEB_CSS = os.path.join(_ROOT, "web", "styles.css")

#: The 7 queue/section areas the layout must expose (plus their tab labels).
AREA_LABELS = [
    "Dashboard",
    "Locations",
    "Review Inbox",
    "AI Drafts",
    "Needs Approval",
    "Approved",
    "Rejected",
    "Audit History",
]

#: The marker strings the JS uses to disable a row's bulk-approve checkbox.
BULK_BLOCK_MARKERS = ["bulk_block_low_stars_1_2", "bulk_block_sensitive"]

#: The V2 sidebar primary nav labels, in the required display order (Group 7 / T18).
SHELL_NAV_LABELS = [
    "Overview",
    "Locations",
    "Review Inbox",
    "AI Drafts",
    "Needs Approval",
    "Approved",
    "Rejected",
    "Audit History",
]

SENSITIVE_COMMENT = "I got a serious infection after my pedicure and my foot was bleeding."
CLEAN_COMMENT = "Loved my gel manicure, thank you so much!"


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class WebUiTestCase(unittest.TestCase):
    """In-memory repo + a running loopback server for static + API checks."""

    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc1 = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-001",
            title="Demo Nails — Garden Grove", city="Garden Grove",
            state="CA", language="en",
        )
        self.loc5 = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-005",
            title="Demo Nails — Westminster", city="Westminster",
            state="CA", language="vi",
        )
        self.tmp = tempfile.TemporaryDirectory()
        brand_path = os.path.join(self.tmp.name, "brand_profiles.json")
        with open(brand_path, "w", encoding="utf-8") as fh:
            json.dump({"profiles": [
                {"location_id": "accounts/1001/locations/loc-005",
                 "business_name": "Demo Nails — Westminster", "city": "Westminster",
                 "services_json": ["Manicure"], "language": "vi", "tone": "friendly",
                 "seo_keywords_json": ["Nail Salon"], "rules_json": {},
                 "auto_publish_allowed": 1},
            ]}, fh, ensure_ascii=False)
        self.loader = BrandLoader.from_file(brand_path)
        self.service = ApprovalService(self.repo, brand_loader=self.loader)

        # One clean 5-star draft and one low-star draft so the queue is non-empty.
        rid = self.repo.upsert_review(
            location_id=self.loc5, review_name="reviews/web1", rating=5,
            comment=CLEAN_COMMENT, reply_text=None,
        )
        self.repo.insert_reply_draft(rid, draft_text="Thanks!", status="needs_review")
        rid2 = self.repo.upsert_review(
            location_id=self.loc1, review_name="reviews/web2", rating=1,
            comment="Terrible, never coming back.", reply_text=None,
        )
        self.repo.insert_reply_draft(rid2, draft_text="Sorry to hear it.", status="needs_review")

        self.server = create_server(self.service, port=0)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repo.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ #
    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()

    def _get_raw_path(self, raw_path):
        """Send a literal request path with no client-side normalization."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest("GET", raw_path, skip_host=False, skip_accept_encoding=True)
            conn.endheaders()
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # static serving
    # ------------------------------------------------------------------ #
    def test_index_served_as_html(self):
        status, ctype, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        text = body.decode("utf-8")
        self.assertIn("DRAFT MODE", text)
        self.assertIn("nothing is published to Google", text)

    def test_app_js_served_with_js_content_type(self):
        status, ctype, body = self._get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        self.assertGreater(len(body), 0)

    def test_styles_css_served_with_css_content_type(self):
        status, ctype, body = self._get("/styles.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", ctype)
        self.assertGreater(len(body), 0)

    def test_traversal_is_refused_never_served(self):
        for raw in (
            "/../src/gbp/models.py",
            "/../../README.md",
            "/%2e%2e/src/gbp/models.py",
            "/..%2f..%2fpyproject.toml",
        ):
            status, body = self._get_raw_path(raw)
            self.assertIn(status, (400, 404), msg=f"{raw} -> {status}")
            text = body.decode("utf-8", "replace")
            # Never leak source: the models module starts with a docstring about
            # business profiles; pyproject has [project].
            self.assertNotIn("[project]", text, msg=raw)
            self.assertNotIn("class BrandProfile", text, msg=raw)

    def test_unknown_static_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/does-not-exist.js")
        self.assertEqual(ctx.exception.code, 404)

    # ------------------------------------------------------------------ #
    # API still works
    # ------------------------------------------------------------------ #
    def test_api_dashboard_still_works(self):
        status, ctype, body = self._get("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["needs_review"], 2)
        self.assertIn("total", data)

    def test_api_queue_still_works(self):
        status, _ctype, body = self._get("/api/queue")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertIsInstance(payload, dict)
        rows = payload["items"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(payload["total"], 2)
        self.assertIn("is_sensitive", rows[0])
        self.assertIn("rating", rows[0])
        self.assertIn("reason", rows[0])

    # ------------------------------------------------------------------ #
    # web security headers (Group 6C)
    # ------------------------------------------------------------------ #
    def _headers(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return {k.lower(): v for k, v in resp.headers.items()}

    def test_every_response_carries_security_headers(self):
        for path in ("/", "/app.js", "/styles.css", "/api/dashboard", "/api/queue"):
            headers = self._headers(path)
            self.assertEqual(headers.get("x-content-type-options"), "nosniff", path)
            self.assertEqual(headers.get("x-frame-options"), "DENY", path)
            self.assertEqual(headers.get("referrer-policy"), "no-referrer", path)
            self.assertEqual(
                headers.get("permissions-policy"),
                "geolocation=(), camera=(), microphone=()",
                path,
            )
            csp = headers.get("content-security-policy", "")
            self.assertIn("default-src 'self'", csp, path)
            self.assertIn("script-src 'self'", csp, path)
            self.assertIn("style-src 'self'", csp, path)
            self.assertIn("frame-ancestors 'none'", csp, path)
            self.assertIn("base-uri 'none'", csp, path)
            self.assertIn("form-action 'self'", csp, path)

    def test_hsts_absent_by_default(self):
        # The localhost demo runs over plain HTTP -> HSTS must NOT be emitted.
        for path in ("/", "/api/dashboard"):
            self.assertNotIn("strict-transport-security", self._headers(path), path)

    def test_hsts_present_when_enabled(self):
        os.environ["GBP_ENABLE_HSTS"] = "1"
        hsts = ""
        try:
            srv = create_server(self.service, port=0)
            _host, port = srv.server_address[:2]
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                url = f"http://127.0.0.1:{port}/api/dashboard"
                with urllib.request.urlopen(url, timeout=5) as resp:
                    hsts = resp.headers.get("Strict-Transport-Security", "")
            finally:
                srv.shutdown()
                srv.server_close()
                th.join(timeout=5)
        finally:
            os.environ.pop("GBP_ENABLE_HSTS", None)
        self.assertIn("max-age=", hsts)
        self.assertIn("includeSubDomains", hsts)

    def test_json_api_responses_are_no_store(self):
        for path in ("/api/dashboard", "/api/queue", "/api/locations"):
            self.assertEqual(self._headers(path).get("cache-control"), "no-store", path)

    # ------------------------------------------------------------------ #
    # UI contract (static markers the tests pin down)
    # ------------------------------------------------------------------ #
    def test_html_has_all_area_labels(self):
        html = _read(WEB_HTML)
        for label in AREA_LABELS:
            self.assertIn(label, html, msg=f"missing area label: {label}")
        # switchable without a page reload -> a tab per area
        self.assertEqual(html.count('class="tab"'), len(AREA_LABELS))

    def test_html_has_bulk_approve_control(self):
        html = _read(WEB_HTML)
        self.assertIn('id="bulk-approve-btn"', html)
        self.assertIn("Approve selected", html)
        # the top banner is present in the page itself
        self.assertIn("DRAFT MODE", html)

    def test_js_defines_bulk_block_markers_and_disables_rows(self):
        js = _read(WEB_JS)
        for marker in BULK_BLOCK_MARKERS:
            self.assertIn(marker, js, msg=f"missing bulk block marker: {marker}")
        # the checkbox is disabled and carries a visible reason for blocked rows
        self.assertIn("data-bulk-disabled", js)
        self.assertIn("data-bulk-block", js)
        self.assertIn(".disabled = true", js)
        # sensitive rows are blocked too
        self.assertIn("is_sensitive", js)

    def test_js_handles_409_and_423(self):
        js = _read(WEB_JS)
        self.assertIn("409", js)
        self.assertIn("423", js)
        self.assertIn("Publish blocked", js)
        # never claim a reply was published
        self.assertIn("nothing is published", js.lower())

    def test_css_has_mobile_breakpoint(self):
        css = _read(WEB_CSS)
        self.assertIn("@media", css)
        self.assertIn("700px", css)

    # ------------------------------------------------------------------ #
    # V2 app shell (Group 7 / T18)
    # ------------------------------------------------------------------ #
    def test_shell_brand_title_and_module_subtitle(self):
        html = _read(WEB_HTML)
        self.assertIn("Steven AI OS", html)
        self.assertIn("Google Reviews Manager", html)

    def test_shell_nav_labels_present_in_order(self):
        html = _read(WEB_HTML)
        pos = -1
        for label in SHELL_NAV_LABELS:
            idx = html.find(label, pos + 1)
            self.assertNotEqual(idx, -1, msg=f"missing nav label: {label}")
            self.assertGreater(idx, pos, msg=f"nav label out of order: {label}")
            pos = idx

    def test_shell_has_system_status_settings_and_version(self):
        html = _read(WEB_HTML)
        self.assertIn("System Status", html)
        self.assertIn("Settings", html)
        self.assertRegex(html, r"v\d+\.\d+\.\d+", msg="no version string in the shell")

    def test_js_has_i18n_map_defaulting_to_english(self):
        js = _read(WEB_JS)
        self.assertIn("STRINGS", js)
        self.assertRegex(js, r"STRINGS\s*=\s*\{\s*en\s*:")
        self.assertRegex(js, r"DEFAULT_LOCALE\s*=\s*\"en\"")
        # the nav labels are routed through the i18n map
        self.assertIn("Review Inbox", js)

    def test_shell_has_mobile_drawer_control_and_focus_visible(self):
        html = _read(WEB_HTML)
        js = _read(WEB_JS)
        css = _read(WEB_CSS)
        self.assertIn('id="nav-toggle"', html)
        self.assertIn('id="nav-overlay"', html)
        self.assertIn("Escape", js)          # Escape closes the drawer
        self.assertIn("openDrawer", js)
        self.assertIn("closeDrawer", js)
        self.assertIn("focus-visible", css)  # visible focus states

    def test_draft_mode_banner_string_exact(self):
        html = _read(WEB_HTML)
        self.assertIn("DRAFT MODE — nothing is published to Google", html)

    def test_js_has_standard_panel_states(self):
        js = _read(WEB_JS)
        self.assertIn("renderLoading", js)
        self.assertIn("renderEmpty", js)
        self.assertIn("renderErrorState", js)
        self.assertIn("Not tracked", js)
        self.assertIn("No data available.", js)

    def test_js_supports_area_aliases(self):
        js = _read(WEB_JS)
        self.assertIn("AREA_ALIASES", js)
        self.assertIn("overview", js)
        self.assertIn("review-inbox", js)


class GoogleConnectionUiTests(unittest.TestCase):
    """Issue #19: offline Google connection UI + fixture-based read-only preview.

    Static contract only -- the preview is hard-coded FIXTURE DATA, there is no
    live Google call and no new API endpoint, so nothing here touches the
    network beyond the loopback static fetch.
    """

    HTML = _read(WEB_HTML)
    JS = _read(WEB_JS)

    def test_connection_area_and_nav_are_present(self):
        self.assertIn('id="area-google"', self.HTML)
        self.assertIn('data-area="google"', self.HTML)
        self.assertIn("Google Connection", self.HTML)
        # routed in app.js (AREAS + showArea dispatch)
        self.assertIn('google:      { section: "google" }', self.JS)
        self.assertIn('renderGoogleConnection', self.JS)
        self.assertIn('"google-connection": "google"', self.JS)

    def test_not_ready_message_is_clear_and_never_fakes_connected(self):
        self.assertIn('id="google-readiness"', self.HTML)
        self.assertIn('data-google-state="unknown"', self.HTML)
        # a distinct not-ready sentence exists and is used on both the error path
        self.assertIn("notReady:", self.JS)
        self.assertIn("Google is not connected.", self.JS)
        self.assertIn('data-google-state", "not_ready"', self.JS)
        # the connected state is only ever reported from the real API flag
        self.assertIn("google_connected === true", self.JS)

    def test_preview_is_labeled_as_fixture_data(self):
        self.assertIn("GOOGLE_PREVIEW_FIXTURE", self.JS)
        self.assertIn("FIXTURE DATA", self.JS)
        self.assertIn("not read from Google", self.HTML)

    def test_preview_exposes_up_to_five_locations_with_required_fields(self):
        ids = re.findall(r'id:\s*"locations/fx-\d{3}"', self.JS)
        self.assertEqual(len(ids), 5, "expected exactly five fixture locations")
        for field in ('name:', "address:", "zip:"):
            self.assertIn(field, self.JS, msg=f"fixture location missing {field}")
        # the renderer prints name / address / ZIP / ID columns
        for col in ("google.colName", "google.colAddress", "google.colZip", "google.colId"):
            self.assertIn(col, self.JS, msg=f"missing preview column {col}")
        # the cap is explicit and enforced
        self.assertIn("GOOGLE_PREVIEW_MAX = 5", self.JS)
        self.assertIn("slice(0, GOOGLE_PREVIEW_MAX)", self.JS)

    def test_selected_location_shows_read_only_fixture_reviews(self):
        self.assertIn('id="google-preview-reviews"', self.HTML)
        self.assertIn("renderGoogleFixtureReviews", self.JS)
        self.assertIn('"locations/fx-001": [', self.JS)
        self.assertIn("google.noReviews", self.JS)
        self.assertIn("google.selectLocation", self.JS)

    def test_connect_control_calls_only_our_own_routes_and_never_google_directly(self):
        # Issue #114: the Connect control is wired through OUR OWN API -- the
        # browser navigates to /api/google/connect and the SERVER 302s to Google
        # (the client never holds the credentials). So the JS must reference our
        # routes and must never call Google directly or invent a foreign host.
        self.assertNotIn("googleapis.com", self.JS)
        self.assertNotIn("accounts.google.com", self.JS)
        for own in ("/api/google/connect", "/api/google/connections",
                    "/api/google/disconnect"):
            self.assertIn(own, self.JS, msg=f"missing own route {own}")
        # The live connect panel exists in the markup but is hidden by default,
        # so the fixture build never shows a connect control.
        self.assertIn('id="google-connections-panel"', self.HTML)
        self.assertIn('id="google-connections"', self.HTML)

    def test_index_page_serves_the_connection_area(self):
        repo = Repository(":memory:")
        repo.init_db(SCHEMA_PATH)
        acct = repo.upsert_account("accounts/1001")
        repo.upsert_location(acct, "accounts/1001/locations/loc-001",
                             title="Demo Nails", city="Garden Grove", state="CA", language="en")
        service = ApprovalService(repo)
        server = create_server(service, port=0)
        host, port = server.server_address[:2]
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                body = resp.read().decode("utf-8")
            self.assertIn('id="area-google"', body)
        finally:
            server.shutdown()
            server.server_close()
            th.join(timeout=5)
            repo.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
