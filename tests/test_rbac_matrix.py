"""Group 8A / T23 -- RBAC matrix + per-location enforcement + IDOR tests.

Standard library only, fully offline. HTTP tests bind 127.0.0.1 on an ephemeral
port; every request goes through the REAL login path (POST /api/login ->
AuthService session cookie + CSRF). Run with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v

The role x route matrix is driven by :meth:`RbacMatrixBase.route_table` so a
route that stops enforcing authorization fails LOUDLY (an expected 403 arrives
as 200). Coverage: admin/owner/reviewer x every location-bearing route, IDOR by
guessed id (DB row asserted unchanged), cross-location list leakage, session
beats any client-supplied actor/role, reviewer cannot reach admin-only routes,
unauthenticated -> 401, inactive -> 403.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import unittest
import uuid

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import (  # noqa: E402
    RBAC_ROUTE_MATRIX,
    SESSION_COOKIE,
    create_server,
)
from gbp.authz import AuthService, UserStore, hash_password  # noqa: E402
from gbp.authz.auth import ROLE_PERMISSIONS  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
TEST_SECRET = "rbac-matrix-secret-not-used-elsewhere"
TEST_ENV = {"GBP_SESSION_SECRET": TEST_SECRET}

LOC_A = "accounts/1001/locations/loc-001"
LOC_B = "accounts/1001/locations/loc-002"

PASSWORD = "M4trixSecret!"

#: Matrix role key -> seeded username.
USERNAMES = {"admin": "admin", "owner": "owner1", "reviewer": "reviewer1"}


class RbacMatrixBase(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")
        self.repo.init_db(SCHEMA_PATH)
        acct = self.repo.upsert_account("accounts/1001")
        self.loc_a = self.repo.upsert_location(
            acct, LOC_A, title="Demo Nails — Garden Grove", city="Garden Grove",
            state="CA", language="en")
        self.loc_b = self.repo.upsert_location(
            acct, LOC_B, title="Demo Nails — Westminster", city="Westminster",
            state="CA", language="vi")
        self.a_ids = str(self.loc_a)
        self.b_ids = str(self.loc_b)
        # Stable review per location (for detail / IDOR assertions).
        self.rid_a = self.repo.upsert_review(
            location_id=self.loc_a, review_name="reviews/a1", rating=5,
            comment="Loved my gel manicure!", reply_text=None)
        self.rid_b = self.repo.upsert_review(
            location_id=self.loc_b, review_name="reviews/b1", rating=1,
            comment="Terrible service", reply_text=None)

        seed = UserStore(self.repo.conn)
        seed.create_user("admin", hash_password(PASSWORD), ["admin"], [])
        seed.create_user("owner1", hash_password(PASSWORD), ["owner"], [self.a_ids])
        seed.create_user("reviewer1", hash_password(PASSWORD),
                         ["reviewer"], [self.a_ids])

        self.service = ApprovalService(self.repo)
        self.auth = AuthService(self.repo.conn, env=TEST_ENV, ttl=3600)
        self.server = create_server(self.service, port=0, auth=self.auth,
                                    rate_limit=100000, login_limit=100000)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repo.close()

    # ----------------------------------------------------------- #
    def _mk_draft(self, loc_id, status="needs_review"):
        """Insert a FRESH review + draft in ``loc_id``; return the draft id."""
        rid = self.repo.upsert_review(
            location_id=loc_id, review_name="reviews/" + uuid.uuid4().hex,
            rating=3, comment="fresh", reply_text=None)
        return self.repo.insert_reply_draft(
            rid, draft_text="draft text", status=status)

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
        cookie = set_cookie.split(";")[0] if set_cookie else None
        csrf = (body or {}).get("csrf_token") if isinstance(body, dict) else None
        return status, cookie, csrf

    # ----------------------------------------------------------- #
    def route_table(self):
        """The full role x route matrix, as explicit data.

        Each row: ``label``, ``method``, ``path`` (str or callable(test, role)),
        ``body`` (None or callable(test, role)), ``expect`` (role -> status),
        and ``unauth`` (status with no session). Every location-bearing route is
        present so a missing/removed authorization check fails the matrix.
        """
        def p_a(t, role):
            return f"/api/drafts/{t._mk_draft(t.loc_a)}"

        def p_b(t, role):
            return f"/api/drafts/{t._mk_draft(t.loc_b)}"

        return [
            # ---- scoped LIST/READ routes: all roles allowed, rows filtered ----
            {"label": "GET /api/queue", "method": "GET", "path": "/api/queue",
             "body": None, "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            {"label": "GET /api/reviews", "method": "GET", "path": "/api/reviews",
             "body": None, "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            {"label": "GET /api/locations", "method": "GET", "path": "/api/locations",
             "body": None, "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            {"label": "GET /api/locations/status", "method": "GET",
             "path": "/api/locations/status", "body": None,
             "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            {"label": "GET /api/dashboard", "method": "GET", "path": "/api/dashboard",
             "body": None, "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            # ---- DETAIL routes ----
            {"label": "GET /api/reviews/<own>", "method": "GET",
             "path": lambda t, r: f"/api/reviews/{t.rid_a}", "body": None,
             "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            {"label": "GET /api/reviews/<foreign> IDOR", "method": "GET",
             "path": lambda t, r: f"/api/reviews/{t.rid_b}", "body": None,
             "expect": {"admin": 200, "owner": 403, "reviewer": 403}},
            {"label": "GET /api/locations/<own>", "method": "GET",
             "path": lambda t, r: f"/api/locations/{t.loc_a}", "body": None,
             "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            {"label": "GET /api/locations/<foreign> IDOR", "method": "GET",
             "path": lambda t, r: f"/api/locations/{t.loc_b}", "body": None,
             "expect": {"admin": 200, "owner": 403, "reviewer": 403}},
            # ---- AUDIT: location scope = view; no scope = admin-only ----
            {"label": "GET /api/audit?location_id=<own>", "method": "GET",
             "path": lambda t, r: f"/api/audit?location_id={t.a_ids}", "body": None,
             "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            {"label": "GET /api/audit (no scope) admin-only", "method": "GET",
             "path": "/api/audit", "body": None,
             "expect": {"admin": 200, "owner": 403, "reviewer": 403}},
            {"label": "GET /api/usage admin-only", "method": "GET",
             "path": "/api/usage", "body": None,
             "expect": {"admin": 200, "owner": 403, "reviewer": 403}},
            # ---- MUTATING routes ----
            {"label": "POST approve <own>", "method": "POST",
             "path": lambda t, r: p_a(t, r) + "/approve", "body": lambda t, r: {},
             "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            {"label": "POST approve <foreign> IDOR", "method": "POST",
             "path": lambda t, r: p_b(t, r) + "/approve", "body": lambda t, r: {},
             "expect": {"admin": 200, "owner": 403, "reviewer": 403}},
            {"label": "POST edit <own>", "method": "POST",
             "path": lambda t, r: p_a(t, r) + "/edit",
             "body": lambda t, r: {"text": "edited"},
             "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            {"label": "POST edit <foreign> IDOR", "method": "POST",
             "path": lambda t, r: p_b(t, r) + "/edit",
             "body": lambda t, r: {"text": "leak"},
             "expect": {"admin": 200, "owner": 403, "reviewer": 403}},
            {"label": "POST reject <own>", "method": "POST",
             "path": lambda t, r: p_a(t, r) + "/reject",
             "body": lambda t, r: {"note": "no"},
             "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            {"label": "POST reject <foreign> IDOR", "method": "POST",
             "path": lambda t, r: p_b(t, r) + "/reject",
             "body": lambda t, r: {"note": "no"},
             "expect": {"admin": 200, "owner": 403, "reviewer": 403}},
            {"label": "POST publish-guard <own>", "method": "POST",
             "path": lambda t, r: p_a(t, r) + "/publish-guard",
             "body": lambda t, r: {},
             # authorize() passes (view) -> the DRY-RUN guard blocks with 423
             # (never publishes); a missing check would surface as 403 instead.
             "expect": {"admin": 423, "owner": 423, "reviewer": 423}},
            {"label": "POST publish-guard <foreign> IDOR", "method": "POST",
             "path": lambda t, r: p_b(t, r) + "/publish-guard",
             "body": lambda t, r: {},
             "expect": {"admin": 423, "owner": 403, "reviewer": 403}},
            {"label": "POST bulk-approve <own>", "method": "POST",
             "path": "/api/drafts/bulk-approve",
             "body": lambda t, r: {"ids": [t._mk_draft(t.loc_a)]},
             "expect": {"admin": 200, "owner": 200, "reviewer": 200}},
            {"label": "POST bulk-approve <foreign> IDOR", "method": "POST",
             "path": "/api/drafts/bulk-approve",
             "body": lambda t, r: {"ids": [t._mk_draft(t.loc_b)]},
             "expect": {"admin": 200, "owner": 403, "reviewer": 403}},
        ]

    def _resolve(self, value, role):
        return value(self, role) if callable(value) else value


# ======================================================================= #
# full role x route matrix + unauthenticated 401 (driven by one table)
# ======================================================================= #
class RoleRouteMatrixTest(RbacMatrixBase):
    def test_every_role_route_pair_matches_expected_status(self):
        for row in self.route_table():
            for role, expected in row["expect"].items():
                status, cookie, csrf = self._login(USERNAMES[role])
                self.assertEqual(status, 200, msg=f"login failed for {role}")
                path = self._resolve(row["path"], role)
                body = self._resolve(row["body"], role)
                st, _h, payload = self._req(
                    row["method"], path, body, cookie=cookie, csrf=csrf)
                self.assertEqual(
                    st, expected,
                    msg=f"{row['label']}: {role} expected {expected} got {st} "
                        f"(payload={payload})")

    def test_unauthenticated_is_401_on_every_route(self):
        for row in self.route_table():
            path = self._resolve(row["path"], "reviewer1")
            body = self._resolve(row["body"], "reviewer1")
            st, _h, payload = self._req(row["method"], path, body)
            self.assertEqual(
                st, 401, msg=f"{row['label']}: expected 401 unauthenticated, got {st}")
            self.assertEqual((payload or {}).get("error"), "unauthenticated",
                             msg=f"{row['label']}: wrong error body")

    def test_rbac_route_matrix_constant_is_documented_and_complete(self):
        # The documented constant must list every location-bearing route.
        for key in (("GET", "/api/queue"), ("GET", "/api/reviews"),
                    ("GET", "/api/locations"), ("GET", "/api/locations/status"),
                    ("GET", "/api/dashboard"), ("GET", "/api/audit"),
                    ("GET", "/api/usage"), ("POST", "/api/drafts/<id>/approve"),
                    ("POST", "/api/drafts/<id>/reject"),
                    ("POST", "/api/drafts/<id>/edit"),
                    ("POST", "/api/drafts/<id>/publish-guard"),
                    ("POST", "/api/drafts/bulk-approve")):
            self.assertIn(key, RBAC_ROUTE_MATRIX, msg=f"missing route in matrix: {key}")
        self.assertEqual(RBAC_ROUTE_MATRIX[("GET", "/api/usage")], "admin")
        self.assertEqual(RBAC_ROUTE_MATRIX[("POST", "/api/drafts/<id>/approve")],
                         "approve")
        # reviewers never carry the admin action
        self.assertNotIn("admin", ROLE_PERMISSIONS["reviewer"])
        self.assertNotIn("admin", ROLE_PERMISSIONS["owner"])


# ======================================================================= #
# IDOR by guessed id: 403 AND the DB row is untouched
# ======================================================================= #
class IdorDbUnchangedTest(RbacMatrixBase):
    def test_reviewer_guessing_foreign_draft_id_is_403_and_db_unchanged(self):
        _s, cookie, csrf = self._login("reviewer1")
        for action, body in (("approve", {}), ("edit", {"text": "leak"}),
                             ("reject", {"note": "x"}), ("publish-guard", {})):
            draft_b = self._mk_draft(self.loc_b)
            before = self.repo.conn.execute(
                "SELECT status, version, draft_text FROM reply_drafts WHERE id = ?",
                (draft_b,)).fetchone()
            st, _h, payload = self._req(
                "POST", f"/api/drafts/{draft_b}/{action}", body,
                cookie=cookie, csrf=csrf)
            self.assertEqual(st, 403, msg=action)
            self.assertEqual(payload.get("error"), "forbidden", msg=action)
            after = self.repo.conn.execute(
                "SELECT status, version, draft_text FROM reply_drafts WHERE id = ?",
                (draft_b,)).fetchone()
            self.assertEqual(before["status"], after["status"], msg=action)
            self.assertEqual(before["version"], after["version"], msg=action)
            self.assertEqual(before["draft_text"], after["draft_text"], msg=action)
            n = self.repo.conn.execute(
                "SELECT COUNT(*) AS n FROM approval_records WHERE draft_id = ?",
                (draft_b,)).fetchone()["n"]
            self.assertEqual(int(n), 0, msg=action)

    def test_reviewer_guessing_foreign_detail_ids_is_403(self):
        _s, cookie, _csrf = self._login("reviewer1")
        self.assertEqual(
            self._req("GET", f"/api/reviews/{self.rid_b}", cookie=cookie)[0], 403)
        self.assertEqual(
            self._req("GET", f"/api/locations/{self.loc_b}", cookie=cookie)[0], 403)

    def test_bulk_approve_mixing_foreign_id_is_403_and_no_side_effect(self):
        _s, cookie, csrf = self._login("reviewer1")
        own = self._mk_draft(self.loc_a)
        foreign = self._mk_draft(self.loc_b)
        st, _h, payload = self._req(
            "POST", "/api/drafts/bulk-approve", {"ids": [own, foreign]},
            cookie=cookie, csrf=csrf)
        self.assertEqual(st, 403)
        for draft_id in (own, foreign):
            status = self.repo.conn.execute(
                "SELECT status FROM reply_drafts WHERE id = ?",
                (draft_id,)).fetchone()["status"]
            self.assertEqual(status, "needs_review")


# ======================================================================= #
# cross-location list leakage
# ======================================================================= #
class ListLeakageTest(RbacMatrixBase):
    def _assert_no_loc_b(self, payload, label):
        blob = json.dumps(payload)
        self.assertNotIn(LOC_B, blob, msg=f"{label} leaked the loc-B resource id")
        self.assertNotIn("Westminster", blob, msg=f"{label} leaked the loc-B name")

    def test_scoped_lists_never_contain_a_foreign_location(self):
        _s, cookie, _csrf = self._login("reviewer1")
        for label, path in (
            ("locations", "/api/locations"),
            ("locations/status", "/api/locations/status"),
            ("queue", "/api/queue"),
            ("reviews", "/api/reviews"),
            ("dashboard", "/api/dashboard"),
            ("audit scoped", f"/api/audit?location_id={self.a_ids}"),
        ):
            st, _h, payload = self._req("GET", path, cookie=cookie)
            self.assertEqual(st, 200, msg=label)
            self._assert_no_loc_b(payload, label)
        # every queue/reviews row belongs to the reviewer's own location
        _s, _h, queue = self._req("GET", "/api/queue", cookie=cookie)
        self.assertTrue(all(str(r["location_id"]) == self.a_ids
                            for r in queue["items"]))
        # global billing (no location column) is admin-only -> no leak
        self.assertEqual(self._req("GET", "/api/usage", cookie=cookie)[0], 403)

    def test_dashboard_kpis_are_scoped_for_a_non_admin(self):
        # Two locations, one review each. A reviewer scoped to loc-A must see
        # ONE location / ONE review, not the global totals.
        _s, cookie, _csrf = self._login("reviewer1")
        _s, _h, dash = self._req("GET", "/api/dashboard", cookie=cookie)
        self.assertEqual(dash["kpis"]["total_locations"], 1)
        self.assertEqual(dash["kpis"]["total_reviews"], 1)
        self.assertEqual(dash["kpis"]["estimated_ai_cost_usd"], 0.0)
        # admin sees the true global totals
        _s, admin_cookie, _csrf = self._login("admin")
        _s, _h, admin_dash = self._req("GET", "/api/dashboard", cookie=admin_cookie)
        self.assertEqual(admin_dash["kpis"]["total_locations"], 2)
        self.assertEqual(admin_dash["kpis"]["total_reviews"], 2)


# ======================================================================= #
# the session is authoritative: a client-supplied actor/role cannot escalate
# ======================================================================= #
class SessionBeatsClientTest(RbacMatrixBase):
    def test_client_actor_field_cannot_escalate_or_place_a_foreign_action(self):
        _s, cookie, csrf = self._login("reviewer1")
        foreign = self._mk_draft(self.loc_b)
        # A JSON "actor":"admin" on a foreign draft is refused as an unknown
        # field (400) -- never honoured -- and the row is untouched.
        st, _h, _p = self._req(
            "POST", f"/api/drafts/{foreign}/approve", {"actor": "admin"},
            cookie=cookie, csrf=csrf)
        self.assertIn(st, (400, 403), msg="client actor must not be accepted")
        row = self.repo.conn.execute(
            "SELECT status FROM reply_drafts WHERE id = ?", (foreign,)).fetchone()
        self.assertEqual(row["status"], "needs_review")
        # ?actor=admin on a mutating/loc route is inert: the session still wins,
        # so a foreign id stays denied and an own id is attributed to reviewer1.
        st, _h, _p = self._req(
            "GET", f"/api/queue?actor=admin", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertTrue(all(str(r["location_id"]) == self.a_ids
                            for r in _p["items"]))
        own = self._mk_draft(self.loc_a)
        st, _h, body = self._req(
            "POST", f"/api/drafts/{own}/approve?actor=admin", {},
            cookie=cookie, csrf=csrf)
        self.assertEqual(st, 200)
        self.assertEqual(body.get("actor"), "reviewer1")

    def test_reviewer_cannot_reach_admin_only_routes_or_manage_users(self):
        _s, cookie, _csrf = self._login("reviewer1")
        self.assertEqual(self._req("GET", "/api/audit", cookie=cookie)[0], 403)
        self.assertEqual(self._req("GET", "/api/usage", cookie=cookie)[0], 403)
        reviewer = self.auth.get_user("reviewer1")
        self.assertFalse(self.auth.can_manage_users(reviewer))
        self.assertFalse(self.auth.authorize(reviewer, "admin", self.a_ids))
        self.assertFalse(self.auth.authorize(reviewer, "admin", None))
        # admin can reach them
        _s, admin_cookie, _csrf = self._login("admin")
        self.assertEqual(self._req("GET", "/api/audit", cookie=admin_cookie)[0], 200)
        self.assertEqual(self._req("GET", "/api/usage", cookie=admin_cookie)[0], 200)


# ======================================================================= #
# inactive user -> 403 everywhere
# ======================================================================= #
class InactiveUserTest(RbacMatrixBase):
    def test_inactive_user_is_403_on_every_route(self):
        _s, cookie, csrf = self._login("reviewer1")
        UserStore(self.repo.conn).set_active("reviewer1", False)
        for row in self.route_table():
            path = self._resolve(row["path"], "reviewer1")
            body = self._resolve(row["body"], "reviewer1")
            st, _h, payload = self._req(
                row["method"], path, body, cookie=cookie, csrf=csrf)
            self.assertEqual(st, 403, msg=f"{row['label']} inactive: got {st}")
            self.assertEqual((payload or {}).get("error"), "inactive",
                             msg=f"{row['label']} inactive: wrong error body")


# ======================================================================= #
# /api/me exposes effective permissions (UI reflects them; backend decides)
# ======================================================================= #
class MePermissionsTest(RbacMatrixBase):
    def test_me_exposes_effective_permissions(self):
        _s, cookie, _csrf = self._login("reviewer1")
        _s, _h, me = self._req("GET", "/api/me", cookie=cookie)
        self.assertTrue(me["authenticated"])
        self.assertFalse(me["is_admin"])
        self.assertEqual(sorted(me["permissions"]),
                         ["approve", "edit", "reject", "view"])
        self.assertEqual(me["scoped_locations"], [self.a_ids])
        self.assertNotIn("admin", me["permissions"])

        _s, admin_cookie, _csrf = self._login("admin")
        _s, _h, admin_me = self._req("GET", "/api/me", cookie=admin_cookie)
        self.assertTrue(admin_me["is_admin"])
        self.assertIn("admin", admin_me["permissions"])
        self.assertIsNone(admin_me["scoped_locations"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
