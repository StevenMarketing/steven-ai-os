"""Server-side authentication + role/per-location authorization.

Standard library only (``hashlib``, ``hmac``, ``secrets``, ``json``, ``base64``,
``time``, ``sqlite3``). No network, no third-party dependency, no hardcoded
secret.

Group 6B hardening
------------------
* Users, sessions and rate-limit counters are **persisted in SQLite**
  (:mod:`gbp.authz.store`) instead of living in process memory, so a restart or
  a second worker process observes the same sessions and the same throttle.
* Every session carries a random ``jti`` that is persisted. :meth:`validate`
  re-checks the ``sessions`` row, so :meth:`logout` (and
  :meth:`revoke_all`) genuinely invalidate a token even though its HMAC is
  still cryptographically valid.
* A user whose ``active`` flag is ``0`` is denied by :meth:`authorize` for
  every action and location.
* **MFA is NOT implemented.** ``mfa_required`` is per-user readiness only and
  :meth:`begin_mfa` / :meth:`verify_mfa` deliberately raise
  ``NotImplementedError`` -- nothing here may claim MFA support.

Backward compatibility
----------------------
``AuthService`` still accepts ``users=``, ``env=``, ``ttl=`` and ``now_fn=``
exactly as before, and ``db_path_or_conn`` defaults to a private in-memory
database, so the pre-existing callers/tests keep working unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

from .models import (
    ACTIONS,
    DEFAULT_ITERATIONS,
    DEFAULT_TTL_SECONDS,
    ROLE_PERMISSIONS,
    User,
    hash_password,
    verify_password,
)
from .store import (
    LOGIN_SCOPE_USER,
    LOGIN_SCOPE_USER_IP,
    LoginGuard,
    RateLimitStore,
    SessionStore,
    UserStore,
)
from ..security.secrets import (
    MIN_SECRET_LENGTH,
    SessionSecretError,
    WeakSessionSecretError,
    load_session_secret,
)

__all__ = [
    "ACTIONS",
    "ROLE_PERMISSIONS",
    "User",
    "AuthService",
    "DEFAULT_LOCKOUT_THRESHOLD",
    "hash_password",
    "verify_password",
    "SessionSecretError",
    "WeakSessionSecretError",
    "MIN_SECRET_LENGTH",
]

logger = logging.getLogger(__name__)

#: Default number of login attempts allowed per (username, IP) per window.
DEFAULT_LOGIN_RATE_LIMIT = 20

# --- Group 8B / T25: brute-force lockout ---------------------------------- #
#: Failed logins from ONE address before that (username, address) pair locks.
DEFAULT_LOCKOUT_THRESHOLD = 5
#: First lock duration; doubles for every extra failure, capped below.
DEFAULT_LOCKOUT_BASE_SECONDS = 60
#: Upper bound for an escalating lock (1 hour).
DEFAULT_LOCKOUT_MAX_SECONDS = 3600
#: Sliding window in which failures are counted.
DEFAULT_FAILURE_WINDOW_SECONDS = 900
#: Total failures across addresses before the ACCOUNT itself is throttled.
DEFAULT_GLOBAL_LOCKOUT_THRESHOLD = 20
#: ... and they must come from at least this many distinct addresses, so a
#: single attacker can never lock an account (anti-DoS by construction).
DEFAULT_GLOBAL_MIN_DISTINCT_IPS = 3
#: Duration of the account-wide throttle.
DEFAULT_GLOBAL_LOCK_SECONDS = 900
#: No response delay below this many failures.
DEFAULT_BACKOFF_AFTER = 3
#: Cap on the progressive response delay (seconds).
DEFAULT_MAX_BACKOFF_SECONDS = 1.0

#: Dummy hash used so an unknown username costs the same as a known one. This
#: is a throwaway string, NOT a credential, and it is never accepted.
_DUMMY_HASH = hash_password("dummy-password-not-a-credential")


def _salt_from_hash(stored: str) -> Optional[str]:
    """Return the hex salt embedded in a ``hash_password`` string, if any."""
    try:
        _algo, _iters, salt_hex, _hash = stored.split("$")
        return salt_hex
    except (ValueError, AttributeError):
        return None


class AuthService:
    """Login/session/authorization service backed by persistent SQLite stores.

    Parameters
    ----------
    db_path_or_conn:
        A ``sqlite3.Connection``, a ``Repository`` (its ``.conn`` is used), or a
        path string. ``None`` (default) uses a private in-memory database -- the
        legacy behaviour, except that sessions/rate limits are now persisted
        for the lifetime of the process.
    secret_env:
        Name of the environment variable holding the HMAC signing secret.
    require_secret:
        When ``True``, a missing/weak signing secret is a hard error
        (:class:`SessionSecretError`) instead of the legacy silent ephemeral
        fallback. Real deployments (``serve_ui.py --auth``) set this.
    allow_ephemeral:
        LOCAL EXPERIMENTATION ONLY. When ``require_secret`` is set, permit a
        random per-process secret (with a loud warning) instead of refusing.
    min_secret_length:
        Minimum acceptable length for a supplied secret in strict mode.
    users:
        Optional iterable of :class:`User`. When omitted, the users are loaded
        from the store if an explicit database was supplied, else the seeded
        demo store is used.
    """

    def __init__(
        self,
        db_path_or_conn: Any = None,
        secret_env: str = "GBP_SESSION_SECRET",
        users: Optional[Iterable[User]] = None,
        ttl: int = DEFAULT_TTL_SECONDS,
        env: Optional[Mapping[str, str]] = None,
        now_fn: Any = None,
        *,
        use_demo_fallback: bool = True,
        login_rate_limit: int = DEFAULT_LOGIN_RATE_LIMIT,
        lockout_threshold: int = DEFAULT_LOCKOUT_THRESHOLD,
        lockout_base_seconds: int = DEFAULT_LOCKOUT_BASE_SECONDS,
        lockout_max_seconds: int = DEFAULT_LOCKOUT_MAX_SECONDS,
        failure_window_seconds: int = DEFAULT_FAILURE_WINDOW_SECONDS,
        global_lockout_threshold: int = DEFAULT_GLOBAL_LOCKOUT_THRESHOLD,
        global_min_distinct_ips: int = DEFAULT_GLOBAL_MIN_DISTINCT_IPS,
        global_lock_seconds: int = DEFAULT_GLOBAL_LOCK_SECONDS,
        backoff_after: int = DEFAULT_BACKOFF_AFTER,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        sleep_fn: Any = None,
        require_secret: bool = False,
        allow_ephemeral: bool = False,
        min_secret_length: int = MIN_SECRET_LENGTH,
    ):
        env = env if env is not None else os.environ
        self.secret_env = secret_env
        self.ttl = int(ttl)
        self.now_fn = now_fn or time.time
        self.ephemeral_secret = False
        self.login_rate_limit = int(login_rate_limit)
        self._explicit_db = db_path_or_conn is not None
        # Group 8B / T25 brute-force controls.
        self.lockout_threshold = max(1, int(lockout_threshold))
        self.lockout_base_seconds = max(1, int(lockout_base_seconds))
        self.lockout_max_seconds = max(self.lockout_base_seconds,
                                       int(lockout_max_seconds))
        self.failure_window_seconds = max(1, int(failure_window_seconds))
        self.global_lockout_threshold = max(1, int(global_lockout_threshold))
        self.global_min_distinct_ips = max(1, int(global_min_distinct_ips))
        self.global_lock_seconds = max(1, int(global_lock_seconds))
        self.backoff_after = max(0, int(backoff_after))
        self.max_backoff_seconds = max(0.0, float(max_backoff_seconds))
        self._sleep = sleep_fn or time.sleep

        loaded = load_session_secret(
            env,
            secret_env,
            require=require_secret,
            allow_ephemeral=allow_ephemeral,
            min_length=min_secret_length,
        )
        self.ephemeral_secret = loaded.ephemeral
        self._secret = loaded.value

        # Persistent stores. All three share one connection so the whole auth
        # state lives in the same database.
        self.users = UserStore(db_path_or_conn)
        self.conn = self.users.conn
        self.sessions = SessionStore(self.conn)
        self.rate_limits = RateLimitStore(self.conn, now_fn=self.now_fn)
        self.login_guard = LoginGuard(self.conn, now_fn=self.now_fn)

        if users is None:
            if self._explicit_db:
                users = self.users.list()
            else:
                from .fixtures import seeded_users

                users = seeded_users(env, use_demo_fallback=use_demo_fallback)
        for user in users:
            self.add_user(user)

    # -------------------------------------------------------------- #
    # user store
    # -------------------------------------------------------------- #
    def add_user(self, user: User) -> None:
        """Persist ``user`` (idempotent on ``username``)."""
        self.users.create_user(
            user.username,
            user.password_hash,
            roles=list(getattr(user, "roles", []) or []),
            locations=list(getattr(user, "locations", []) or []),
            active=bool(getattr(user, "active", True)),
            mfa_required=bool(getattr(user, "mfa_required", False)),
            salt=_salt_from_hash(user.password_hash),
        )

    def get_user(self, username: str) -> Optional[User]:
        """Always read from the persistent store (role changes take effect)."""
        if username is None:
            return None
        return self.users.get(str(username))

    def list_users(self) -> List[User]:
        return self.users.list()

    # -------------------------------------------------------------- #
    # sessions
    # -------------------------------------------------------------- #
    def _sign(self, payload_b64: str) -> str:
        return hmac.new(self._secret, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()

    def login(
        self,
        username: str,
        password: str,
        *,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[str]:
        """Verify credentials and return a signed session token, else ``None``.

        The session is persisted (``jti`` + expiry) so it can be revoked later.
        Unknown usernames still run a dummy hash comparison so the response time
        does not reveal whether the account exists. The password is never
        logged.
        """
        result = self.login_with_status(
            username, password, ip=ip, user_agent=user_agent)
        return result.get("token") if result.get("ok") else None

    # ---------------------------------------------------------------- #
    # T25: brute-force lockout / progressive backoff
    # ---------------------------------------------------------------- #
    def _issue_session(self, user: User, ip: Any, user_agent: Any) -> str:
        """Persist a fresh session and return its signed token."""
        now = int(self.now_fn())
        jti = secrets.token_hex(16)
        expires = now + self.ttl
        payload = {"u": user.username, "iat": now, "exp": expires, "jti": jti}
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        # Persist BEFORE returning so the token is immediately revocable.
        self.sessions.create(
            jti, user.username, now, expires,
            ip=str(ip) if ip is not None else None,
            user_agent=str(user_agent) if user_agent is not None else None,
        )
        return f"{payload_b64}.{self._sign(payload_b64)}"

    def lock_retry_after(self, username: str, ip: Any = None) -> int:
        """Seconds until the caller may try again (0 = not locked)."""
        now = int(self.now_fn())
        for scope, key in (
            (LOGIN_SCOPE_USER_IP, LoginGuard.pair_key(username, ip)),
            (LOGIN_SCOPE_USER, str(username)),
        ):
            until = self.login_guard.locked_until(scope, key)
            if until is not None and until > now:
                return max(1, until - now)
        return 0

    def backoff_delay(self, failures: int) -> float:
        """Progressive response delay for a failed attempt (0 below the floor)."""
        if failures <= self.backoff_after:
            return 0.0
        step = failures - self.backoff_after
        return min(self.max_backoff_seconds, 0.25 * (2 ** (step - 1)))

    def _note_failure(self, username: str, ip: Any, now: int) -> Dict[str, Any]:
        """Record a failed login, escalate the lock, return a result dict."""
        pair = LoginGuard.pair_key(username, ip)
        failures = self.login_guard.note_failure(
            LOGIN_SCOPE_USER_IP, pair, self.failure_window_seconds)
        delay = self.backoff_delay(failures)
        if delay:
            self._sleep(delay)

        # (a) Lock THIS (user, address) pair once it crosses the threshold.
        if failures >= self.lockout_threshold:
            if not self.login_guard.locked_until(LOGIN_SCOPE_USER_IP, pair):
                escalations = failures - self.lockout_threshold
                duration = min(
                    self.lockout_base_seconds * (2 ** escalations),
                    self.lockout_max_seconds,
                )
                self.login_guard.lock(LOGIN_SCOPE_USER_IP, pair, now + duration)

        # (b) The ACCOUNT is throttled only when failures are spread over
        #     several distinct addresses. One attacker on one address can never
        #     trigger this, so a legitimate admin cannot be locked out by it.
        total, distinct = self.login_guard.account_failures(
            username, self.failure_window_seconds)
        if (total >= self.global_lockout_threshold
                and distinct >= self.global_min_distinct_ips
                and not self.login_guard.locked_until(LOGIN_SCOPE_USER, username)):
            self.login_guard.lock(
                LOGIN_SCOPE_USER, username, now + self.global_lock_seconds)

        retry = self.lock_retry_after(username, ip)
        return {
            "ok": False,
            "token": None,
            "locked": bool(retry),
            "retry_after": retry,
            "failures": failures,
            "reason": "locked" if retry else "invalid_credentials",
        }

    def lock_status(self, username: str, ip: Any = None) -> Dict[str, Any]:
        """Introspection helper for tools/tests (never returns a password)."""
        total, distinct = self.login_guard.account_failures(
            username, self.failure_window_seconds)
        return {
            "username": str(username),
            "pair_failures": self.login_guard.failures(
                LOGIN_SCOPE_USER_IP,
                LoginGuard.pair_key(username, ip),
                self.failure_window_seconds),
            "account_failures": total,
            "distinct_addresses": distinct,
            "retry_after": self.lock_retry_after(username, ip),
            "account_locked_until": self.login_guard.locked_until(
                LOGIN_SCOPE_USER, str(username)),
        }

    def unlock_user(self, username: str) -> int:
        """Clear every counter and lock for one account (recovery path).

        Intended to be run locally by an operator (see
        ``scripts/manage_users.py unlock``); it is deliberately NOT exposed over
        HTTP, so an attacker cannot use it to reset their own throttle.
        """
        return self.login_guard.clear_account(str(username))

    def login_with_status(
        self,
        username: str,
        password: str,
        *,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Like :meth:`login` but reports the lock/throttle state.

        Returns ``{'ok', 'token', 'locked', 'retry_after', 'failures',
        'reason'}``. A locked caller is rejected BEFORE the password is checked,
        and the failure reply is identical for an unknown username and a wrong
        password, so neither the response nor its timing reveals whether an
        account exists. The password is never logged or echoed.
        """
        now = int(self.now_fn())
        retry = self.lock_retry_after(username, ip)
        if retry:
            # Still hammering while locked: count the attempt and EXTEND the
            # lock (capped) so a sustained attack backs off further instead of
            # being rewarded with a fresh window every base interval.
            pair = LoginGuard.pair_key(username, ip)
            failures = self.login_guard.note_failure(
                LOGIN_SCOPE_USER_IP, pair, self.failure_window_seconds)
            if failures > self.lockout_threshold:
                escalations = failures - self.lockout_threshold
                duration = min(
                    self.lockout_base_seconds * (2 ** escalations),
                    self.lockout_max_seconds,
                )
                self.login_guard.lock(LOGIN_SCOPE_USER_IP, pair, now + duration)
            delay = self.backoff_delay(failures)
            if delay:
                self._sleep(delay)
            return {
                "ok": False,
                "token": None,
                "locked": True,
                "retry_after": self.lock_retry_after(username, ip),
                "failures": failures,
                "reason": "locked",
            }

        user = self.users.get(str(username))
        if user is None:
            # Constant-cost comparison against a throwaway hash.
            verify_password(str(password), _DUMMY_HASH)
            return self._note_failure(str(username), ip, now)
        if not verify_password(str(password), user.password_hash):
            return self._note_failure(str(username), ip, now)

        # Success clears this source's failures (never the other sources').
        self.login_guard.clear(LOGIN_SCOPE_USER_IP,
                               LoginGuard.pair_key(username, ip))
        return {
            "ok": True,
            "token": self._issue_session(user, ip, user_agent),
            "locked": False,
            "retry_after": 0,
            "failures": 0,
            "reason": "ok",
        }

    def _payload(self, token: str) -> Optional[Dict[str, Any]]:
        if not isinstance(token, str) or token.count(".") != 1:
            return None
        payload_b64, signature = token.split(".", 1)
        if not payload_b64 or not signature:
            return None
        expected = self._sign(payload_b64)
        if not hmac.compare_digest(expected, signature):
            return None
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def validate(self, token: Optional[str]) -> Optional[User]:
        """Return the :class:`User` for a valid session, else ``None``.

        A token is only valid when the HMAC verifies, it is unexpired AND its
        persisted session row exists, is not revoked and is not expired -- so
        logout/revocation take effect immediately.
        """
        data = self._payload(token) if token else None
        if not data:
            return None
        now = int(self.now_fn())
        exp = data.get("exp")
        if not isinstance(exp, int) or exp <= now:
            return None
        jti = data.get("jti")
        if not isinstance(jti, str) or not jti:
            # Tokens issued before Group 6B carry no jti and cannot be checked
            # against a revocation list -> refuse them (fail closed).
            return None
        if not self.sessions.is_active(jti, now):
            return None
        return self.users.get(str(data.get("u")))

    def logout(self, token: Optional[str]) -> bool:
        """Revoke the session carried by ``token`` (idempotent)."""
        data = self._payload(token) if token else None
        if not data:
            return False
        jti = data.get("jti")
        if not isinstance(jti, str) or not jti:
            return False
        return self.sessions.revoke(jti)

    def revoke_all(self, username: str) -> int:
        """Revoke every live session of ``username``."""
        return self.sessions.revoke_all(username)

    def purge_expired_sessions(self) -> int:
        """Delete expired session rows (housekeeping)."""
        return self.sessions.purge_expired(int(self.now_fn()))

    def expires_at(self, token: str) -> Optional[int]:
        data = self._payload(token)
        if not data or not isinstance(data.get("exp"), int):
            return None
        return int(data["exp"])

    def csrf_token(self, session_token: str) -> str:
        """Derive the CSRF token for a session (stateless double-submit)."""
        return hmac.new(
            self._secret, f"csrf:{session_token}".encode("utf-8"), hashlib.sha256
        ).hexdigest()

    # -------------------------------------------------------------- #
    # rate limiting (persistent)
    # -------------------------------------------------------------- #
    def hit_rate_limit(
        self, bucket: str, window_seconds: int, limit: int
    ):
        """Delegate to the persistent :class:`RateLimitStore`."""
        return self.rate_limits.hit(bucket, window_seconds, limit)

    # -------------------------------------------------------------- #
    # MFA -- READINESS ONLY, NOT IMPLEMENTED
    # -------------------------------------------------------------- #
    def begin_mfa(self, user: Optional[User]) -> None:
        """Placeholder. MFA is NOT implemented in this build."""
        raise NotImplementedError(
            "MFA is not implemented: mfa_required is readiness metadata only. "
            "begin_mfa()/verify_mfa() are deliberate stubs -- do not enable "
            "or advertise MFA until a real second-factor flow is built."
        )

    def verify_mfa(self, user: Optional[User], code: str) -> None:
        """Placeholder. MFA is NOT implemented in this build."""
        raise NotImplementedError(
            "MFA is not implemented: verify_mfa() is a deliberate stub and no "
            "second-factor code is ever checked. Do not claim MFA support."
        )

    # -------------------------------------------------------------- #
    # authorization
    # -------------------------------------------------------------- #
    def authorize(
        self,
        user: Optional[User],
        action: str,
        location_id: Optional[Union[str, int]] = None,
    ) -> bool:
        """Server-side permission check.

        An inactive user (``active=0``) is denied everywhere. ``admin`` users
        may do anything anywhere. ``owner``/``reviewer`` are limited to the
        actions in :data:`ROLE_PERMISSIONS` AND to the locations listed on their
        (persisted) user record; a non-admin check without a concrete location
        always fails. Unknown actions always fail.
        """
        if user is None:
            return False
        if not getattr(user, "active", True):
            return False
        if action not in ACTIONS:
            return False
        perms = user.permitted_actions()
        if action not in perms:
            return False
        if "admin" in perms:
            return True
        if location_id is None:
            return False
        return str(location_id) in user.location_set()

    def can_manage_users(self, user: Optional[User]) -> bool:
        """Only an ``admin`` may manage users (reviewers never can)."""
        return (
            user is not None
            and getattr(user, "active", True)
            and "admin" in user.permitted_actions()
        )
