"""Blocker 3 tests: redacted status publishing + reader-side freshness."""
import json
import unittest

from gbp.bridge.publisher import (
    STATUS_ISSUE_TITLE,
    age_seconds,
    build_redacted_document,
    find_status_issue,
    publish_status,
    render_comment,
)

REPO = "StevenMarketing/steven-ai-os"


def doc():
    return build_redacted_document(
        generated_at="2027-01-15T08:00:00Z",
        worker={"state": "idle", "last_run": "2027-01-15T07:59:00Z",
                "note": "token sk-or-v1-abcdef1234567890 must never be published"},
        cron={"state": "scheduled", "schedule": "every 1m"},
        budget={"spend_today": 0.29, "cap": 0.60},
        repository=REPO,
    )


class DocumentTests(unittest.TestCase):
    def test_no_frozen_age_is_published(self):
        d = doc()
        self.assertNotIn("document_age_seconds", d.get("freshness", {}))
        self.assertTrue(d["freshness"]["recomputed_by_reader"])
        self.assertIn("generated_at", d)

    def test_secret_shaped_text_is_redacted(self):
        blob = json.dumps(doc())
        self.assertNotIn("sk-or-v1-abcdef1234567890", blob)
        # the repository redactor replaces the whole token; accept any clear masking
        self.assertTrue("[REDACTED]" in blob or "sk-" in blob, blob)

    def test_reader_recomputes_age(self):
        from datetime import datetime, timezone

        now = datetime(2027, 1, 15, 8, 5, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(age_seconds(doc(), now), 300.0, places=3)

    def test_comment_carries_parsable_json(self):
        text = render_comment(doc())
        payload = text.split("```json")[1].split("```")[0]
        self.assertEqual(json.loads(payload)["schema"], "steven-ai-os/worker-status@1")


class PublisherTests(unittest.TestCase):
    def test_dry_run_validates_without_writing(self):
        calls = []

        def request(*a, **k):
            calls.append(a)
            raise AssertionError("dry run must not touch the API")

        out = publish_status(request, "tok", REPO, doc(), dry_run=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["code"], "dry_run")
        self.assertEqual(calls, [])

    def test_live_publish_creates_or_reuses_the_status_issue(self):
        posted = []

        def request(method, path, token=None, body=None):
            if path.endswith("/issues?state=open&per_page=100"):
                return ([{"number": 77, "title": STATUS_ISSUE_TITLE}], {})
            if path.endswith("/issues/77/comments"):
                posted.append(body["body"])
                return ({"html_url": "https://github.com/x/y/issues/77#c1"}, {})
            raise AssertionError(path)

        out = publish_status(request, "tok", REPO, doc(), dry_run=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["issue"], 77)
        self.assertEqual(out["attempts"], 1)
        self.assertIn("generated_at", posted[0])
        self.assertNotIn("sk-or-v1-abcdef1234567890", posted[0])

    def test_creates_the_issue_when_absent(self):
        created = {}

        def request(method, path, token=None, body=None):
            if path.endswith("?state=open&per_page=100"):
                return ([], {})
            if path.endswith("/issues") and method == "POST":
                created["issue"] = body["title"]
                return ({"number": 12}, {})
            if path.endswith("/issues/12/comments"):
                return ({"html_url": "u"}, {})
            raise AssertionError(path)

        out = publish_status(request, "tok", REPO, doc(), dry_run=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["issue"], 12)
        self.assertEqual(created["issue"], STATUS_ISSUE_TITLE)

    def test_transient_failure_is_retried_then_reported(self):
        state = {"n": 0}

        def flaky(method, path, token=None, body=None):
            if path.endswith("?state=open&per_page=100"):
                return ([{"number": 5, "title": STATUS_ISSUE_TITLE}], {})
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("502 from GitHub")
            return ({"html_url": "u"}, {})

        out = publish_status(flaky, "tok", REPO, doc(), dry_run=False, attempts=3)
        self.assertTrue(out["ok"])
        self.assertEqual(out["attempts"], 2)

    def test_persistent_failure_is_structured_and_never_raises(self):
        def broken(*a, **k):
            raise RuntimeError("down")

        out = publish_status(broken, "tok", REPO, doc(), dry_run=False, attempts=2)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "publish_failed")
        self.assertIn("down", out["detail"])

    def test_invalid_document_is_refused(self):
        out = publish_status(lambda *a, **k: None, "tok", REPO, {"worker": {}}, dry_run=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "invalid_document")

    def test_find_status_issue_matches_exact_title(self):
        def request(*a, **k):
            return ([{"number": 3, "title": "other"}, {"number": 9, "title": STATUS_ISSUE_TITLE}], {})

        self.assertEqual(find_status_issue(request, "tok", REPO), 9)
