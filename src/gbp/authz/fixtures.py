"""Demo/MOCK user fixtures for :mod:`gbp.authz`.

NOTHING here is safe for production: the passwords below are explicitly
labelled DEMO constants used so the MOCK/demo server can be exercised offline.
Every one of them can be overridden by an environment variable (see
:data:`DEMO_USERS`'s ``password_env``) and the production path must always
supply passwords through the environment, never through this module.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .auth import User, hash_password

#: DEMO-ONLY users. ``password_env`` names the environment variable that
#: overrides the demo password; ``password`` is the explicit demo fallback.
DEMO_USERS: List[Dict[str, Any]] = [
    {
        "username": "admin",
        "password_env": "GBP_DEMO_ADMIN_PASSWORD",
        "password": "demo-admin-not-a-secret",  # DEMO ONLY
        "roles": ["admin"],
        "locations": [],
    },
    {
        "username": "owner1",
        "password_env": "GBP_DEMO_OWNER1_PASSWORD",
        "password": "demo-owner1-not-a-secret",  # DEMO ONLY
        "roles": ["owner"],
        "locations": ["accounts/1001/locations/loc-001"],
    },
    {
        "username": "reviewer1",
        "password_env": "GBP_DEMO_REVIEWER1_PASSWORD",
        "password": "demo-reviewer1-not-a-secret",  # DEMO ONLY
        "roles": ["reviewer"],
        "locations": ["accounts/1001/locations/loc-001"],
    },
]


def user_from_dict(
    spec: Mapping[str, Any],
    env: Optional[Mapping[str, str]] = None,
    *,
    use_demo_fallback: bool = False,
) -> User:
    """Build a :class:`User` from a fixture dict, hashing the password.

    The plaintext password is taken from ``spec['password_env']`` in ``env``
    (completed from the ``username``); the demo fallback is only used when
    ``use_demo_fallback`` is explicitly true. The plaintext is hashed and is
    never stored on the returned object.
    """
    env = env if env is not None else os.environ
    username = str(spec["username"])
    env_name = spec.get("password_env") or f"GBP_{username.upper()}_PASSWORD"
    password = env.get(env_name)
    if not password:
        if not use_demo_fallback or not spec.get("password"):
            raise ValueError(
                f"no password for demo user {username!r}: set {env_name}"
            )
        password = str(spec["password"])
    return User(
        username=username,
        password_hash=hash_password(password),
        roles=list(spec.get("roles") or []),
        locations=list(spec.get("locations") or []),
    )


def seeded_users(
    env: Optional[Mapping[str, str]] = None,
    *,
    use_demo_fallback: bool = False,
    specs: Optional[Iterable[Mapping[str, Any]]] = None,
) -> List[User]:
    """Return the seeded demo users (env-driven passwords)."""
    specs = list(specs if specs is not None else DEMO_USERS)
    return [user_from_dict(s, env, use_demo_fallback=use_demo_fallback) for s in specs]
