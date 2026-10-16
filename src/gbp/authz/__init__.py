"""Authentication + role-based, per-location authorization.

Standard library only, fully offline. See :mod:`gbp.authz.auth` for the
server-side permission matrix, password hashing and HMAC-signed session tokens,
and :mod:`gbp.authz.store` for the persistent (SQLite) user/session/rate-limit
stores added by Group 6B.
"""

from .auth import (
    ACTIONS,
    ROLE_PERMISSIONS,
    AuthService,
    User,
    hash_password,
    verify_password,
)
from .fixtures import DEMO_USERS, seeded_users, user_from_dict
from .store import RateLimitStore, SessionStore, UserStore, ensure_auth_tables

__all__ = [
    "ACTIONS",
    "ROLE_PERMISSIONS",
    "AuthService",
    "User",
    "hash_password",
    "verify_password",
    "DEMO_USERS",
    "seeded_users",
    "user_from_dict",
    "UserStore",
    "SessionStore",
    "RateLimitStore",
    "ensure_auth_tables",
]
