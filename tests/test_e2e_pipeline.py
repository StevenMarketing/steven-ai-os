"""End-to-end pipeline test over a generated mock dataset (Group 5C).

Runs the WHOLE offline pipeline in one place, no network anywhere:

    google-mock transport -> GbpClient -> ReviewSync -> sqlite DB
    -> ReviewReplyEngine (MockModelClient) -> validator -> ApprovalService
    -> dashboard counts -> bulk_approve -> publish_guard

The dataset is generated in-process (>= 20 locations) and deliberately mixes:

* 1..5 star reviews,
* a rating-only review (and a low-star rating-only one),
* an already-replied review,
* EN + VI reviews,
* a sensitive (health-signal) review,
* a duplicate review that appears on TWO pages of the same location.

Every HTTP call is served by ``MockTransport`` and every model completion by
``MockModelClient`` -- standard library only, offline.

Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.brand import BrandLoader  # noqa: E402
from gbp.client.gbp_client import GbpClient  # noqa: E402
from gbp.client.http_client import MockTransport  # noqa: E402
from gbp.reply.classify import classify  # noqa: E402
from gbp.reply.duplicate import DuplicateChecker  # noqa: E402
from gbp.reply.engine import ReviewReplyEngine  # noqa: E402
from gbp.reply.generator import ReplyGenerator  # noqa: E402
from gbp.reply.model_client import MockModelClient  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.sync.reviews_sync import ReviewSync  # noqa: E402
from gbp.workflow.service import ApprovalService, PublishBlocked  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")

BASE = "https://mybusiness.googleapis.com"
ACCOUNT = "accounts/9001"
N_LOCATIONS = 20
#: location index -> brand auto-publish flag (0 = disabled). Keeps both
#: eligible and non-eligible brands in the same run.
AUTO_PUBLISH_DISABLED = {1, 6, 11, 16}

ALLOWED_DRAFT_STATUSES = {"draft", "needs_review", "approved", "rejected"}
KNOWN_CITIES = ["Garden Grove", "San Jose", "Houston", "Dallas", "Westminster", "Atlanta"]


def location_name(index: int) -> str:
    """GBP resource name for generated location ``index``."""
    return f"{ACCOUNT}/locations/loc-{index:03d}"


def _tag(seed: int) -> str:
    """A unique, pure-letters token (longest word in every comment).

    Digits would be stripped by the validator's tokeniser, so the seed is
    letter-encoded -- that keeps the token unique and always the longest one.
    """
    letters = "".join(chr(ord("a") + int(d)) for d in f"{seed:04d}")
    return f"detailref{letters}"


def _review(loc: str, rid: str, rating: int, comment, update_time: str, reply=None):
    review = {
        "name": f"{loc}/reviews/{rid}",
        "reviewId": rid,
        "rating": rating,
        "comment": comment,
        "createTime": update_time,
        "updateTime": update_time,
        "reviewer": {"displayName": "Guest"},
    }
    if reply:
        review["reviewReply"] = {"comment": reply, "updateTime": update_time}
    return review


def build_dataset():
    """Return ``(pages_by_location, expected_unique_names, brands)``.

    ``pages_by_location[loc]`` is the list of raw review pages for that
    location, in traversal order.
    """
    pages: dict = {}
    names: set = set()
    brands = []

    for i in range(N_LOCATIONS):
        loc = location_name(i)
        stamp = f"2026-09-{(i % 27) + 1:02d}T10:00:00Z"

        page1 = []
        page2 = []

        # a) star carries 1..5 so every rating appears somewhere.
        stars_a = (i % 5) + 1
        page1.append(
            _review(loc, f"r{i:02d}-a", stars_a,
                    f"Loved the service today, the {_tag(i * 10 + 1)} detail really stood out.",
                    stamp)
        )
        # b) a Vietnamese review (5 star, non-sensitive).
        page1.append(
            _review(loc, f"r{i:02d}-b", 5,
                    f"Dịch vụ tốt và nhân viên dễ thương, {_tag(i * 10 + 2)} rất đáng khen.",
                    stamp)
        )
        # c) a rating-only review: low-star (needs_review) every 3rd shop,
        #    otherwise a clean 5-star rating.
        page1.append(
            _review(loc, f"r{i:02d}-c", 3 if i % 3 == 0 else 5, "", stamp)
        )
        # d) an already-replied review every 4th shop (engine must skip it).
        if i % 4 == 0:
            replied = _review(
                loc, f"r{i:02d}-d", 5,
                f"Great experience, the {_tag(i * 10 + 4)} service was wonderful.",
                stamp,
                reply="Thanks so much for visiting us, we hope to see you again soon.",
            )
            (page2 if i == 0 else page1).append(replied)
        # s) a sensitive (health-signal) review every 5th shop.
        if i % 5 == 0:
            sensitive = _review(
                loc, f"r{i:02d}-s", 3,
                "I got an infection after my pedicure and my foot was bleeding, "
                f"please review the {_tag(i * 10 + 5)} report.",
                stamp,
            )
            (page2 if i == 0 else page1).append(sensitive)

        if i == 0:
            # A duplicate review across TWO pages: same name, page 2 newer.
            dup_name = f"{loc}/reviews/r{i:02d}-dup"
            page1.append(_review(
                loc, f"r{i:02d}-dup", 3,
                f"Nice visit overall, the {_tag(i * 10 + 3)} touch was lovely.",
                "2026-08-01T10:00:00Z",
            ))
            page2.append(_review(
                loc, f"r{i:02d}-dup", 4,
                f"Nice visit overall, the {_tag(i * 10 + 3)} touch was lovely and quick.",
                "2026-09-30T10:00:00Z",
            ))
            pages[loc] = [
                {"reviews": page1, "nextPageToken": "tok-loc-000"},
                {"reviews": page2},
            ]
            names.update(r["name"] for r in page1 + page2)
        else:
            pages[loc] = [{"reviews": page1}]
            names.update(r["name"] for r in page1)

        brands.append({
            "location_id": loc,
            "business_name": f"End-to-End Nails #{i}",
            "city": KNOWN_CITIES[i % len(KNOWN_CITIES)],
            "state": "CA",
            "category": "Nail Salon",
            "services_json": ["Manicure", "Pedicure"],
            "language": "vi" if i % 2 else "en",
            "tone": "warm",
            "seo_keywords_json": ["Nail Salon", "Manicure"],
            "rules_json": {"do_not_mention": ["Kim"]},
            "auto_publish_allowed": 0 if i in AUTO_PUBLISH_DISABLED else 1,
        })

    return pages, names, {"profiles": brands}


def make_reply(system: str, user: str) -> str:
    """Deterministic reply echoing the review's unique tag token.

    Echoing a token lifted verbatim from the review keeps the draft grounded
    and relevant, so an ordinary review validates and lands as ``draft``.
    """
    payload = json.loads(user)
    comment = str(payload.get("review", {}).get("comment") or "")
    tokens = re.findall(r"[^\W\d_]+", comment, re.UNICODE)
    if tokens:
        tag = max(tokens, key=len).lower()
        text = (
            f"Thank you for taking the time to write about {tag}, "
            "we truly appreciate your feedback."
        )
    else:
        text = "Thank you so much for leaving us this rating, it means a lot to our team."
    return json.dumps({"reply": text})


class E2EPipelineTests(unittest.TestCase):
    """Generated 20-location dataset pushed through the whole offline pipeline."""

    def setUp(self) -> None:
        self.pages, self.expected_names, self.brand_doc = build_dataset()
        self.locations = [location_name(i) for i in range(N_LOCATIONS)]

        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        self.addCleanup(self.repo.close)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.brand_path = os.path.join(self.tmp.name, "brand_profiles.json")
        with open(self.brand_path, "w", encoding="utf-8") as handle:
            json.dump(self.brand_doc, handle, ensure_ascii=False)
        self.loader = BrandLoader.from_file(self.brand_path)

        # -- transport + client ----------------------------------------- #
        self.transport = MockTransport()
        for loc in self.locations:
            url = f"{BASE}/v4/{loc}/reviews"
            pages = self.pages[loc]
            if len(pages) > 1:
                # Param-specific page FIRST (first matching route wins).
                self.transport.register(
                    "GET", url, pages[1],
                    params={"pageSize": 50, "pageToken": pages[0]["nextPageToken"]},
                )
            self.transport.register("GET", url, pages[0], params={"pageSize": 50})
        self.client = GbpClient(self.transport, base=BASE)

        # -- sync ------------------------------------------------------- #
        self.sync = ReviewSync(self.repo, self.client)
        self.sync_results = self.sync.sync_all(self.locations)

        # -- engine ----------------------------------------------------- #
        model = MockModelClient(responses=[make_reply])
        generator = ReplyGenerator(model, self.loader, DuplicateChecker())
        self.engine = ReviewReplyEngine(
            self.repo, generator, DuplicateChecker(), self.loader
        )
        self.results = {}
        for row in self.repo.get_reviews():
            self.results[row["id"]] = self.engine.process(row)

        # -- approval service ------------------------------------------- #
        self.service = ApprovalService(self.repo, brand_loader=self.loader)

        self.all_drafts = [r for r in self.repo.get_reply_drafts()]

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def count(self, sql: str, args=()) -> int:
        return int(self.repo.conn.execute(sql, args).fetchone()[0])

    def drafts_by_review(self):
        out = {}
        for row in self.repo.get_reply_drafts():
            out.setdefault(row["review_id"], []).append(row)
        return out

    def review_row(self, review_id):
        return self.repo.get_review(review_id)

    # ================================================================== #
    # 1. sync: stored exactly once, duplicates collapsed, rerun idempotent
    # ================================================================== #
    def test_sync_stores_each_review_exactly_once(self):
        self.assertGreaterEqual(len(self.locations), 20)
        stored = self.count("SELECT COUNT(*) FROM reviews")
        self.assertEqual(stored, len(self.expected_names))
        # No duplicate review_name rows ever exist.
        self.assertEqual(
            self.count("SELECT COUNT(*) - COUNT(DISTINCT review_name) FROM reviews"), 0
        )
        # Locations were persisted for every shop.
        self.assertEqual(self.count("SELECT COUNT(*) FROM business_locations"), 20)

    def test_duplicate_across_two_pages_collapses_to_one_row(self):
        dup_name = f"{location_name(0)}/reviews/r00-dup"
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM reviews WHERE review_name = ?", (dup_name,)), 1
        )
        # The NEWER snapshot (page 2, rating 4) must have won.
        row = self.repo.conn.execute(
            "SELECT rating, update_time FROM reviews WHERE review_name = ?", (dup_name,)
        ).fetchone()
        self.assertEqual(row["rating"], 4)
        self.assertEqual(row["update_time"], "2026-09-30T10:00:00Z")

    def test_second_sync_upserts_zero_new_rows(self):
        before = self.count("SELECT COUNT(*) FROM reviews")
        first = sum(
            self.sync.incremental_sync(loc)["upserted"] for loc in self.locations
        )
        self.assertGreater(first, 0)  # watermark was empty: everything is (re)seen
        second = sum(
            self.sync.incremental_sync(loc)["upserted"] for loc in self.locations
        )
        self.assertEqual(second, 0)  # unchanged data => nothing new
        self.assertEqual(self.count("SELECT COUNT(*) FROM reviews"), before)

    # ================================================================== #
    # 2. drafts: expected status per classification, replied reviews skipped
    # ================================================================== #
    def test_draft_status_matches_classification(self):
        drafts_by_review = self.drafts_by_review()
        for review_id, out in self.results.items():
            row = self.review_row(review_id)
            if str(row["reply_text"] or "").strip():
                self.assertEqual(out["status"], "skipped")
                self.assertEqual(drafts_by_review.get(review_id, []), [])
                continue
            expected = (
                "needs_review" if classify(row).requires_manual_approval else "draft"
            )
            self.assertEqual(out["status"], expected, dict(row))
            drafts = drafts_by_review[review_id]
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0]["status"], expected)

    def test_dataset_contains_every_required_case(self):
        rows = [dict(r) for r in self.repo.get_reviews()]
        self.assertEqual({r["rating"] for r in rows}, {1, 2, 3, 4, 5})
        self.assertTrue(any(not str(r["comment"] or "").strip() for r in rows))  # rating-only
        self.assertTrue(any(str(r["reply_text"] or "").strip() for r in rows))  # replied
        self.assertTrue(any(classify(r).is_sensitive for r in rows))  # sensitive
        # EN + VI both present (diacritics = VI).
        self.assertTrue(any(re.search(r"[ăâđêôơư]", r["comment"] or "") for r in rows))
        self.assertTrue(any("loved" in (r["comment"] or "").lower() for r in rows))
        # A rating-only LOW star review requires manual approval.
        self.assertTrue(
            any(
                not str(r["comment"] or "").strip()
                and (r["rating"] or 0) <= 4
                and classify(r).requires_manual_approval
                for r in rows
            )
        )

    # ================================================================== #
    # 3. validator results are persisted for each draft
    # ================================================================== #
    def test_validator_results_are_persisted_on_the_draft(self):
        audit = self.repo.get_audit_logs(entity="reply_draft")
        self.assertGreater(len(audit), 0)
        by_draft = {str(a["entity_id"]): json.loads(a["detail_json"]) for a in audit}

        checked_clean = 0
        for draft in self.all_drafts:
            detail = by_draft.get(str(draft["id"]))
            self.assertIsNotNone(detail, f"no audit detail for draft {draft['id']}")
            self.assertIn("validation_ok", detail)
            self.assertIn("validation_failed", detail)
            if draft["status"] == "draft":
                # A clean auto-draft must have passed every validator check.
                self.assertTrue(detail["validation_ok"], detail)
                self.assertEqual(detail["validation_failed"], [])
                checked_clean += 1
        self.assertGreater(checked_clean, 0)

    # ================================================================== #
    # 4. dashboard counts match the DB
    # ================================================================== #
    def test_dashboard_counts_match_db(self):
        counts = self.service.dashboard_counts()
        grouped = {
            str(r["status"]): int(r["n"])
            for r in self.repo.conn.execute(
                "SELECT status, COUNT(*) AS n FROM reply_drafts GROUP BY status"
            )
        }
        total = self.count("SELECT COUNT(*) FROM reply_drafts")
        for status, n in grouped.items():
            self.assertEqual(counts[status], n, status)
        for status in ("draft", "needs_review", "approved", "rejected",
                       "ready_to_publish", "published", "failed"):
            self.assertEqual(counts[status], grouped.get(status, 0), status)
        self.assertEqual(counts["total"], total)
        self.assertEqual(counts["published"], 0)

    # ================================================================== #
    # 5. bulk_approve skips 1-2 star + sensitive
    # ================================================================== #
    def test_bulk_approve_skips_low_stars_and_sensitive(self):
        ids = [d["id"] for d in self.all_drafts]
        out = self.service.bulk_approve(ids, "owner@e2e")

        approved_ids = {a["draft_id"] for a in out["approved"]}
        skipped = {s["id"]: s["reason"] for s in out["skipped"]}
        self.assertTrue(approved_ids)
        self.assertTrue(skipped)

        drafts = {d["id"]: d for d in self.all_drafts}
        for draft_id in ids:
            review = self.review_row(drafts[draft_id]["review_id"])
            c = classify(review)
            brand = self.service._brand_for(review)
            eligible = not (
                c.stars in (1, 2)
                or c.is_sensitive
                or not getattr(brand, "complete", False)
                or not getattr(brand, "auto_publish_allowed", False)
            )
            if draft_id in approved_ids:
                self.assertTrue(eligible, f"approved an ineligible draft {draft_id}")
            else:
                self.assertIn(draft_id, skipped)
                self.assertFalse(eligible, f"skipped an eligible draft {draft_id}")

        # Every 1-2 star and every sensitive draft was skipped, never approved.
        for draft_id in ids:
            review = self.review_row(drafts[draft_id]["review_id"])
            c = classify(review)
            if c.stars in (1, 2):
                self.assertNotIn(draft_id, approved_ids)
                self.assertIn("low_stars", skipped[draft_id])
            if c.is_sensitive:
                self.assertNotIn(draft_id, approved_ids)
                self.assertIn("sensitive", skipped[draft_id])

    # ================================================================== #
    # 6. publish_guard refusals
    # ================================================================== #
    def test_publish_guard_refuses_not_approved(self):
        pending = [d for d in self.all_drafts if d["status"] in ("draft", "needs_review")]
        self.assertTrue(pending)
        for draft in pending[:5]:
            with self.assertRaises(PublishBlocked) as ctx:
                self.service.publish_guard(draft["id"])
            self.assertIn("state_not_publishable", ctx.exception.reason)

    def test_publish_guard_refuses_review_already_has_reply(self):
        row = self.repo.conn.execute(
            "SELECT id FROM reviews WHERE TRIM(COALESCE(reply_text, '')) <> '' LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        draft_id = self.repo.insert_reply_draft(
            row["id"], draft_text="draft for a review that already has a reply.",
            status="approved",
        )
        with self.assertRaises(PublishBlocked) as ctx:
            self.service.publish_guard(draft_id)
        self.assertEqual(ctx.exception.reason, "review_already_has_reply")

    def test_publish_guard_refuses_brand_auto_publish_disabled(self):
        disabled_locs = {self.repo.upsert_location(
            self.repo.upsert_account(ACCOUNT), location_name(i)) for i in AUTO_PUBLISH_DISABLED
        }
        drafts = {d["id"]: d for d in self.all_drafts}
        target = None
        for draft in self.all_drafts:
            review = self.review_row(draft["review_id"])
            c = classify(review)
            if (
                review["location_id"] in disabled_locs
                and not (c.stars in (1, 2) or c.is_sensitive)
                and not str(review["reply_text"] or "").strip()
            ):
                target = draft
                break
        self.assertIsNotNone(target, "expected an eligible draft on a brand-disabled shop")

        approved = self.service.approve(target["id"], "owner@e2e")
        self.assertEqual(approved["status"], "approved")  # not ready_to_publish
        self.assertFalse(approved["auto_publish_eligible"])
        with self.assertRaises(PublishBlocked) as ctx:
            self.service.publish_guard(target["id"])
        self.assertEqual(ctx.exception.reason, "brand_auto_publish_disabled")

    # ================================================================== #
    # 7. publish_guard succeeds without side effects
    # ================================================================== #
    def test_publish_guard_ok_for_eligible_approved_draft(self):
        ids = [d["id"] for d in self.all_drafts]
        self.service.bulk_approve(ids, "owner@e2e")
        ready = [d for d in self.repo.get_reply_drafts() if d["status"] == "ready_to_publish"]
        self.assertTrue(ready)

        approvals_before = self.count("SELECT COUNT(*) FROM approval_records")
        audits_before = self.count("SELECT COUNT(*) FROM audit_logs")
        out = self.service.publish_guard(ready[0]["id"])
        self.assertTrue(out["publishable"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM approval_records"), approvals_before)
        self.assertEqual(self.count("SELECT COUNT(*) FROM audit_logs"), audits_before)

    # ================================================================== #
    # 8. nothing is ever published
    # ================================================================== #
    def test_no_row_ever_reaches_published(self):
        self.service.bulk_approve([d["id"] for d in self.all_drafts], "owner@e2e")
        statuses = {
            str(r["status"]) for r in self.repo.conn.execute("SELECT status FROM reply_drafts")
        }
        self.assertNotIn("published", statuses)
        self.assertTrue(statuses <= ALLOWED_DRAFT_STATUSES | {"ready_to_publish", "failed"})
        self.assertEqual(self.count("SELECT COUNT(*) FROM reply_drafts WHERE status = 'published'"), 0)

    # ================================================================== #
    # 9. locations stay isolated
    # ================================================================== #
    def test_locations_are_isolated_in_the_queue(self):
        loc_a = self.locations[0]
        loc_b = self.locations[1]
        id_a = self.repo.upsert_location(self.repo.upsert_account(ACCOUNT), loc_a)
        id_b = self.repo.upsert_location(self.repo.upsert_account(ACCOUNT), loc_b)

        queue_a = self.service.list_queue(location_id=loc_a)
        self.assertTrue(queue_a)
        self.assertTrue(all(item["location_id"] == id_a for item in queue_a))

        drafts_by_review = self.drafts_by_review()
        b_draft_ids = {
            d["id"]
            for r in self.repo.get_reviews()
            if r["location_id"] == id_b
            for d in drafts_by_review.get(r["id"], [])
        }
        self.assertTrue(b_draft_ids)
        queue_a_ids = {item["draft_id"] for item in queue_a}
        self.assertTrue(b_draft_ids.isdisjoint(queue_a_ids))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
