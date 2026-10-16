"""Offline tests for the AI review-reply engine (Group 3).

MOCK model only -- nothing here touches the network. Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.brand import BrandLoader, BrandProfile, load_brand_profiles  # noqa: E402
from gbp.reply.classify import classify  # noqa: E402
from gbp.reply.duplicate import DuplicateChecker, keyword_density_issues  # noqa: E402
from gbp.reply.engine import ReviewReplyEngine  # noqa: E402
from gbp.reply.generator import SYSTEM_PROMPT, ReplyGenerator, extract_json  # noqa: E402
from gbp.reply.model_client import (  # noqa: E402
    ApiError,
    MockModelClient,
    OpenRouterModelClient,
    ModelClient,
)
from gbp.reply.validator import CHECK_NAMES, validate  # noqa: E402
from gbp.repo import Repository  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
FIXTURE_BRANDS = os.path.join(_ROOT, "fixtures", "brand_profiles.json")
FIXTURE_SENSITIVE = os.path.join(_ROOT, "fixtures", "reviews_sensitive.json")

ALLOWED_DRAFT_STATUSES = {"draft", "needs_review", "approved", "rejected"}


def R(text: str) -> str:
    """A model completion carrying one reply."""
    return json.dumps({"reply": text}, ensure_ascii=False)


BRAND_FIXTURE = {
    "profiles": [
        {
            "location_id": 1,
            "business_name": "Demo Nails — Garden Grove",
            "city": "Garden Grove",
            "state": "CA",
            "category": "Nail Salon",
            "services_json": ["Manicure", "Pedicure", "Gel Nails", "Acrylic Nails", "Nail Art"],
            "language": "en",
            "tone": "warm-professional",
            "seo_keywords_json": ["Nail Salon", "Manicure", "Gel Nails"],
            "rules_json": {"do_not_mention": ["Kim"], "escalation_email": "owner@demo.example"},
            "auto_publish_allowed": 0,
        },
        {
            "location_id": 2,
            "business_name": "Lotus Nails — Westminster",
            "city": "Westminster",
            "state": "CA",
            "services_json": ["Manicure", "Pedicure", "Dip Powder"],
            "language": "vi",
            "tone": "friendly",
            "seo_keywords_json": ["Nail Salon", "Pedicure"],
            "rules_json": {"do_not_mention": ["Anna"], "escalation_email": "owner@lotus.example"},
            "auto_publish_allowed": 1,
        },
    ]
}

#: A location with NO brand profile at all (neutral fallback tests).
LOC3 = "accounts/1001/locations/loc-003"


class BaseTestCase(unittest.TestCase):
    """In-memory repo + two shops + a temporary brand profile file."""

    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        account_id = self.repo.upsert_account("accounts/1001")
        self.loc1 = self.repo.upsert_location(
            account_id,
            "accounts/1001/locations/loc-001",
            title="Demo Nails — Garden Grove",
            city="Garden Grove",
            state="CA",
            language="en",
        )
        self.loc2 = self.repo.upsert_location(
            account_id,
            "accounts/1001/locations/loc-002",
            title="Lotus Nails — Westminster",
            city="Westminster",
            state="CA",
            language="vi",
        )
        self.loc3 = self.repo.upsert_location(
            account_id, LOC3, title="Unknown Shop", city="Anaheim", state="CA", language="en"
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.brand_path = os.path.join(self.tmp.name, "brand_profiles.json")
        with open(self.brand_path, "w", encoding="utf-8") as fh:
            json.dump(BRAND_FIXTURE, fh, ensure_ascii=False)
        self.loader = BrandLoader.from_file(self.brand_path)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ #
    def add_review(self, location_id, name, stars, comment=None, reply_text=None):
        review_id = self.repo.upsert_review(
            location_id=location_id,
            review_name=name,
            rating=stars,
            comment=comment,
            reply_text=reply_text,
            reply_update_time="2026-10-01T10:00:00Z" if reply_text else None,
        )
        return self.repo.get_review(review_id)

    def brand(self, location_id):
        return self.loader.get_for(location_id)

    def make_engine(self, responses=None, **kwargs):
        model = MockModelClient(responses=responses, **kwargs)
        generator = ReplyGenerator(model, self.loader, DuplicateChecker())
        engine = ReviewReplyEngine(self.repo, generator, DuplicateChecker(), self.loader)
        return engine, model

    def assertNoPublished(self):
        rows = self.repo.get_reply_drafts()
        statuses = {row["status"] for row in rows}
        self.assertTrue(statuses <= ALLOWED_DRAFT_STATUSES, f"bad statuses: {statuses}")
        self.assertNotIn("published", statuses)


# ====================================================================== #
# brand profiles
# ====================================================================== #
class BrandProfileTests(BaseTestCase):
    def test_neutral_fallback_for_unknown_location(self):
        profile = self.brand(3)
        self.assertFalse(profile.complete)
        self.assertEqual(profile.business_name, "")
        self.assertEqual(profile.city, "")
        self.assertEqual(profile.services, [])
        self.assertEqual(profile.seo_keywords, [])

    def test_profiles_are_isolated_per_location(self):
        one = self.brand(1)
        two = self.brand(2)
        self.assertTrue(one.complete and two.complete)
        self.assertIn("Garden Grove", one.business_name)
        self.assertNotIn("Garden Grove", two.business_name)
        self.assertNotIn("Westminster", one.city)
        self.assertEqual(two.language, "vi")

    def test_missing_location_never_borrows_another_shop(self):
        neutral = self.brand(999)
        merged = neutral.merge_for_prompt()
        self.assertEqual(merged["business_name"], "")
        self.assertNotIn("Garden Grove", json.dumps(merged))
        self.assertNotIn("Westminster", json.dumps(merged))

    def test_merge_for_prompt_is_empty_safe(self):
        merged = BrandProfile.neutral().merge_for_prompt()
        for key in (
            "location_id", "business_name", "city", "state", "category", "services",
            "language", "tone", "seo_keywords", "do_not_mention", "rules",
            "auto_publish_allowed", "complete",
        ):
            self.assertIn(key, merged)
        self.assertEqual(merged.get("business_name", ""), "")

    def test_partial_profile_is_incomplete_and_masked(self):
        path = os.path.join(self.tmp.name, "partial.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"profiles": [{"location_id": 7, "business_name": "Half Shop"}]}, fh)
        profile = BrandLoader.from_file(path).get_for(7)
        self.assertFalse(profile.complete)
        self.assertEqual(profile.merge_for_prompt()["business_name"], "")

    def test_loads_real_project_fixture_without_cross_shop_mix(self):
        loader = BrandLoader.from_file(FIXTURE_BRANDS)
        one = loader.get_for("accounts/1001/locations/loc-001")
        five = loader.get_for("accounts/1001/locations/loc-005")
        self.assertTrue(one.complete and five.complete)
        self.assertEqual(one.city, "Garden Grove")
        self.assertEqual(five.city, "Westminster")
        self.assertNotIn("Garden Grove", five.merge_for_prompt()["business_name"])

    def test_module_level_loader_helper(self):
        profiles = load_brand_profiles(self.brand_path)
        self.assertIn("1", profiles)
        self.assertIn("2", profiles)


# ====================================================================== #
# classification
# ====================================================================== #
class ClassificationTests(unittest.TestCase):
    def test_star_words_parse(self):
        for word, value in (("ONE", 1), ("TWO", 2), ("THREE", 3), ("FOUR", 4), ("FIVE", 5)):
            self.assertEqual(classify({"starRating": word, "comment": "ok detail here"}).stars, value)

    def test_rating_only_detection(self):
        result = classify({"starRating": "FIVE", "comment": None})
        self.assertTrue(result.is_rating_only)
        self.assertFalse(result.requires_manual_approval)
        self.assertTrue(classify({"starRating": "THREE", "comment": "   "}).is_rating_only)

    def test_requires_manual_approval_matrix(self):
        cases = [
            (5, "Great gel manicure, loved it", False),
            (4, "Nice pedicure, a bit rushed", False),
            (3, "Okay manicure overall", False),
            (2, "Bad pedicure", True),
            (1, "Terrible service", True),
        ]
        for stars, comment, expected in cases:
            result = classify({"starRating": None, "rating": stars, "comment": comment})
            self.assertEqual(result.requires_manual_approval, expected, (stars, comment))

    def test_rating_only_low_stars_requires_manual(self):
        self.assertTrue(classify({"rating": 4, "comment": ""}).requires_manual_approval)
        self.assertTrue(classify({"rating": 3, "comment": ""}).requires_manual_approval)
        self.assertFalse(classify({"rating": 5, "comment": ""}).requires_manual_approval)

    def test_sensitive_signals_detected(self):
        with open(FIXTURE_SENSITIVE, "r", encoding="utf-8") as fh:
            reviews = json.load(fh)["reviews"]
        expected = {
            "r-3001": "health",
            "r-3002": "discrimination",
            "r-3003": "threat_abuse",
            "r-3004": "legal_refund",
            "r-3005": "pii",
            "r-3006": "spam",
        }
        for review in reviews:
            result = classify(review)
            self.assertTrue(result.is_sensitive, review["reviewId"])
            self.assertIn(expected[review["reviewId"]], result.signals)
            self.assertTrue(result.requires_manual_approval)

    def test_staff_name_mention_is_not_itself_sensitive(self):
        result = classify({"rating": 5, "comment": "Anna did a wonderful gel manicure for me"})
        self.assertFalse(result.is_sensitive)
        self.assertFalse(result.requires_manual_approval)


# ====================================================================== #
# model client
# ====================================================================== #
class ModelClientTests(unittest.TestCase):
    def test_protocol_shape(self):
        client = MockModelClient()
        self.assertTrue(isinstance(client, ModelClient))
        self.assertEqual(client.complete("s", "u"), R("Thank you for your feedback."))

    def test_mock_scripted_sequence_then_repeat(self):
        client = MockModelClient(responses=[R("first"), R("second")])
        self.assertEqual(client.complete("s", "u"), R("first"))
        self.assertEqual(client.complete("s", "u"), R("second"))
        self.assertEqual(client.complete("s", "u"), R("second"))

    def test_mock_can_raise_timeout_and_api_error(self):
        with self.assertRaises(TimeoutError):
            MockModelClient(raise_on_call=TimeoutError("slow")).complete("s", "u")
        with self.assertRaises(ApiError):
            MockModelClient(responses=[ApiError("boom")]).complete("s", "u")

    def test_mock_can_return_invalid_json(self):
        self.assertEqual(MockModelClient(responses=["not json at all"]).complete("s", "u"), "not json at all")

    def test_openrouter_builds_request_without_network(self):
        seen = {}

        def transport(payload, timeout):
            seen["payload"] = payload
            seen["timeout"] = timeout
            return {"choices": [{"message": {"content": R("hello from model")}}]}

        client = OpenRouterModelClient(api_key="test-key", transport=transport)
        self.assertEqual(client.complete("sys", "usr"), R("hello from model"))
        self.assertEqual(seen["payload"]["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(seen["payload"]["messages"][0]["role"], "system")
        self.assertEqual(seen["payload"]["messages"][1]["content"], "usr")

    def test_openrouter_without_key_raises(self):
        client = OpenRouterModelClient(api_key="")
        with self.assertRaises(ApiError):
            client.complete("s", "u")


# ====================================================================== #
# duplicate detection
# ====================================================================== #
class DuplicateTests(BaseTestCase):
    def test_exact_duplicate_fails(self):
        text = "Thank you so much for the lovely review about your gel manicure today."
        result = DuplicateChecker().check(text, [{"reply_text": text, "location_id": 1}], location_id=1)
        self.assertEqual(result.status, "fail")
        self.assertIn("exact_duplicate", result.reason())

    def test_jaccard_fail_and_warn(self):
        checker = DuplicateChecker()
        base = "Thank you so much for visiting us and for the kind words about your gel manicure appointment"
        fail_case = base + " it really means the world to our little team downtown"
        warn_case = "We appreciate your visit and the wonderful feedback about the pedicure you had with us"
        self.assertEqual(
            checker.check(fail_case, [{"reply_text": base, "location_id": 1}], location_id=1).status,
            "fail",
        )
        warn_result = checker.check(warn_case, [{"reply_text": base, "location_id": 1}], location_id=1)
        self.assertIn(warn_result.status, ("warn", "pass", "fail"))
        self.assertGreaterEqual(warn_result.max_similarity, 0.0)

    def test_repeated_prefix_fails(self):
        checker = DuplicateChecker()
        old = "Thank you so much for taking the time to share your feedback with us here"
        new = "Thank you so much for taking the time to write something completely different now"
        result = checker.check(new, [{"reply_text": old, "location_id": 1}], location_id=1)
        self.assertEqual(result.status, "fail")
        self.assertIn("repeated_prefix", result.reason())

    def test_scope_limited_to_same_location(self):
        text = "Thank you so much for the lovely review about your gel manicure today."
        other = [{"reply_text": text, "location_id": 2}]
        self.assertEqual(DuplicateChecker().check(text, other, location_id=1).status, "pass")
        self.assertEqual(DuplicateChecker().check(text, other, location_id=2).status, "fail")

    def test_keyword_density_flags_stuffing(self):
        brand = self.brand(1)
        issues = keyword_density_issues(
            "Nail Salon Manicure Gel Nails Garden Grove Garden Grove", brand
        )
        self.assertTrue(any("keyword_stuffing" in i for i in issues))
        self.assertTrue(any("city_repeated" in i for i in issues))


# ====================================================================== #
# validator (7 checks)
# ====================================================================== #
class ValidatorTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.review = {"rating": 5, "starRating": "FIVE", "location_id": 1,
                       "comment": "Loved my gel manicure, the shop was spotless!"}
        self.classification = classify(self.review)

    def test_all_seven_checks_present_and_pass(self):
        draft = "Thank you so much for the kind words about your gel manicure, we are glad you enjoyed the visit."
        result = validate(draft, self.review, self.brand(1), [], self.classification)
        self.assertEqual([c.name for c in result.checks], list(CHECK_NAMES))
        self.assertTrue(result.ok, result.reason())

    def test_no_fabrication_unverified_service(self):
        draft = "Thanks for coming in, we hope your acrylic set is perfect and we appreciate the manicure feedback."
        review = {"rating": 5, "location_id": 1, "comment": "Loved the manicure"}
        result = validate(draft, review, self.brand(2), [], classify(review))
        check = result.by_name("no_fabrication")
        self.assertEqual(check.status, "fail")
        self.assertIn("acrylic", check.reason)

    def test_no_fabrication_promise_fails(self):
        draft = "Sorry about that, we will give you a refund and a free manicure next time."
        result = validate(draft, self.review, self.brand(1), [], self.classification)
        self.assertEqual(result.by_name("no_fabrication").status, "fail")

    def test_no_fabrication_unverified_staff_name(self):
        draft = "Thank you, Jessica really appreciated your gel manicure feedback."
        result = validate(draft, self.review, self.brand(1), [], self.classification)
        self.assertEqual(result.by_name("no_fabrication").status, "fail")
        self.assertIn("Jessica", result.by_name("no_fabrication").reason)

    def test_brand_fit_blocks_do_not_mention(self):
        draft = "Thanks Kim for the gel manicure review, we appreciate it a lot here."
        result = validate(draft, self.review, self.brand(1), [], self.classification)
        self.assertEqual(result.by_name("brand_fit").status, "fail")

    def test_local_seo_fails_on_stuffing(self):
        draft = "Thank you for the gel manicure review, visit our Nail Salon Manicure Gel Nails studio soon."
        result = validate(draft, self.review, self.brand(1), [], self.classification)
        self.assertEqual(result.by_name("local_seo").status, "fail")

    def test_duplication_check_fails_on_repeat(self):
        draft = "Thank you so much for the kind words about your gel manicure, we are glad you enjoyed the visit."
        recent = [{"reply_text": draft, "location_id": 1}]
        result = validate(draft, self.review, self.brand(1), recent, self.classification)
        self.assertEqual(result.by_name("duplication").status, "fail")
        self.assertFalse(result.ok)

    def test_approval_compliance_flags_pii_and_ranking(self):
        draft = "Thanks for the gel manicure review, call me at 555-123-4567 and our Google ranking will improve."
        result = validate(draft, self.review, self.brand(1), [], self.classification)
        self.assertEqual(result.by_name("approval_compliance").status, "fail")

    def test_empty_draft_fails_relevance(self):
        result = validate("", self.review, self.brand(1), [], self.classification)
        self.assertEqual(result.by_name("relevance").status, "fail")
        self.assertFalse(result.ok)


# ====================================================================== #
# generator
# ====================================================================== #
class GeneratorTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.review = {
            "id": 1, "location_id": 1, "starRating": "FIVE",
            "comment": "Loved my gel manicure, the shop was spotless!",
        }
        self.classification = classify(self.review)

    def test_build_prompt_encodes_the_hard_rules(self):
        system, user = ReplyGenerator(MockModelClient(), self.loader, DuplicateChecker()).build_prompt(
            self.review, self.brand(1), self.classification, []
        )
        self.assertIn("DRAFT", system)
        for token in ("language", "template", "staff", "prices", "promotions", "keyword", "ranking", "personal data"):
            self.assertIn(token, system.lower())
        payload = json.loads(user)
        self.assertEqual(payload["reply_language"], "en")
        self.assertEqual(payload["brand"]["verified_services"][:2], ["Manicure", "Pedicure"])
        self.assertIn("gel manicure", payload["review"]["comment"])

    def test_cross_shop_prompt_isolation(self):
        other = {
            "id": 2, "location_id": 2, "starRating": "FIVE",
            "comment": "Dịch vụ tốt, nhân viên dễ thương.",
        }
        _, user = ReplyGenerator(MockModelClient(), self.loader, DuplicateChecker()).build_prompt(
            other, self.brand(2), classify(other), []
        )
        self.assertNotIn("Garden Grove", user)
        self.assertNotIn("Demo Nails", user)
        self.assertIn("Westminster", user)
        self.assertEqual(json.loads(user)["reply_language"], "vi")

    def test_generate_timeout_is_failed_not_raised(self):
        generator = ReplyGenerator(
            MockModelClient(raise_on_call=TimeoutError("timed out")), self.loader, DuplicateChecker()
        )
        draft, meta = generator.generate(self.review, 1)
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["error"], "timeout")
        self.assertEqual(draft, "")

    def test_generate_api_error_is_failed(self):
        generator = ReplyGenerator(MockModelClient(responses=[ApiError("500")]), self.loader, DuplicateChecker())
        _, meta = generator.generate(self.review, 1)
        self.assertEqual((meta["status"], meta["error"]), ("failed", "api_error"))

    def test_generate_invalid_json_is_failed(self):
        generator = ReplyGenerator(MockModelClient(responses=["{not json"]), self.loader, DuplicateChecker())
        _, meta = generator.generate(self.review, 1)
        self.assertEqual((meta["status"], meta["error"]), ("failed", "invalid_json"))

    def test_one_controlled_repair_attempt(self):
        bad = "Thanks, we will give you a refund on your gel manicure."  # promise -> invalid
        good = "Thank you for the kind words about your gel manicure, we are glad you enjoyed it."
        generator = ReplyGenerator(
            MockModelClient(responses=[R(bad), R(good)]), self.loader, DuplicateChecker()
        )
        draft, meta = generator.generate(self.review, 1)
        self.assertEqual(meta["attempts"], 2)
        self.assertTrue(meta["repair_used"])
        self.assertIn("gel manicure", draft)
        self.assertEqual(meta["status"], "ok")

    def test_repair_is_capped_at_one(self):
        bad = R("Thanks, we will give you a refund on your gel manicure.")
        generator = ReplyGenerator(
            MockModelClient(responses=[bad, bad]), self.loader, DuplicateChecker()
        )
        _, meta = generator.generate(self.review, 1)
        self.assertEqual(meta["attempts"], 2)
        self.assertEqual(meta["status"], "needs_review")

    def test_extract_json_tolerates_fences(self):
        self.assertEqual(extract_json('```json\n{"reply": "hi"}\n```'), {"reply": "hi"})
        self.assertEqual(extract_json("garbage [{\"reply\": \"hi\"}] no"), {"reply": "hi"})


# ====================================================================== #
# engine
# ====================================================================== #
class EngineTests(BaseTestCase):
    def test_five_star_with_text_writes_draft(self):
        review = self.add_review(1, "r-1", 5, "Loved my gel manicure, the shop was spotless!")
        engine, _ = self.make_engine([R("Thank you for the kind words about your gel manicure, we are glad you enjoyed it.")])
        out = engine.process(review)
        self.assertEqual(out["status"], "draft")
        self.assertIn("gel manicure", out["draft_text"])
        self.assertNoPublished()

    def test_four_star_with_text(self):
        review = self.add_review(1, "r-2", 4, "Nice manicure but I waited a while.")
        engine, _ = self.make_engine([R("Thank you for the manicure feedback, and sorry about the wait you mentioned.")])
        out = engine.process(review)
        # classification.requires_manual_approval is False for 4 stars with text
        # (task formula), so the draft is queued as a normal draft.
        self.assertEqual(out["status"], "draft")

    def test_three_star_with_text(self):
        review = self.add_review(1, "r-3", 3, "Manicure was okay, nothing special.")
        engine, _ = self.make_engine([R("Thank you for the honest manicure feedback, we would love to hear more.")])
        self.assertEqual(engine.process(review)["status"], "draft")

    def test_one_star_requires_manual_review(self):
        review = self.add_review(1, "r-4", 1, "Terrible pedicure, I was very disappointed.")
        engine, _ = self.make_engine([R("We are sorry about your pedicure experience and would like to make it right.")])
        out = engine.process(review)
        self.assertEqual(out["status"], "needs_review")
        self.assertTrue(out["meta"]["requires_manual_approval"])

    def test_two_star_requires_manual_review(self):
        review = self.add_review(1, "r-5", 2, "Poor manicure, not happy at all.")
        engine, _ = self.make_engine([R("We are sorry you were unhappy with your manicure and want to help.")])
        self.assertEqual(engine.process(review)["status"], "needs_review")

    def test_rating_only_does_not_invent_an_experience(self):
        review = self.add_review(1, "r-6", 5, None)
        engine, _ = self.make_engine([R("Thank you so much for the five star rating, it truly means a lot to us.")])
        out = engine.process(review)
        self.assertEqual(out["status"], "draft")
        lowered = out["draft_text"].lower()
        for word in ("manicure", "pedicure", "gel", "acrylic", "loved your"):
            self.assertNotIn(word, lowered)

    def test_rating_only_low_star_requires_manual_review(self):
        review = self.add_review(1, "r-7", 3, "")
        engine, _ = self.make_engine([R("Thank you for the rating; we would love to hear what we could improve.")])
        out = engine.process(review)
        self.assertEqual(out["status"], "needs_review")
        self.assertTrue(out["meta"]["is_rating_only"])

    def test_sensitive_review_requires_manual_review(self):
        review = self.add_review(
            1, "r-8", 1, "I got an infection after my pedicure and my foot was bleeding."
        )
        engine, _ = self.make_engine([R("We are very sorry; please contact our salon directly so we can help you.")])
        out = engine.process(review)
        self.assertEqual(out["status"], "needs_review")
        self.assertTrue(out["meta"]["is_sensitive"])
        self.assertIn("health", out["meta"]["signals"])

    def test_review_naming_a_service(self):
        review = self.add_review(1, "r-9", 5, "My gel pedicure was beautiful, thank you!")
        engine, _ = self.make_engine([R("Thank you for the lovely words about your gel pedicure, we appreciate it.")])
        out = engine.process(review)
        self.assertEqual(out["status"], "draft")
        self.assertIn("gel pedicure", out["draft_text"])

    def test_review_naming_a_staff_member(self):
        review = self.add_review(1, "r-10", 5, "Anna did a wonderful gel manicure for me.")
        engine, model = self.make_engine([R("Thank you for the kind words about Anna and your gel manicure.")])
        out = engine.process(review)
        self.assertEqual(out["status"], "draft")
        self.assertIn("Anna", out["draft_text"])
        # The do-not-mention term stays in the prompt as a prohibition, but must
        # never appear in the generated reply.
        self.assertIn("Kim", model.last_prompt())
        self.assertNotIn("Kim", out["draft_text"])

    def test_english_review(self):
        review = self.add_review(1, "r-11", 5, "Great manicure and a friendly team!")
        engine, _ = self.make_engine([R("Thank you for the kind words about your manicure and our team.")])
        out = engine.process(review)
        self.assertEqual(out["meta"]["language"], "en")
        self.assertEqual(out["status"], "draft")

    def test_vietnamese_review(self):
        review = self.add_review(2, "r-12", 5, "Dịch vụ tốt, nhân viên dễ thương.")
        engine, model = self.make_engine([R("Cảm ơn bạn đã khen ngợi dịch vụ của tiệm, chúng tôi rất vui.")])
        out = engine.process(review)
        self.assertEqual(out["meta"]["language"], "vi")
        self.assertEqual(out["status"], "draft")
        self.assertIn("Dịch", model.last_prompt())

    def test_short_vague_review(self):
        review = self.add_review(1, "r-13", 5, "ok")
        engine, _ = self.make_engine([R("Thanks for stopping by and leaving a rating, we appreciate it.")])
        self.assertEqual(engine.process(review)["status"], "draft")

    def test_two_shops_never_mix_brand_data(self):
        review_b = self.add_review(2, "r-14", 5, "Dịch vụ tốt, nhân viên dễ thương.")
        engine, model = self.make_engine([R("Cảm ơn bạn đã đánh giá dịch vụ của tiệm, hẹn gặp lại bạn.")])
        out = engine.process(review_b)
        prompt = model.last_prompt()
        for leaked in ("Garden Grove", "Demo Nails", "loc-001"):
            self.assertNotIn(leaked, prompt)
            self.assertNotIn(leaked, out["draft_text"] or "")
        self.assertNoPublished()

    def test_existing_reply_is_not_overwritten(self):
        review = self.add_review(1, "r-15", 5, "Nice manicure!", reply_text="Owner already replied here.")
        engine, model = self.make_engine([R("This must never be used.")])
        out = engine.process(review)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["reason"], "already_has_reply")
        self.assertEqual(model.calls, [])
        self.assertEqual(self.repo.get_reply_drafts(review["id"]), [])
        self.assertEqual(self.repo.get_review(review["id"])["reply_text"], "Owner already replied here.")

    def test_missing_brand_profile_uses_neutral_fallback(self):
        review = self.add_review(3, "r-16", 5, "Great service, thank you!")
        engine, model = self.make_engine([R("Thank you for the kind words about your visit, we appreciate it.")])
        out = engine.process(review)
        self.assertFalse(out["meta"]["brand_complete"])
        self.assertNotIn("Garden Grove", model.last_prompt())
        self.assertNotIn("Demo Nails", model.last_prompt())
        self.assertNotIn("Garden Grove", out["draft_text"] or "")

    def test_duplicate_reply_is_sent_to_review(self):
        repeated = "Thank you for the kind words about your gel manicure, we are glad you enjoyed it."
        self.add_review(1, "r-17-old", 5, "Old review about manicure.", reply_text=repeated)
        review = self.add_review(1, "r-18", 5, "Loved my gel manicure, thank you!")
        engine, _ = self.make_engine([R(repeated)])
        out = engine.process(review)
        self.assertEqual(out["status"], "needs_review")
        self.assertEqual(out["meta"]["duplicate"]["status"], "fail")

    def test_keyword_stuffing_is_sent_to_review(self):
        review = self.add_review(1, "r-19", 5, "Loved my gel manicure, thank you!")
        stuffy = "Thanks for the gel manicure review, visit our Nail Salon Manicure Gel Nails studio."
        engine, _ = self.make_engine([R(stuffy)])
        out = engine.process(review)
        self.assertEqual(out["status"], "needs_review")
        self.assertIn("local_seo", out["meta"]["validation"]["failed"])

    def test_repair_rescues_a_bad_first_draft(self):
        review = self.add_review(1, "r-20", 5, "Loved my gel manicure, thank you!")
        engine, _ = self.make_engine(
            [
                R("Thanks, we will refund you for the gel manicure."),
                R("Thank you for the kind words about your gel manicure, we are glad you enjoyed it."),
            ]
        )
        out = engine.process(review)
        self.assertEqual(out["status"], "draft")
        self.assertEqual(out["meta"]["attempts"], 2)

    def test_model_failures_still_persist_a_needs_review_row(self):
        cases = {
            "timeout": MockModelClient(raise_on_call=TimeoutError("slow")),
            "api_error": MockModelClient(responses=[ApiError("502")]),
            "invalid_json": MockModelClient(responses=["{oops"]),
        }
        for index, (label, model) in enumerate(cases.items(), start=40):
            review = self.add_review(1, f"r-{index}", 5, f"Loved my gel manicure, case {label}.")
            generator = ReplyGenerator(model, self.loader, DuplicateChecker())
            engine = ReviewReplyEngine(self.repo, generator, DuplicateChecker(), self.loader)
            out = engine.process(review)
            self.assertEqual(out["status"], "needs_review", label)
            self.assertEqual(out["meta"]["error"], label)
            drafts = self.repo.get_reply_drafts(review["id"])
            self.assertEqual(drafts[0]["status"], "needs_review")
        self.assertNoPublished()

    def test_audit_log_is_written_per_decision(self):
        review = self.add_review(1, "r-50", 5, "Loved my gel manicure, thank you!")
        engine, _ = self.make_engine([R("Thank you for the kind words about your gel manicure, we appreciate it.")])
        engine.process(review)
        logs = self.repo.get_audit_logs(entity="reply_draft")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["action"], "draft_created")
        self.assertEqual(logs[0]["actor"], "system")
        detail = json.loads(logs[0]["detail_json"])
        self.assertEqual(detail["draft_status"], "draft")

    def test_brand_fixture_keyed_by_location_name_resolves(self):
        """Fixtures key on the GBP location name; the engine must map id->name."""
        loader = BrandLoader.from_file(FIXTURE_BRANDS)
        review = self.add_review(1, "r-70", 5, "Loved my gel manicure, thank you!")
        model = MockModelClient([R("Thank you for the kind words about your gel manicure, we appreciate it.")])
        generator = ReplyGenerator(model, loader, DuplicateChecker())
        engine = ReviewReplyEngine(self.repo, generator, DuplicateChecker(), loader)
        out = engine.process(review)
        self.assertTrue(out["meta"]["brand_complete"])
        self.assertEqual(out["meta"]["brand_key"], "accounts/1001/locations/loc-001")
        self.assertIn("Garden Grove", model.last_prompt())

    def test_no_draft_is_ever_published(self):
        scenarios = [(5, "Loved my gel manicure!"), (1, "Awful pedicure"), (5, None)]
        responses = [
            R("Thank you for the kind words about your gel manicure, we appreciate it."),
            R("We are sorry about your pedicure and would like to help."),
            R("Thank you so much for the five star rating, it means a lot to our team."),
        ]
        for index, (stars, comment) in enumerate(scenarios, start=60):
            review = self.add_review(1, f"r-{index}", stars, comment)
            engine, _ = self.make_engine([responses[index - 60]])
            engine.process(review)
        self.assertNoPublished()
        self.assertTrue(self.repo.get_reply_drafts())


# ====================================================================== #
# Group-4 additions: completeness flags, grounding, length, SEO, language
# ====================================================================== #
class Group4BrandAndPromptTests(BaseTestCase):
    """Brand completeness flags + prompt grounding + grounding failures."""

    def _loader_from(self, profiles):
        path = os.path.join(self.tmp.name, "g4_profiles.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"profiles": profiles}, fh, ensure_ascii=False)
        return BrandLoader.from_file(path)

    def test_completeness_flags_exposed_per_profile(self):
        one = self.brand(1)
        self.assertTrue(one.has_city and one.has_state and one.has_services and one.has_seo_keywords)
        neutral = self.brand(999)
        for flag in ("has_city", "has_state", "has_services", "has_seo_keywords"):
            self.assertFalse(getattr(neutral, flag), flag)

    def test_partial_brand_without_city_has_false_flags(self):
        loader = self._loader_from([{"location_id": "loc-9", "business_name": "Half Nails"}])
        profile = loader.get_for("loc-9")
        self.assertFalse(profile.complete)
        self.assertFalse(profile.has_city)
        self.assertFalse(profile.has_state)

    def test_missing_city_forbids_prompt_and_validator_fails_invented_city(self):
        loader = self._loader_from([{"location_id": "loc-9", "business_name": "Half Nails"}])
        review = {"id": 1, "location_id": "loc-9", "rating": 5,
                  "comment": "Loved my manicure, thank you!"}
        model = MockModelClient(
            [R("Thank you for the kind words, we are located in Houston and glad you visited.")]
        )
        generator = ReplyGenerator(model, loader, DuplicateChecker())
        draft, meta = generator.generate(review, "loc-9")
        # The prompt must forbid inventing a city ...
        self.assertIn("Do NOT mention any city", model.last_prompt())
        payload = json.loads(model.calls[0]["user"])
        self.assertFalse(payload["brand"]["has_city"])
        # ... and the validator must reject a draft that invents one.
        self.assertIn("grounding", meta["validation"]["failed"])
        self.assertEqual(meta["status"], "needs_review")

    def test_missing_services_draft_naming_service_fails_grounding(self):
        loader = self._loader_from([
            {"location_id": "loc-8", "business_name": "Plain Nails", "city": "Reno"}
        ])
        profile = loader.get_for("loc-8")
        self.assertTrue(profile.complete)
        self.assertFalse(profile.has_services)
        review = {"id": 1, "location_id": "loc-8", "rating": 5,
                  "comment": "Great visit, thank you so much!"}
        draft = "Thank you, we are glad you enjoyed your acrylic manicure today."
        result = validate(draft, review, profile, [], classify(review))
        self.assertEqual(result.by_name("grounding").status, "fail")
        self.assertIn("acrylic", result.by_name("grounding").reason)


class Group4ValidatorTests(BaseTestCase):
    """length_ok and local_seo behaviour, plus language preservation."""

    def test_positive_reply_max_three_sentences(self):
        review = {"rating": 5, "location_id": 1, "comment": "Loved my gel manicure!"}
        cls = classify(review)
        ok_draft = "Thank you for the kind words about your gel manicure, we are glad you enjoyed it."
        four = ("Thank you for the kind words. We are glad you enjoyed it. "
                "It means a lot to us. See you next time.")
        self.assertEqual(validate(ok_draft, review, self.brand(1), [], cls).by_name("length_ok").status, "pass")
        self.assertEqual(validate(four, review, self.brand(1), [], cls).by_name("length_ok").status, "fail")

    def test_negative_reply_allows_four_sentences(self):
        review = {"rating": 1, "location_id": 1, "comment": "Terrible pedicure, very disappointed."}
        cls = classify(review)
        four = ("We are sorry about your pedicure experience. It should not have happened. "
                "We would like to help. Please contact us directly.")
        five = four + " Thank you for telling us."
        self.assertEqual(validate(four, review, self.brand(1), [], cls).by_name("length_ok").status, "pass")
        self.assertEqual(validate(five, review, self.brand(1), [], cls).by_name("length_ok").status, "fail")

    def test_seo_stuffing_fails_and_natural_one_keyword_passes(self):
        review = {"rating": 5, "location_id": 1, "comment": "Loved my gel manicure, thank you!"}
        cls = classify(review)
        stuffy = "Thanks for the gel manicure review, visit our Nail Salon Manicure Gel Nails studio."
        natural = "Thank you for the kind words about your manicure, we are glad you enjoyed it."
        self.assertEqual(validate(stuffy, review, self.brand(1), [], cls).by_name("local_seo").status, "fail")
        self.assertEqual(validate(natural, review, self.brand(1), [], cls).by_name("local_seo").status, "pass")

    def test_language_preserved_when_brand_language_unset(self):
        review = {"rating": 5, "location_id": 9, "comment": "Dịch vụ tốt, nhân viên dễ thương."}
        brand = BrandProfile.neutral(9)
        system, user = ReplyGenerator(MockModelClient(), self.loader, DuplicateChecker()).build_prompt(
            review, brand, classify(review), []
        )
        self.assertEqual(json.loads(user)["reply_language"], "vi")
        self.assertIn("Vietnamese", system + user)
        self.assertIn("Write the reply in Vietnamese", user)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
