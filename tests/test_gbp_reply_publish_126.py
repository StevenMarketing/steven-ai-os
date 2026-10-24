"""Issue #126 -- Inc 3b: live reply posting behind the human approval gate.

This increment adds the FIRST Google WRITE in the whole system (one reply PUT),
so the tests here are about the GATES as much as the happy path:

* ``workflow.states`` gains ``publish-intent`` and the terminal ``sent``
  (reachable ONLY from ``publish-intent``); ``replies.states`` gains the single
  ``publish-intent -> sent`` edge;
* the reply-draft CHECK constraint is widened (and pre-#126 databases migrate in
  place, keeping their rows) and ``approval_records`` gains the approval text
  hash ``init_db`` adds additively;
* ``ApprovalService.request_publish`` is the only edge into ``publish-intent``;
* ``PublishService`` enforces state -> approval-hash -> live-location gates
  BEFORE any network call, fails closed offline (``PublishDisabled``), never
  marks a failed publish ``sent`` and is idempotent per draft;
* ``POST /api/drafts/<id>/publish`` is session-scoped (RBAC ``approve``),
  live-gated (409) and posts exactly ONE reply behind a fake transport.

Fully OFFLINE: fake transports only (no socket beyond the loopback test server),
no credential is real and ``GBP_LIVE`` is set only inside a patched environment.

Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest \\
        tests.test_gbp_reply_publish_126
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import RBAC_ROUTE_MATRIX, create_server  # noqa: E402
from gbp.auth.token_manager import Token  # noqa: E402
from gbp.authz import AuthService, UserStore, hash_password  # noqa: E402
from gbp.client.http_client import MockTransport  # noqa: E402
from gbp.oauth.config import LIVE_ENV  # noqa: E402
from gbp.oauth.vault import TOKEN_URL, TokenVault  # noqa: E402
from gbp.oauth.wiring import GoogleConnectWiring  # noqa: E402
from gbp.replies import states as reply_states  # noqa: E402
from gbp.replies.publish import (  # noqa: E402
    PUBLISH_INTENT,
    SENT,
    PublishDisabled,
    PublishService,
)
from gbp.replies.tenancy import ANSWERED_STATES  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.sync.reviews_sync import ReviewSync  # noqa: E402
from gbp.workflow import states as wf_states  # noqa: E402
from gbp.workflow.service import ApprovalService, PublishBlocked, text_hash  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
TEST_SECRET = "gbp-reply-publish-secret-not-used-elsewhere"
TEST_ENV = {"GBP_SESSION_SECRET": TEST_SECRET}
VAULT_KEY = "unit-test-vault-key-material-0123456789abcdef"
CANONICAL_REDIRECT = "https://gbp.adzdigital.com/api/google/callback"

ACCOUNT = "accounts/1001"
LOC_A = "accounts/1001/locations/loc-001"     # the tenant anchor (a fixture row)
SHORT_1 = "locations/777001"
FULL_1 = "accounts/1001/locations/777001"
REVIEW_NAME = f"{FULL_1}/reviews/r1"
REPLY_URL = f"https://mybusiness.googleapis.com/v4/{REVIEW_NAME}/reply"

PASSWORD = "PublishSecret!"

APPROVED_TEXT = "Thank you so much for visiting — we hope to see you again!"


def full_env(vault_dir: str, **over: str) -> dict:
    env = {
        LIVE_ENV: "1",
        "GBP_OAUTH_CLIENT_ID": "client-id-probe",
        "GBP_OAUTH_CLIENT_SECRET": "client-secret-probe",
        "GBP_OAUTH_REDIRECT_URI": CANONICAL_REDIRECT,
        "GBP_TOKEN_ENCRYPTION_KEY": VAULT_KEY,
        "GBP_TOKEN_VAULT_DIR": vault_dir,
    }
    env.update(over)
    return env


def make_repo() -> Repository:
    repo = Repository(":memory:")
    repo.init_db(SCHEMA_PATH)
    return repo


def token_transport() -> MockTransport:
    transport = MockTransport()
    transport.register("POST", TOKEN_URL, {
        "access_token": "access-token-probe",
        "refresh_token": "refresh-token-probe",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/business.manage",
        "token_type": "Bearer",
    }, status=200)
    return transport


def api_transport() -> MockTransport:
    """A fake GBP API: one live location whose review can be replied to."""
    transport = MockTransport()
    transport.register(
        "GET", "https://mybusiness.googleapis.com/v1/accounts",
        {"accounts": [{"name": ACCOUNT, "accountName": "Acme Group",
                       "type": "LOCATION_GROUP"}]})
    transport.register(
        "GET", f"https://mybusiness.googleapis.com/v1/{ACCOUNT}/locations",
        {"locations": [{"name": SHORT_1, "title": "Live Nails A",
                        "storefrontAddress": {"postalCode": "94103",
                                              "locality": "Garden Grove",
                                              "administrativeArea": "CA"}}]})
    transport.register(
        "GET", f"https://mybusiness.googleapis.com/v4/{FULL_1}/reviews",
        {"reviews": [{
            "name": REVIEW_NAME, "rating": 5, "comment": "Loved it",
            "createTime": "2026-10-19T09:00:00Z",
            "updateTime": "2026-10-20T10:00:00Z",
            "reviewer": {"displayName": "Ada"},
        }]})
    # The ONE write: the reply PUT.
    transport.register(
        "PUT", REPLY_URL,
        {"name": REPLY_URL, "comment": APPROVED_TEXT,
         "updateTime": "2026-10-24T12:00:00Z"}, status=200)
    return transport


class RecordingPublisher:
    """A publish callable that records every call (and can fail on demand)."""

    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response if response is not None else {
            "updateTime": "2026-10-24T12:00:00Z"}
        self.error = error

    def __call__(self, review_name, comment):
        self.calls.append((review_name, comment))
        if self.error is not None:
            raise self.error
        return dict(self.response)


# ===================================================================== #
# state machine (pure)
# ===================================================================== #
class StateMachineTests(unittest.TestCase):
    def test_workflow_gains_publish_intent_and_terminal_sent(self):
        self.assertIn("publish-intent", wf_states.STATES)
        self.assertIn("sent", wf_states.STATES)
        wf_states.assert_transition("approved", "publish-intent")
        wf_states.assert_transition("ready_to_publish", "publish-intent")
        wf_states.assert_transition("publish-intent", "sent")
        # ``sent`` is terminal and reachable ONLY from publish-intent.
        self.assertEqual(wf_states.ALLOWED["sent"], frozenset())
        for src in ("draft", "needs_review", "rejected", "approved",
                    "ready_to_publish", "published", "failed"):
            with self.assertRaises(wf_states.InvalidTransition):
                wf_states.assert_transition(src, "sent")

    def test_may_publish_is_unchanged_so_the_guard_stays_a_dry_run(self):
        self.assertTrue(wf_states.may_publish("approved"))
        self.assertTrue(wf_states.may_publish("ready_to_publish"))
        self.assertFalse(wf_states.may_publish("publish-intent"))
        self.assertFalse(wf_states.may_publish("sent"))

    def test_replies_states_adds_exactly_the_sent_edge(self):
        self.assertIn("sent", reply_states.STATES)
        self.assertTrue(reply_states.can_transition("publish-intent", "sent"))
        self.assertFalse(reply_states.can_transition("approved", "sent"))
        self.assertFalse(reply_states.can_transition("sent", "draft"))

    def test_tenancy_counts_sent_as_answered(self):
        self.assertIn("sent", ANSWERED_STATES)


# ===================================================================== #
# schema migration
# ===================================================================== #
class MigrationTests(unittest.TestCase):
    def test_new_states_are_accepted_by_the_check_constraint(self):
        repo = make_repo()
        try:
            acct = repo.upsert_account(ACCOUNT)
            loc = repo.upsert_location(acct, FULL_1)
            rid = repo.upsert_review(loc, REVIEW_NAME, rating=5, comment="x")
            did = repo.insert_reply_draft(rid, draft_text="hi", status="approved")
            for state in (PUBLISH_INTENT, SENT):
                repo.conn.execute(
                    "UPDATE reply_drafts SET status = ? WHERE id = ?", (state, did))
            with self.assertRaises(Exception):
                repo.conn.execute(
                    "UPDATE reply_drafts SET status = 'nonsense' WHERE id = ?", (did,))
        finally:
            repo.close()

    def test_approval_records_carry_a_text_hash_column(self):
        repo = make_repo()
        try:
            cols = {r[1] for r in repo.conn.execute(
                "PRAGMA table_info(approval_records)")}
            self.assertIn("text_hash", cols)
        finally:
            repo.close()

    def test_a_pre_126_database_migrates_in_place_and_keeps_its_rows(self):
        repo = Repository(":memory:")
        try:
            repo.conn.executescript(
                "CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " location_id INTEGER, review_name TEXT NOT NULL UNIQUE,"
                " rating INTEGER, comment TEXT, reply_text TEXT,"
                " status TEXT, update_time TEXT);"
                "CREATE TABLE reply_drafts ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER NOT NULL,"
                " draft_text TEXT, status TEXT NOT NULL DEFAULT 'draft'"
                "   CHECK (status IN ('draft','needs_review','approved','rejected',"
                "                     'ready_to_publish','published','failed')),"
                " version INTEGER NOT NULL DEFAULT 1,"
                " created_at TEXT, updated_at TEXT);"
                "CREATE TABLE approval_records ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL,"
                " action TEXT, actor TEXT, decided_at TEXT, note TEXT);"
                "INSERT INTO reviews(id, review_name) VALUES (1, 'r1');"
                "INSERT INTO reply_drafts(id, review_id, draft_text, status, version)"
                " VALUES (7, 1, 'kept text', 'approved', 3);"
                "INSERT INTO approval_records(draft_id, action, actor) VALUES (7, 'approve', 'owner');"
            )
            repo.conn.commit()
            repo.init_db(SCHEMA_PATH)
            row = repo.conn.execute(
                "SELECT id, draft_text, status, version FROM reply_drafts").fetchone()
            self.assertEqual((int(row["id"]), row["draft_text"], row["status"],
                              int(row["version"])),
                             (7, "kept text", "approved", 3))
            # The approval child row was NOT cascade-deleted by the table swap.
            self.assertEqual(repo.conn.execute(
                "SELECT COUNT(*) AS n FROM approval_records").fetchone()["n"], 1)
            self.assertIn("text_hash", {r[1] for r in repo.conn.execute(
                "PRAGMA table_info(approval_records)")})
            # ...and the widened CHECK now accepts the new states.
            repo.conn.execute(
                "UPDATE reply_drafts SET status = 'sent' WHERE id = 7")
            repo.conn.commit()
        finally:
            repo.close()


# ===================================================================== #
# ApprovalService.request_publish (offline)
# ===================================================================== #
class RequestPublishTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        acct = self.repo.upsert_account(ACCOUNT, tenant_id=LOC_A)
        self.loc = self.repo.upsert_location(
            acct, SHORT_1, title="Live Nails A", tenant_id=LOC_A, source="live")
        self.rid = self.repo.upsert_review(
            self.loc, REVIEW_NAME, rating=5, comment="Loved it")
        self.did = self.repo.insert_reply_draft(
            self.rid, draft_text=APPROVED_TEXT, status="draft")
        self.service = ApprovalService(self.repo)

    def tearDown(self):
        self.repo.close()

    def _status(self):
        return self.repo.conn.execute(
            "SELECT status FROM reply_drafts WHERE id = ?", (self.did,)).fetchone()["status"]

    def test_only_an_approved_draft_may_request_publish(self):
        with self.assertRaises(PublishBlocked):
            self.service.request_publish(self.did, "owner")
        self.assertEqual(self._status(), "draft")

    def test_approve_then_request_publish_reaches_publish_intent(self):
        self.service.approve(self.did, "owner")
        self.assertEqual(self._status(), "approved")
        result = self.service.request_publish(self.did, "owner")
        self.assertEqual(result["status"], PUBLISH_INTENT)
        self.assertEqual(self._status(), PUBLISH_INTENT)
        # The approval row records the exact-text hash.
        row = self.repo.conn.execute(
            "SELECT text_hash FROM approval_records WHERE action = 'approve'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["text_hash"], text_hash(APPROVED_TEXT))

    def test_re_requesting_is_a_no_op(self):
        self.service.approve(self.did, "owner")
        self.service.request_publish(self.did, "owner")
        before = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM approval_records").fetchone()["n"]
        again = self.service.request_publish(self.did, "owner")
        self.assertTrue(again["idempotent"])
        self.assertEqual(self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM approval_records").fetchone()["n"], before)


# ===================================================================== #
# PublishService gate matrix (offline, recording publisher)
# ===================================================================== #
class PublishServiceBase(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        acct = self.repo.upsert_account(ACCOUNT, tenant_id=LOC_A)
        self.loc = self.repo.upsert_location(
            acct, SHORT_1, title="Live Nails A", tenant_id=LOC_A, source="live")
        self.rid = self.repo.upsert_review(
            self.loc, REVIEW_NAME, rating=5, comment="Loved it")
        self.did = self.repo.insert_reply_draft(
            self.rid, draft_text=APPROVED_TEXT, status="draft")
        self.service = ApprovalService(self.repo)
        self.publisher = RecordingPublisher()
        self.publish = PublishService(self.repo, self.service)
        self.tenant = LOC_A

    def tearDown(self):
        self.repo.close()

    def _approve_and_request(self):
        self.service.approve(self.did, "owner")
        self.service.request_publish(self.did, "owner")

    def _status(self):
        return self.repo.conn.execute(
            "SELECT status FROM reply_drafts WHERE id = ?", (self.did,)).fetchone()["status"]

    def _publish(self, **kw):
        kw.setdefault("tenant", self.tenant)
        kw.setdefault("publisher", self.publisher)
        return self.publish.publish(self.did, "owner", **kw)


class GateMatrixTests(PublishServiceBase):
    def test_not_approved_is_refused_with_zero_puts(self):
        result = self._publish()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "state_not_publish_intent")
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(self._status(), "draft")

    def test_needs_review_is_refused_with_zero_puts(self):
        self.repo.conn.execute(
            "UPDATE reply_drafts SET status = 'needs_review' WHERE id = ?", (self.did,))
        self.repo.conn.commit()
        result = self._publish()
        self.assertEqual(result["reason"], "state_not_publish_intent")
        self.assertEqual(self.publisher.calls, [])

    def test_editing_after_approval_is_refused_with_zero_puts(self):
        self._approve_and_request()
        # An edit AFTER approval changes the text; the recorded hash no longer
        # covers it, so the publish must fail closed.
        self.service.edit_draft(self.did, "Sneaky replacement text", "owner")
        result = self._publish()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "approval_text_mismatch")
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(self._status(), PUBLISH_INTENT)

    def test_wrong_tenant_is_refused_with_zero_puts(self):
        self._approve_and_request()
        result = self._publish(tenant="accounts/999/locations/other")
        self.assertEqual(result["reason"], "review_not_live_for_tenant")
        self.assertEqual(self.publisher.calls, [])

    def test_a_non_live_location_is_refused_with_zero_puts(self):
        self._approve_and_request()
        self.repo.conn.execute(
            "UPDATE business_locations SET source = 'fixture' WHERE id = ?", (self.loc,))
        self.repo.conn.commit()
        result = self._publish()
        self.assertEqual(result["reason"], "review_not_live_for_tenant")
        self.assertEqual(self.publisher.calls, [])

    def test_no_publisher_fails_closed_and_writes_nothing(self):
        self._approve_and_request()
        with self.assertRaises(PublishDisabled):
            self.publish.publish(self.did, "owner", tenant=self.tenant)
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(self._status(), PUBLISH_INTENT)

    def test_unknown_draft_is_not_found(self):
        from gbp.workflow.service import NotFoundError

        with self.assertRaises(NotFoundError):
            self.publish.publish(999999, "owner", tenant=self.tenant,
                                 publisher=self.publisher)
        self.assertEqual(self.publisher.calls, [])


class PublishSuccessTests(PublishServiceBase):
    def test_success_posts_exactly_one_put_and_lands_in_sent(self):
        self._approve_and_request()
        result = self._publish()
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], SENT)
        # Exactly ONE call, to the review's full v4 name, with the approved text.
        self.assertEqual(self.publisher.calls, [(REVIEW_NAME, APPROVED_TEXT)])
        self.assertEqual(self._status(), SENT)
        # The review now reads as answered.
        row = self.repo.conn.execute(
            "SELECT reply_text, reply_update_time FROM reviews WHERE id = ?",
            (self.rid,)).fetchone()
        self.assertEqual(row["reply_text"], APPROVED_TEXT)
        self.assertTrue(row["reply_update_time"])
        # An audit entry recorded the publish -- and never a token.
        audit = self.repo.conn.execute(
            "SELECT action, detail_json FROM audit_logs WHERE action = 'reply_published'"
        ).fetchall()
        self.assertEqual(len(audit), 1)
        self.assertNotIn("token", audit[0]["detail_json"].lower())

    def test_second_publish_of_a_sent_draft_is_a_true_no_op(self):
        self._approve_and_request()
        first = self._publish()
        second = self._publish()
        self.assertTrue(first["ok"] and second["ok"])
        self.assertTrue(second.get("idempotent"))
        self.assertEqual(len(self.publisher.calls), 1)   # NO second PUT

    def test_google_failure_leaves_publish_intent_and_never_marks_sent(self):
        self._approve_and_request()
        failing = RecordingPublisher(error=RuntimeError("boom"))
        result = self.publish.publish(
            self.did, "owner", tenant=self.tenant, publisher=failing)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], PUBLISH_INTENT)
        self.assertEqual(result["reason"], "RuntimeError")   # a SAFE reason only
        self.assertEqual(self._status(), PUBLISH_INTENT)
        self.assertEqual(len(failing.calls), 1)
        # The review was NOT marked answered.
        row = self.repo.conn.execute(
            "SELECT reply_text FROM reviews WHERE id = ?", (self.rid,)).fetchone()
        self.assertIsNone(row["reply_text"])

    def test_publish_guard_stays_a_dry_run(self):
        # The guard still refuses a publish-intent row and performs no write.
        self._approve_and_request()
        with self.assertRaises(PublishBlocked):
            self.service.publish_guard(self.did)


class ResyncTests(PublishServiceBase):
    def test_a_resync_does_not_resurrect_a_sent_reply(self):
        self._approve_and_request()
        self._publish()
        # A later sync of the same location (the fake API omits any reply) must
        # not wipe the posted reply or flip the draft back.
        class FakeClient:
            def list_reviews(self, location, page_token=None):
                return {"reviews": [{
                    "name": REVIEW_NAME, "rating": 5, "comment": "Loved it",
                    "updateTime": "2026-10-20T10:00:00Z"}]}

        ReviewSync(self.repo, FakeClient()).sync_location(
            FULL_1, location_row_id=self.loc)
        row = self.repo.conn.execute(
            "SELECT reply_text FROM reviews WHERE id = ?", (self.rid,)).fetchone()
        self.assertEqual(row["reply_text"], APPROVED_TEXT)
        self.assertEqual(self._status(), SENT)


# ===================================================================== #
# HTTP: POST /api/drafts/<id>/publish
# ===================================================================== #
class PublishRouteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gbp-reply-publish-")
        self.repo = make_repo()
        acct = self.repo.upsert_account(ACCOUNT, tenant_id=LOC_A)
        self.loc_a = self.repo.upsert_location(
            acct, SHORT_1, title="Live Nails A", tenant_id=LOC_A, source="live")
        # A second, NON-live location for the "not live" refusal.
        self.loc_fixture = self.repo.upsert_location(
            acct, "accounts/1001/locations/fixture", title="Fixture")
        self.rid = self.repo.upsert_review(
            self.loc_a, REVIEW_NAME, rating=5, comment="Loved it")
        self.draft_id = self.repo.insert_reply_draft(
            self.rid, draft_text=APPROVED_TEXT, status="draft")

        seed = UserStore(self.repo.conn)
        seed.create_user("admin", hash_password(PASSWORD), ["admin"], [])
        seed.create_user("owner1", hash_password(PASSWORD), ["owner"],
                         [str(self.loc_a)])

        self.service = ApprovalService(self.repo)
        self.auth = AuthService(self.repo.conn, env=TEST_ENV, ttl=3600)
        self.token_transport = token_transport()
        self.api_transport = api_transport()
        self.wiring = GoogleConnectWiring(
            token_transport=self.token_transport, api_transport=self.api_transport)
        self.server = create_server(
            self.service, port=0, auth=self.auth, rate_limit=100000,
            login_limit=100000, google_oauth=self.wiring)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repo.close()

    # -- helpers --------------------------------------------------------- #
    def _req(self, method, path, body=None, cookie=None, csrf=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(payload))
        if cookie:
            hdrs["Cookie"] = cookie
        if csrf:
            hdrs["X-CSRF-Token"] = csrf
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else None
            return resp.status, dict(resp.getheaders()), parsed
        finally:
            conn.close()

    def _login(self, username):
        status, headers, body = self._req(
            "POST", "/api/login", {"username": username, "password": PASSWORD})
        set_cookie = headers.get("Set-Cookie", "")
        return (status,
                set_cookie.split(";")[0] if set_cookie else None,
                (body or {}).get("csrf_token") if isinstance(body, dict) else None)

    def _save_token(self):
        TokenVault(self.tmp, VAULT_KEY).save(LOC_A, Token(
            access_token="access-token-probe", refresh_token="refresh-token-probe",
            expires_at=1e12, scopes=["scope/a"]))

    def _approve(self, actor="admin"):
        self.service.approve(self.draft_id, actor)

    def _puts(self):
        return [c for c in self.api_transport.calls if c["method"] == "PUT"]

    def _status(self):
        return self.repo.conn.execute(
            "SELECT status FROM reply_drafts WHERE id = ?",
            (self.draft_id,)).fetchone()["status"]


class PublishRouteTests(PublishRouteBase):
    def test_end_to_end_publish_posts_one_reply_and_marks_it_sent(self):
        self._approve()
        self._save_token()
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            _s, cookie, csrf = self._login("admin")
            status, _h, body = self._req(
                "POST", f"/api/drafts/{self.draft_id}/publish", {},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 200, body)
            self.assertTrue(body["ok"], body)
            self.assertEqual(body["state"], SENT)
            self.assertNotIn("access-token-probe", json.dumps(body))

            # Exactly ONE PUT, to the correct v4 reply URL, with the approved text.
            puts = self._puts()
            self.assertEqual(len(puts), 1)
            self.assertEqual(puts[0]["url"], REPLY_URL)
            self.assertEqual(puts[0]["data"], {"comment": APPROVED_TEXT})

            self.assertEqual(self._status(), SENT)

            # The review now reads as answered through the existing projection.
            _s2, admin_cookie, _c = self._login("admin")
            status, _h, page = self._req(
                "GET", "/api/reviews?unanswered=1", cookie=admin_cookie)
            self.assertEqual(status, 200)
            comments = [i["comment"] for i in page["items"]]
            self.assertNotIn("Loved it", comments)

            # A SECOND publish call returns the recorded result, NO second PUT.
            status, _h, again = self._req(
                "POST", f"/api/drafts/{self.draft_id}/publish", {},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 200, again)
            self.assertTrue(again["ok"])
            self.assertEqual(len(self._puts()), 1)

    def test_refuses_honestly_when_live_mode_is_off(self):
        self._approve()
        _s, cookie, csrf = self._login("admin")   # no GBP_LIVE in the env
        status, _h, body = self._req(
            "POST", f"/api/drafts/{self.draft_id}/publish", {},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 409)
        self.assertEqual(body.get("error"), "live_not_configured")
        self.assertEqual(self._puts(), [])

    def test_refuses_a_draft_that_is_not_human_approved(self):
        self._save_token()
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            _s, cookie, csrf = self._login("admin")
            status, _h, body = self._req(
                "POST", f"/api/drafts/{self.draft_id}/publish", {},
                cookie=cookie, csrf=csrf)
            self.assertEqual(status, 423)
            self.assertEqual(body.get("error"), "publish_blocked")
            self.assertIn("state_not_publishable", json.dumps(body))
            self.assertEqual(self._puts(), [])
            self.assertEqual(self._status(), "draft")

    def test_refuses_a_location_that_is_not_live(self):
        # A draft on the fixture (non-live) location.
        rid = self.repo.upsert_review(
            self.loc_fixture, "reviews/fixture-1", rating=5, comment="Fixture review")
        did = self.repo.insert_reply_draft(
            rid, draft_text=APPROVED_TEXT, status="draft")
        self.service.approve(did, "admin")
        self._save_token()
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            _s, cookie, csrf = self._login("admin")
            status, _h, body = self._req(
                "POST", f"/api/drafts/{did}/publish", {}, cookie=cookie, csrf=csrf)
            self.assertEqual(status, 409)
            self.assertEqual(body.get("error"), "not_live_location")
            self.assertEqual(self._puts(), [])

    def test_is_session_scoped_and_csrf_guarded(self):
        self._approve()
        self._save_token()
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            # No session -> 401.
            status, _h, _b = self._req(
                "POST", f"/api/drafts/{self.draft_id}/publish", {})
            self.assertEqual(status, 401)
            _s, cookie, _csrf = self._login("admin")
            # Session without CSRF -> refused.
            status, _h, body = self._req(
                "POST", f"/api/drafts/{self.draft_id}/publish", {}, cookie=cookie)
            self.assertEqual(status, 403)
            self.assertEqual(body.get("error"), "csrf_failed")
            self.assertEqual(self._puts(), [])

    def test_a_scoped_user_cannot_publish_another_locations_draft(self):
        # loc_b is owned by nobody in this test; use a fixture location outside
        # the owner's scope and confirm RBAC refuses before any write.
        acct = self.repo.conn.execute(
            "SELECT account_id FROM business_locations WHERE id = ?",
            (self.loc_fixture,)).fetchone()["account_id"]
        other = self.repo.upsert_location(
            acct, "accounts/1001/locations/other", title="Other",
            tenant_id=LOC_A, source="live")
        rid = self.repo.upsert_review(
            other, "accounts/1001/locations/other/reviews/r9", rating=5, comment="x")
        did = self.repo.insert_reply_draft(
            rid, draft_text=APPROVED_TEXT, status="draft")
        self.service.approve(did, "admin")
        self._save_token()
        with mock.patch.dict(os.environ, full_env(self.tmp), clear=True):
            _s, cookie, csrf = self._login("owner1")
            status, _h, _b = self._req(
                "POST", f"/api/drafts/{did}/publish", {}, cookie=cookie, csrf=csrf)
            self.assertEqual(status, 403)
            self.assertEqual(self._puts(), [])

    def test_route_is_in_the_rbac_matrix_with_approve(self):
        self.assertEqual(RBAC_ROUTE_MATRIX[("POST", "/api/drafts/<id>/publish")],
                         "approve")


if __name__ == "__main__":
    unittest.main()
