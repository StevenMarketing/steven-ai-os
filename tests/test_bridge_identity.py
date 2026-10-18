"""Blocker 1 tests: authorization must come from verified GitHub metadata, never
from self-declared payload fields.
"""
import unittest

from gbp.bridge.identity import (
    ADMIN_ONLY_OPERATIONS,
    TrustedContext,
    fetch_trusted_context,
    verify_identity,
)

OWNER = "StevenMarketing"


def ctx(**kw):
    base = dict(repository="StevenMarketing/steven-ai-os", source_kind="issue_comment",
                source_id=1, author_login=OWNER, author_association="OWNER",
                permission="admin", event_type="issue_comment", verified=True)
    base.update(kw)
    return TrustedContext(**base)


class SpoofingTests(unittest.TestCase):
    def test_actor_field_cannot_grant_identity(self):
        """A commenter who writes actor=StevenMarketing must be refused."""
        env = {"operation": "status", "actor": OWNER, "origin": OWNER,
               "repository": "StevenMarketing/steven-ai-os"}
        ok, code, detail = verify_identity(env, ctx(author_login="attacker", permission="admin",
                                                   author_association="NONE"),
                                           allowlisted_logins=[OWNER])
        self.assertFalse(ok)
        self.assertEqual(code, "actor_mismatch")
        self.assertIn("attacker", detail)

    def test_documented_envelope_with_verified_owner_metadata_is_accepted(self):
        """End to end: the documented envelope uses origin = the repository."""
        env = {"operation": "status",
               "repository": "StevenMarketing/steven-ai-os",
               "origin": "StevenMarketing/steven-ai-os",
               "actor": OWNER}
        ok, code, detail = verify_identity(env, ctx(), allowlisted_logins=[OWNER])
        self.assertTrue(ok, detail)
        self.assertEqual(code, "verified")

    def test_origin_is_compared_with_the_repository_not_the_actor(self):
        """origin names the repository: a different repository is a refusal."""
        env = {"operation": "status", "origin": "attacker/other"}
        ok, code, _ = verify_identity(env, ctx(author_login=OWNER), allowlisted_logins=[OWNER])
        self.assertFalse(ok)
        self.assertEqual(code, "origin_mismatch")

    def test_spoofed_actor_with_repository_origin_is_still_refused(self):
        env = {"operation": "status",
               "repository": "StevenMarketing/steven-ai-os",
               "origin": "StevenMarketing/steven-ai-os",
               "actor": OWNER}
        ok, code, _ = verify_identity(env, ctx(author_login="attacker", permission="admin"),
                                      allowlisted_logins=[OWNER])
        self.assertFalse(ok)
        self.assertEqual(code, "actor_mismatch")

    def test_verified_owner_is_authorised(self):
        # documented envelope: origin is the repository, actor is the login
        env = {"operation": "status", "actor": OWNER,
               "origin": "StevenMarketing/steven-ai-os",
               "repository": "StevenMarketing/steven-ai-os"}
        ok, code, _ = verify_identity(env, ctx(), allowlisted_logins=[OWNER])
        self.assertTrue(ok)
        self.assertEqual(code, "verified")

    def test_non_allowlisted_writer_is_refused(self):
        ok, code, _ = verify_identity({"operation": "status"}, ctx(author_login="collaborator",
                                                                  permission="write"),
                                      allowlisted_logins=[OWNER])
        self.assertFalse(ok)
        self.assertEqual(code, "not_allowlisted")

    def test_privileged_operation_requires_admin_even_for_allowlisted_writer(self):
        for operation in ADMIN_ONLY_OPERATIONS:
            ok, code, _ = verify_identity({"operation": operation},
                                          ctx(permission="write"), allowlisted_logins=[OWNER])
            self.assertFalse(ok, operation)
            self.assertEqual(code, "insufficient_permission")

    def test_missing_or_unverified_metadata_fails_closed(self):
        for trusted in (None, ctx(verified=False)):
            ok, code, _ = verify_identity({"operation": "status"}, trusted,
                                          allowlisted_logins=[OWNER])
            self.assertFalse(ok)
            self.assertEqual(code, "missing_trusted_metadata")

    def test_repository_mismatch_is_refused(self):
        env = {"operation": "status", "repository": "attacker/other"}
        ok, code, _ = verify_identity(env, ctx(), allowlisted_logins=[OWNER])
        self.assertFalse(ok)
        self.assertEqual(code, "repo_mismatch")

    def test_non_comment_source_cannot_carry_commands(self):
        ok, code, _ = verify_identity({"operation": "status"}, ctx(source_kind="issue_body"),
                                      allowlisted_logins=[OWNER])
        self.assertFalse(ok)
        self.assertEqual(code, "source_not_allowed")

    def test_no_allowlist_is_refused(self):
        ok, code, _ = verify_identity({"operation": "status"}, ctx(), allowlisted_logins=[])
        self.assertFalse(ok)
        self.assertEqual(code, "not_allowlisted")


class FetchTrustedContextTests(unittest.TestCase):
    def test_transport_is_called_with_the_authenticated_signature(self):
        """The permission lookup must use the documented API path and the token."""
        seen = []

        def request(method, path, token=None, body=None):
            seen.append((method, path, token))
            if path.endswith("/issues/comments/9"):
                return ({"user": {"login": OWNER}, "author_association": "OWNER"}, {})
            if path.endswith("/collaborators/%s/permission" % OWNER):
                return ({"permission": "admin"}, {})
            raise AssertionError(path)

        trusted = fetch_trusted_context(request, repository="StevenMarketing/steven-ai-os",
                                        source_kind="issue_comment", source_id=9, token="tok")
        self.assertEqual(trusted.permission, "admin")
        self.assertTrue(all(token == "tok" for _, _, token in seen), seen)
        self.assertIn(("GET", "/repos/StevenMarketing/steven-ai-os/collaborators/StevenMarketing/permission", "tok"), seen)
        ok, code, _ = verify_identity({"operation": "resume_queue"}, trusted,
                                      allowlisted_logins=[OWNER])
        self.assertTrue(ok, code)

    def test_missing_token_is_not_verified(self):
        trusted = fetch_trusted_context(lambda *a, **k: None, repository="r/r",
                                        source_kind="issue_comment", source_id=1, token="")
        self.assertFalse(trusted.verified)
    def test_uses_api_metadata_and_unknown_permission_fails_closed(self):
        calls = []

        def request(method, path, token=None, body=None):
            calls.append((method, path, token))
            if path.endswith("/issues/comments/7"):
                return ({"user": {"login": OWNER}, "author_association": "OWNER"}, {})
            raise RuntimeError("no permission scope")

        trusted = fetch_trusted_context(request, repository="StevenMarketing/steven-ai-os",
                                        source_kind="issue_comment", source_id=7, token="tok")
        self.assertEqual(trusted.author_login, OWNER)
        self.assertTrue(trusted.verified)
        self.assertEqual(trusted.permission, "")
        ok, code, _ = verify_identity({"operation": "pause_queue"}, trusted,
                                      allowlisted_logins=[OWNER])
        self.assertFalse(ok)
        self.assertEqual(code, "insufficient_permission")

    def test_lookup_failure_is_not_verified(self):
        def request(*a, **k):
            raise RuntimeError("boom")

        trusted = fetch_trusted_context(request, repository="r/r", source_kind="issue_comment",
                                        source_id=1, token="tok")
        self.assertFalse(trusted.verified)
        self.assertFalse(verify_identity({"operation": "status"}, trusted,
                                         allowlisted_logins=[OWNER])[0])
