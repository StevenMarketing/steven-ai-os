#!/usr/bin/env python3
"""Fail-closed pre-flight checks for the private GReply preview container.

Pure, offline and side-effect free: :func:`check` takes an environment mapping
and returns a list of human-readable violations. The empty list is the only
"go" signal. Any violation means the preview container must refuse to start.

A rejected environment is described by variable NAME only -- never by its
value -- so a secret can never leak into a log through this module.
"""

from __future__ import annotations

from typing import List, Mapping, Optional

#: The preview must carry an explicit acknowledgement, so a bare
#: ``docker run <image>`` can never silently serve the dashboard.
PREVIEW_ACK = ("GBP_PREVIEW", "1")

#: The session-signing secret must be long enough to be worth anything; this
#: mirrors the app's own ``GBP_SESSION_SECRET`` policy.
MIN_SECRET_CHARS = 32

#: Variable names meaning "this process can reach LIVE Google / production".
#: None of them may be present: the preview is fixtures-only, login-gated.
LIVE_MARKERS = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GBP_CLIENT_SECRET",
    "GBP_REFRESH_TOKEN",
    "GBP_SERVICE_ACCOUNT_JSON",
    "GBP_LIVE",
    "GBP_ALLOW_PROD",
)

#: Environment names that would mark this as the production deployment.
PRODUCTION_ENVS = ("prod", "production", "live")

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def check(env: Mapping[str, str]) -> List[str]:
    """Return the violations that must stop the preview from starting.

    The returned strings contain variable names and rule descriptions only.
    """
    problems: List[str] = []

    ack_name, ack_value = PREVIEW_ACK
    if env.get(ack_name) != ack_value:
        problems.append(f"{ack_name} must be {ack_value!r} to acknowledge a preview run")

    secret = env.get("GBP_SESSION_SECRET")
    if not secret:
        problems.append("GBP_SESSION_SECRET is required: preview sessions must be signed")
    elif len(secret) < MIN_SECRET_CHARS:
        problems.append(f"GBP_SESSION_SECRET must be at least {MIN_SECRET_CHARS} characters")

    if not str(env.get("GBP_DEMO_PASSWORD") or "").strip():
        problems.append("GBP_DEMO_PASSWORD is required: login is the only way in")

    # Anonymous access is never allowed, whatever a caller tries to set.
    if "GBP_ALLOW_ANON" in env:
        problems.append("GBP_ALLOW_ANON must not be set: anonymous access is never allowed")

    if str(env.get("GBP_ENV") or "").strip().lower() in PRODUCTION_ENVS:
        problems.append("GBP_ENV marks this as production: the preview is not production")

    present = sorted(name for name in LIVE_MARKERS if name in env)
    if present:
        problems.append(
            "live Google/production variables must not be set: " + ", ".join(present)
        )

    return problems


def main(argv: Optional[List[str]] = None) -> int:
    import os
    import sys

    problems = check(os.environ)
    if problems:
        for problem in problems:
            print(f"preview guard: {problem}", file=sys.stderr)
        return 1
    print("preview guard: ok (fixtures only, login required, loopback bind)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
