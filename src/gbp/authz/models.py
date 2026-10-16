"""Core authn/authz primitives shared by the persistent stores and the service.

Kept dependency-free (stdlib only) and separate from :mod:`gbp.authz.store`
(which needs :class:`User`) and :mod:`gbp.authz.auth` (which needs both) so the
import graph stays acyclic::

    models  <-  store  <-  auth  <-  fixtures

``gbp.authz.auth`` re-exports every public name here for backward
compatibility (existing callers import ``User``/``hash_password`` from it).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "ACTIONS",
    "ROLE_PERMISSIONS",
    "User",
    "hash_password",
    "verify_password",
    "DEFAULT_ITERATIONS",
    "DEFAULT_TTL_SECONDS",
]

#: The actions authorization is evaluated against.
ACTIONS = frozenset({"view", "edit", "approve", "reject", "admin"})

#: Server-side role -> permitted actions matrix (single source of truth).
ROLE_PERMISSIONS: Dict[str, frozenset] = {
    "admin": frozenset({"view", "edit", "approve", "reject", "admin"}),
    "owner": frozenset({"view", "edit", "approve", "reject"}),
    "reviewer": frozenset({"view", "edit", "approve", "reject"}),
}

_PBKDF2_ALGO = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 100_000
DEFAULT_TTL_SECONDS = 3600
_SALT_BYTES = 16


def hash_password(
    password: str,
    *,
    salt: Optional[bytes] = None,
    iterations: int = DEFAULT_ITERATIONS,
) -> str:
    """Hash ``password`` with PBKDF2-HMAC-SHA256 and a per-user salt.

    Returns ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``. The plaintext
    is never kept.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = salt if salt is not None else secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_PBKDF2_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a stored hash."""
    if not password or not stored:
        return False
    try:
        algo, iters, salt_hex, _hash_hex = stored.split("$")
        if algo != _PBKDF2_ALGO:
            return False
        salt = bytes.fromhex(salt_hex)
        iterations = int(iters)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    ).hex()
    return hmac.compare_digest(candidate, _hash_hex)


@dataclass
class User:
    """A user record. ``password_hash`` is an opaque PBKDF2 string.

    ``active`` and ``mfa_required`` were added by Group 6B (both default to the
    historic behaviour so existing callers are unaffected). ``mfa_required`` is
    READINESS ONLY -- MFA is not implemented (see ``AuthService.begin_mfa``).
    """

    username: str
    password_hash: str
    roles: List[str] = field(default_factory=list)
    locations: List[str] = field(default_factory=list)
    active: bool = True
    mfa_required: bool = False

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def permitted_actions(self) -> frozenset:
        perms: set = set()
        for role in self.roles:
            perms |= ROLE_PERMISSIONS.get(role, frozenset())
        return frozenset(perms)

    def location_set(self) -> set:
        return {str(loc) for loc in self.locations}

    def to_public_dict(self) -> Dict[str, Any]:
        """JSON-safe view for the API -- never includes the password hash."""
        return {
            "username": self.username,
            "roles": list(self.roles),
            "locations": list(self.locations),
            "active": bool(self.active),
            "mfa_required": bool(self.mfa_required),
        }
