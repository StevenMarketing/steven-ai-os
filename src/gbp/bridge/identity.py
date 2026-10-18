"""Trusted-identity verification for the GitHub command bridge (blocker 1).

Authorization NEVER uses self-asserted payload fields (``actor``/``origin``). Those
fields stay in the envelope for readability only: they are *compared* against the
trusted metadata fetched from the GitHub API, and any disagreement is reported as a
spoofing attempt and refused.

Trusted metadata comes from the GitHub API for the exact event being read:
  * ``GET /repos/{repo}/issues/comments/{comment_id}`` -> author login + association
  * ``GET /repos/{repo}/collaborators/{login}/permission`` -> effective permission
  * the repository full name and event type come from the caller's request context

Fail-closed: when the trusted metadata is unavailable, nothing is authorised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

__all__ = [
    "TrustedContext",
    "IdentityError",
    "ALLOWED_SOURCE_KINDS",
    "ADMIN_ONLY_OPERATIONS",
    "verify_identity",
    "fetch_trusted_context",
    "redact_login",
]

#: Only these event sources may carry a command.
ALLOWED_SOURCE_KINDS = ("issue_comment",)

#: Operations that require the owner (admin) permission, never a lesser one.
ADMIN_ONLY_OPERATIONS = ("pause_queue", "resume_queue", "retry_failed_issue")

#: Permission levels GitHub can report, weakest first.
_PERMISSION_ORDER = {"none": 0, "read": 1, "triage": 2, "write": 3, "maintain": 4, "admin": 5}


class IdentityError(Exception):
    """Raised only for programmer errors; verification returns a tuple instead."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class TrustedContext:
    """Metadata fetched from GitHub, never from the command payload."""

    repository: str
    source_kind: str
    source_id: Optional[int]
    author_login: str
    author_association: str = ""
    permission: str = ""
    event_type: str = ""
    verified: bool = False
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_public_dict(self) -> Dict[str, Any]:
        """Redacted view, safe to put into a status document."""
        return {
            "repository": self.repository,
            "source_kind": self.source_kind,
            "author_login": redact_login(self.author_login),
            "permission": self.permission,
            "verified": self.verified,
        }


def redact_login(login: Any) -> str:
    """Keep just enough of a login to be recognisable; never a full identifier."""
    text = str(login or "")
    if len(text) <= 3:
        return "***"
    return text[:2] + "***" + text[-1:]


def _permission_rank(permission: str) -> int:
    return _PERMISSION_ORDER.get(str(permission or "none").lower(), 0)


def verify_identity(
    envelope: Dict[str, Any],
    trusted: Optional[TrustedContext],
    *,
    allowlisted_logins: Sequence[str],
    required_permission: str = "admin",
) -> Tuple[bool, str, str]:
    """Authorize a command envelope against trusted metadata.

    Returns ``(ok, code, detail)``. ``code`` is a stable machine-readable reason:

    ``missing_trusted_metadata``, ``source_not_allowed``, ``repo_mismatch``,
    ``actor_mismatch``, ``origin_mismatch``, ``not_allowlisted``,
    ``insufficient_permission``, ``not_verified``.
    """
    if trusted is None or not trusted.verified:
        return False, "missing_trusted_metadata", "no verified GitHub context was supplied"
    if trusted.source_kind not in ALLOWED_SOURCE_KINDS:
        return False, "source_not_allowed", f"source kind {trusted.source_kind!r} cannot carry commands"

    envelope_repo = str(envelope.get("repository") or envelope.get("repo") or "")
    if envelope_repo and envelope_repo != trusted.repository:
        return False, "repo_mismatch", "command names a different repository than the event"

    # Self-declared fields are compared, never trusted.
    declared_actor = str(envelope.get("actor") or "").strip()
    if declared_actor and declared_actor != trusted.author_login:
        return False, "actor_mismatch", (
            "payload actor %s does not match the authenticated author %s"
            % (declared_actor, trusted.author_login)
        )
    declared_origin = str(envelope.get("origin") or "").strip()
    if declared_origin and declared_origin != trusted.author_login:
        return False, "origin_mismatch", (
            "payload origin %s does not match the authenticated author %s"
            % (declared_origin, trusted.author_login)
        )

    allow = {str(x).strip() for x in allowlisted_logins if str(x).strip()}
    if not allow:
        return False, "not_allowlisted", "no allowlisted command origin is configured"
    if trusted.author_login not in allow:
        return False, "not_allowlisted", "author is not in the command allowlist"

    operation = str(envelope.get("operation") or "")
    needed = "admin" if operation in ADMIN_ONLY_OPERATIONS else required_permission
    if _permission_rank(trusted.permission) < _permission_rank(needed):
        return False, "insufficient_permission", (
            "effective permission %r is below the required %r" % (trusted.permission, needed)
        )
    return True, "verified", "authenticated by GitHub event metadata"


def fetch_trusted_context(
    request,
    *,
    repository: str,
    source_kind: str,
    source_id: int,
) -> TrustedContext:
    """Build a TrustedContext from GitHub API responses.

    ``request`` is the existing ``issue_github.request``-shaped callable
    ``(method, path, token, body=None) -> (payload, meta)``. Any failure yields a
    context with ``verified=False`` so the caller fails closed.
    """
    if source_kind not in ALLOWED_SOURCE_KINDS:
        return TrustedContext(repository, source_kind, source_id, "", verified=False,
                              notes=("unsupported source",))
    try:
        token = request.__self__.read_token() if hasattr(request, "__self__") else None
    except Exception:  # pragma: no cover - transport differences
        token = None
    try:
        payload, _ = request("GET", "/repos/%s/issues/comments/%d" % (repository, int(source_id)), token)
    except Exception as exc:  # fail closed
        return TrustedContext(repository, source_kind, source_id, "", verified=False,
                              notes=("comment lookup failed: %s" % type(exc).__name__,))

    author = str((payload.get("user") or {}).get("login") or "")
    association = str(payload.get("author_association") or "")
    if not author:
        return TrustedContext(repository, source_kind, source_id, "", verified=False,
                              notes=("comment has no author",))
    permission = ""
    try:
        perm, _ = request("GET", "/repos/%s/collaborators/%s/permission" % (repository, author), token)
        permission = str(perm.get("permission") or "")
    except Exception:
        permission = ""  # verification below refuses when permission is unknown
    return TrustedContext(
        repository=repository,
        source_kind=source_kind,
        source_id=int(source_id),
        author_login=author,
        author_association=association,
        permission=permission,
        event_type="issue_comment",
        verified=True,
        notes=("permission lookup failed" if not permission else "",),
    )
