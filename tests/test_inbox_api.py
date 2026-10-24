"""Offline tests for Group 7 / T20 -- Review Inbox API + approval UX backing.

Covers the ADDITIVE ``GET /api/reviews`` list and ``GET /api/reviews/<id>``
detail endpoints. Standard library only, MOCK data only -- nothing here touches
the network (the server binds 127.0.0.1 on an ephemeral port). The approval
policy (bulk-approve skips, validator, publish_guard) MUST behave exactly as
before: these tests pin that down. Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import (  # noqa: E402
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    create_server,
)
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")

SENSITIVE_COMMENT = "I got a serious infection after my pedicure and it was bleeding."


class ReviewInboxApiTestCase(unittest.TestCase):
    """In-memory repo + a running loopback server in mock (auth=None) mode."""

    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc_a = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-001",
            title="Demo Nails A", city="Garden Grove", state="CA", language="en",
        )
        self.loc_b = self.repo.upsert_location(
            acct, "accounts/1001/locations/loc-002",
            title="Demo Nails B", city="Westminster", state="CA", language="vi",
        )
        # loc A: a COMPLETE brand profile with auto-publish eligibility, so an
        # ordinary 4-5 star draft is eligible for the (dry-run only) workflow.
        self.repo.conn.execute(
            "INSERT INTO brand_profiles(location_id, business_name, city, language,"
            " services_json, seo_keywords_json, auto_publish_allowed)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            (self.loc_a, "Sunny Nails Spa", "Garden Grove", "en",
             json.dumps(["Manicure"]), json.dumps(["Nail Salon"])),
        )
        self.repo.conn.commit()

        # A 5-star review WITH a stored draft (real draft text).
        self.r_clean = self.repo.upsert_review(
            self.loc_a, "reviews/i1", rating=5,
            comment="Loved my gel manicure, thank you so much!")
        self.d_clean = self.repo.insert_reply_draft(
            self.r_clean, draft_text="Thank you for the kind words!",
            status="needs_review")

        # A 5-star review WITHOUT any draft (has_draft False -> "Reply not generated").
        self.r_nodraft = self.repo.upsert_review(
            self.loc_a, "reviews/i2", rating=5, comment="Great work, very neat.")

        # A 1-star review -> requires manual approval, bulk-approve SKIPS it.
        self.r_low = self.repo.upsert_review(
            self.loc_a, "reviews/i3", rating=1, comment="Terrible service, never again.")
        self.d_low = self.repo.insert_reply_draft(
            self.r_low, draft_text="We are sorry.", status="draft")

        # A sensitive review -> requires manual approval, bulk-approve SKIPS it.
        self.r_sens = self.repo.upsert_review(
            self.loc_a, "reviews/i4", rating=3, comment=SENSITIVE_COMMENT)
        self.d_sens = self.repo.insert_reply_draft(
            self.r_sens, draft_text="Please contact us.", status="draft")

        # An ordinary 4-star review in the OTHER location (language vi).
        self.r_b = self.repo.upsert_review(
            self.loc_b, "reviews/i5", rating=4, comment="Nice place")
        self.d_b = self.repo.insert_reply_draft(
            self.r_b, draft_text="Merci!", status="rejected")

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

    # ------------------------------------------------------------------ #
    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path, body):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _items(self, path="/api/reviews"):
        status, payload = self._get(path)
        self.assertEqual(status, 200)
        return payload

    # ------------------------------------------------------------------ #
    # A. list items + business_name join
    # ------------------------------------------------------------------ #
    def test_list_items_expose_business_name_not_resource_id(self):
        payload = self._items()
        self.assertEqual(set(payload), {"items", "total", "page", "page_size",
                                        "pages", "sort", "order"})
        by_review = {r["review_id"]: r for r in payload["items"]}
        self.assertEqual(payload["total"], 5)

        required = {"review_id", "location_id", "stars", "comment", "review_date",
                    "language", "status", "has_draft", "draft_text",
                    "validator_reason", "requires_manual_approval",
                    "location_resource_id", "business_name"}
        for r in payload["items"]:
            self.assertTrue(required.issubset(set(r)), msg=str(set(required) - set(r)))

        # loc A reviews carry the brand business_name (never the resource id).
        self.assertEqual(by_review[self.r_clean]["business_name"], "Sunny Nails Spa")
        self.assertEqual(by_review[self.r_clean]["location_resource_id"],
                         "accounts/1001/locations/loc-001")
        self.assertNotEqual(by_review[self.r_clean]["business_name"],
                            by_review[self.r_clean]["location_resource_id"])
        # loc B has no brand profile -> falls back to the location title.
        self.assertEqual(by_review[self.r_b]["business_name"], "Demo Nails B")

        # Independently recompute the join over the seeded DB and compare.
        expected = {
            row["id"]: row["bn"]
            for row in self.repo.conn.execute(
                "SELECT r.id AS id,"
                " COALESCE(b.business_name, l.title, l.location_name) AS bn"
                " FROM reviews r"
                " LEFT JOIN business_locations l ON r.location_id = l.id"
                " LEFT JOIN brand_profiles b ON b.location_id = r.location_id"
            ).fetchall()
        }
        for r in payload["items"]:
            self.assertEqual(r["business_name"], expected[r["review_id"]])

        # No item exposes the raw resource id AS a title field.
        for r in payload["items"]:
            self.assertNotIn("/", r["business_name"])

    def test_has_draft_and_draft_text_both_cases(self):
        by_review = {r["review_id"]: r for r in self._items()["items"]}
        # WITH a stored draft -> the REAL text.
        self.assertTrue(by_review[self.r_clean]["has_draft"])
        self.assertEqual(by_review[self.r_clean]["draft_text"],
                         "Thank you for the kind words!")
        # WITHOUT a stored draft -> has_draft False and draft_text None.
        self.assertFalse(by_review[self.r_nodraft]["has_draft"])
        self.assertIsNone(by_review[self.r_nodraft]["draft_text"])
        self.assertEqual(by_review[self.r_nodraft]["status"], "no_draft")
        # Both cases are present in the fixtures.
        self.assertTrue(any(r["has_draft"] for r in by_review.values()))
        self.assertTrue(any(not r["has_draft"] for r in by_review.values()))

    def test_requires_manual_approval_flags(self):
        by_review = {r["review_id"]: r for r in self._items()["items"]}
        self.assertTrue(by_review[self.r_low]["requires_manual_approval"])
        self.assertEqual(by_review[self.r_low]["stars"], 1)
        self.assertTrue(by_review[self.r_sens]["requires_manual_approval"])
        self.assertTrue(by_review[self.r_sens]["is_sensitive"])
        self.assertIsNotNone(by_review[self.r_sens]["validator_reason"])
        # Ordinary 4-5 star reviews are NOT manual-approval.
        self.assertFalse(by_review[self.r_clean]["requires_manual_approval"])
        self.assertFalse(by_review[self.r_b]["requires_manual_approval"])

    # ------------------------------------------------------------------ #
    # B. filters / sort / pagination
    # ------------------------------------------------------------------ #
    def test_filters(self):
        self.assertEqual(self._items("/api/reviews?stars=1")["total"], 1)
        self.assertEqual(self._items("/api/reviews?status=needs_review")["total"], 1)
        self.assertEqual(self._items("/api/reviews?status=rejected")["total"], 1)
        lang = self._items("/api/reviews?language=vi")
        self.assertEqual(lang["total"], 1)
        self.assertEqual(lang["items"][0]["location_id"], self.loc_b)
        loc = self._items("/api/reviews?location_id=accounts/1001/locations/loc-001")
        self.assertEqual(loc["total"], 4)
        self.assertEqual(self._items("/api/reviews?since=2999-01-01")["total"], 0)
        self.assertEqual(self._items("/api/reviews?q=manicure")["total"], 1)

    def test_sort(self):
        asc = self._items("/api/reviews?sort=stars&order=asc")
        stars = [r["stars"] for r in asc["items"]]
        self.assertEqual(stars, sorted(stars))
        desc = self._items("/api/reviews?sort=stars&order=desc")
        stars = [r["stars"] for r in desc["items"]]
        self.assertEqual(stars, sorted(stars, reverse=True))
        syl = self._items("/api/reviews?sort=business_name&order=asc")
        names = [r["business_name"] for r in syl["items"]]
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_pagination_math_and_page_size_cap(self):
        p1 = self._items("/api/reviews?page=1&page_size=2")
        self.assertEqual(p1["total"], 5)
        self.assertEqual(p1["pages"], 3)
        self.assertEqual(len(p1["items"]), 2)
        p3 = self._items("/api/reviews?page=3&page_size=2")
        self.assertEqual(len(p3["items"]), 1)
        self.assertEqual(self._items("/api/reviews?page=9&page_size=2")["items"], [])
        # defaults + cap
        self.assertEqual(self._items("/api/reviews")["page_size"], DEFAULT_PAGE_SIZE)
        self.assertEqual(self._items("/api/reviews?page_size=1000")["page_size"],
                         MAX_PAGE_SIZE)
        # no overlap across pages
        ids = ([r["review_id"] for r in p1["items"]]
               + [r["review_id"] for r in self._items("/api/reviews?page=2&page_size=2")["items"]])
        self.assertEqual(len(ids), len(set(ids)))

    # ------------------------------------------------------------------ #
    # C. bulk-approve policy UNCHANGED (skips 1-2 star / sensitive)
    # ------------------------------------------------------------------ #
    def test_bulk_approve_skips_low_star_and_sensitive(self):
        status, res = self._post("/api/drafts/bulk-approve",
                                 {"ids": [self.d_clean, self.d_low, self.d_sens],
                                  "actor": "manager"})
        self.assertEqual(status, 200)
        approved_ids = sorted(a["draft_id"] for a in res["approved"])
        self.assertEqual(approved_ids, [self.d_clean])
        self.assertEqual(len(res["skipped"]), 2)
        skipped_ids = sorted(s["id"] for s in res["skipped"])
        self.assertEqual(skipped_ids, sorted([self.d_low, self.d_sens]))
        for s in res["skipped"]:
            self.assertIn("manual_approval", s["reason"])

    def test_approve_and_reject_still_work(self):
        status, res = self._post(f"/api/drafts/{self.d_clean}/approve",
                                 {"actor": "manager"})
        self.assertEqual(status, 200)
        self.assertIn(res["status"], ("approved", "ready_to_publish"))
        status, res = self._post(f"/api/drafts/{self.d_low}/reject",
                                 {"actor": "manager", "note": "not ok"})
        self.assertEqual(status, 200)
        self.assertEqual(res["status"], "rejected")

    def test_publish_remains_impossible(self):
        # The dry-run guard still REFUSES a non-publishable draft (423)...
        status, payload = self._post(f"/api/drafts/{self.d_low}/publish-guard",
                                     {"actor": "manager"})
        self.assertEqual(status, 423)
        self.assertEqual(payload["error"], "publish_blocked")
        # Issue #126: a publish ROUTE now exists, but this is the offline/fixture
        # build -- it refuses honestly (409) rather than pretending to publish.
        status, payload = self._post(f"/api/drafts/{self.d_low}/publish",
                                     {"actor": "manager"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "live_not_configured")
        # No draft row is ever 'published' or 'sent'.
        n = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM reply_drafts"
            " WHERE status IN ('published', 'sent')"
        ).fetchone()["n"]
        self.assertEqual(int(n), 0)

    # ------------------------------------------------------------------ #
    # D. detail endpoint
    # ------------------------------------------------------------------ #
    def test_detail_returns_review_drafts_and_audit(self):
        # Perform a real transition so there is an audit entry to expose.
        self._post(f"/api/drafts/{self.d_clean}/approve", {"actor": "manager"})
        status, detail = self._get(f"/api/reviews/{self.r_clean}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["business_name"], "Sunny Nails Spa")
        self.assertEqual(detail["review_id"], self.r_clean)
        self.assertEqual(len(detail["drafts"]), 1)
        self.assertEqual(detail["drafts"][0]["id"], self.d_clean)
        self.assertGreaterEqual(len(detail["audit"]), 1)
        self.assertEqual(detail["audit"][0]["entity"], "reply_draft")
        self.assertEqual(detail["audit"][0]["action"], "draft_approved")
        # recent first
        ids = [a["id"] for a in detail["audit"]]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_detail_404_for_missing_id(self):
        status, payload = self._get("/api/reviews/999999")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")
        status, _ = self._get("/api/reviews/not-an-int")
        self.assertEqual(status, 404)

    # ------------------------------------------------------------------ #
    # E. read-only: no AI call, no api_usage row
    # ------------------------------------------------------------------ #
    def test_inbox_get_creates_no_api_usage_row(self):
        before = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM api_usage").fetchone()["n"]
        self._items("/api/reviews?page_size=50")
        self._get(f"/api/reviews/{self.r_clean}")
        after = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM api_usage").fetchone()["n"]
        self.assertEqual(int(before), int(after))


if __name__ == "__main__":
    unittest.main(verbosity=2)
