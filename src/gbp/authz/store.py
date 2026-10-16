"""Persistent (SQLite-backed) user / session / rate-limit stores.

Standard library only. Everything lives in the SQLite database the caller
passes in (a ``sqlite3.Connection``, a path, or ``None`` for a private
in-memory DB), so a **restart or a second process sees the same state** --
that is the whole point of Group 6B (the old service kept sessions and rate
counters in process memory, so a restart silently logged everyone out and
reset every throttle).

Tables (created with ``CREATE TABLE IF NOT EXISTS``, backward compatible with
an existing DB -- see ``sql/schema.sql``)::

    users(username PK, password_hash, salt, roles_json, locations_json,
          active, mfa_required, created_at, updated_at)
    sessions(jti PK, username, issued_at, expires_at, revoked_at, ip, user_agent)
    rate_limits(bucket, window_start, count, PRIMARY KEY(bucket, window_start))

Passwords are stored exactly like :func:`gbp.authz.models.hash_password`
produces (``pbkdf2_sha256$iters$salt$hash``); the ``salt`` column redundantly
keeps the salt for inspection but the hash string is self-describing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .models import User, verify_password

__all__ = ["UserStore", "SessionStore", "RateLimitStore", "LoginGuard",
           "ensure_auth_tables"]

#: DDL for the three Group 6B tables (idempotent).
_AUTH_DDL = """
CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    password_hash   TEXT NOT NULL,
    salt            TEXT,
    roles_json      TEXT NOT NULL DEFAULT '[]',
    locations_json  TEXT NOT NULL DEFAULT '[]',
    active          INTEGER NOT NULL DEFAULT 1,
    mfa_required    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    jti         TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    issued_at   INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    revoked_at  INTEGER,
    ip          TEXT,
    user_agent  TEXT
);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket        TEXT NOT NULL,
    window_start  INTEGER NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, window_start)
);

CREATE TABLE IF NOT EXISTS login_guard (
    scope           TEXT NOT NULL,
    key             TEXT NOT NULL,
    failures        INTEGER NOT NULL DEFAULT 0,
    window_start    INTEGER,
    last_failure_at INTEGER,
    locked_until    INTEGER,
    PRIMARY KEY (scope, key)
);
"""


def _coerce_connection(db: Any) -> sqlite3.Connection:
    """Accept a Connection, a Repository-like object, or a path/``:memory:``."""
    if isinstance(db, sqlite3.Connection):
        return db
    conn = getattr(db, "conn", None)
    if isinstance(conn, sqlite3.Connection):
        return conn
    if db is None:
        return sqlite3.connect(":memory:", check_same_thread=False)
    return sqlite3.connect(str(db), check_same_thread=False)


def ensure_auth_tables(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Create the Group 6B tables if they are missing (idempotent)."""
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    conn.executescript(_AUTH_DDL)
    conn.commit()
    return conn


def _as_int_flag(value: Any) -> int:
    return 1 if value else 0


class UserStore:
    """Persistent user records + PBKDF2 password verification."""

    def __init__(self, db: Any = None):
        self.conn = ensure_auth_tables(_coerce_connection(db))
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- #
    def create_user(
        self,
        username: str,
        password_hash: str,
        roles: Optional[List[str]] = None,
        locations: Optional[List[str]] = None,
        *,
        active: bool = True,
        mfa_required: bool = False,
        salt: Optional[str] = None,
    ) -> User:
        """Insert or replace a user record and return the :class:`User`."""
        roles = list(roles or [])
        locations = list(locations or [])
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO users(username, password_hash, salt, roles_json,
                                  locations_json, active, mfa_required,
                                  created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    salt          = excluded.salt,
                    roles_json    = excluded.roles_json,
                    locations_json = excluded.locations_json,
                    active        = excluded.active,
                    mfa_required  = excluded.mfa_required,
                    updated_at    = datetime('now')
                """,
                (
                    str(username), password_hash, salt,
                    json.dumps(roles), json.dumps(locations),
                    _as_int_flag(active), _as_int_flag(mfa_required),
                ),
            )
            self.conn.commit()
        return User(
            username=str(username), password_hash=password_hash, roles=roles,
            locations=locations, active=bool(active),
            mfa_required=bool(mfa_required),
        )

    def get(self, username: str) -> Optional[User]:
        if username is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ?", (str(username),)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    def list(self) -> List[User]:
        rows = self.conn.execute(
            "SELECT * FROM users ORDER BY username"
        ).fetchall()
        return [self._row_to_user(r) for r in rows]

    def set_active(self, username: str, active: bool) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE users SET active = ?, updated_at = datetime('now')"
                " WHERE username = ?",
                (_as_int_flag(active), str(username)),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def update_roles(self, username: str, roles: List[str]) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE users SET roles_json = ?, updated_at = datetime('now')"
                " WHERE username = ?",
                (json.dumps(list(roles or [])), str(username)),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def update_locations(self, username: str, locations: List[str]) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE users SET locations_json = ?, updated_at = datetime('now')"
                " WHERE username = ?",
                (json.dumps([str(loc) for loc in (locations or [])]), str(username)),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def verify_password(self, username: str, password: str) -> bool:
        """Constant-time PBKDF2 check against the stored hash for ``username``."""
        user = self.get(username)
        if user is None:
            return False
        return verify_password(password, user.password_hash)

    # ---------------------------------------------------------------- #
    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            username=str(row["username"]),
            password_hash=str(row["password_hash"]),
            roles=list(json.loads(row["roles_json"] or "[]")),
            locations=list(json.loads(row["locations_json"] or "[]")),
            active=bool(row["active"]),
            mfa_required=bool(row["mfa_required"]),
        )


class SessionStore:
    """Persistent session records -- revocation actually invalidates tokens."""

    def __init__(self, db: Any = None):
        self.conn = ensure_auth_tables(_coerce_connection(db))
        self._lock = threading.Lock()

    def create(
        self,
        jti: str,
        username: str,
        issued_at: int,
        expires_at: int,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO sessions(jti, username, issued_at, expires_at,
                                     revoked_at, ip, user_agent)
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(jti) DO UPDATE SET
                    username   = excluded.username,
                    issued_at  = excluded.issued_at,
                    expires_at = excluded.expires_at,
                    revoked_at = NULL,
                    ip         = excluded.ip,
                    user_agent = excluded.user_agent
                """,
                (str(jti), str(username), int(issued_at), int(expires_at),
                 ip, user_agent),
            )
            self.conn.commit()

    def get(self, jti: str) -> Optional[sqlite3.Row]:
        if not jti:
            return None
        return self.conn.execute(
            "SELECT * FROM sessions WHERE jti = ?", (str(jti),)
        ).fetchone()

    def is_active(self, jti: str, now: Optional[int] = None) -> bool:
        """True only when the row exists, is not revoked and is not expired."""
        row = self.get(jti)
        if row is None:
            return False
        if row["revoked_at"] is not None:
            return False
        now = int(now if now is not None else time.time())
        return int(row["expires_at"]) > now

    def revoke(self, jti: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE sessions SET revoked_at = ?"
                " WHERE jti = ? AND revoked_at IS NULL",
                (int(time.time()), str(jti)),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def revoke_all(self, username: str) -> int:
        """Revoke every live session of ``username``; returns how many."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE sessions SET revoked_at = ?"
                " WHERE username = ? AND revoked_at IS NULL",
                (int(time.time()), str(username)),
            )
            self.conn.commit()
        return cur.rowcount

    def purge_expired(self, now: Optional[int] = None) -> int:
        now = int(now if now is not None else time.time())
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (now,)
            )
            self.conn.commit()
        return cur.rowcount


class RateLimitStore:
    """Fixed-window rate limiter whose counters live in SQLite.

    Because the counters are rows (not an in-process deque) a restart or a
    second worker process observes the same window, which is what makes the
    throttle meaningful behind multiple workers.
    """

    def __init__(self, db: Any = None, now_fn: Any = None):
        self.conn = ensure_auth_tables(_coerce_connection(db))
        self._lock = threading.Lock()
        self.now_fn = now_fn or time.time

    def hit(
        self, bucket: str, window_seconds: int, limit: int
    ) -> Tuple[bool, int, int]:
        """Record one hit and report ``(allowed, remaining, retry_after)``.

        ``retry_after`` is ``0`` when the request is allowed, otherwise the
        whole seconds until the current fixed window rolls over (>= 1).
        """
        window_seconds = max(1, int(window_seconds))
        limit = max(0, int(limit))
        now = int(self.now_fn())
        window_start = now - (now % window_seconds)
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO rate_limits(bucket, window_start, count)
                VALUES (?, ?, 1)
                ON CONFLICT(bucket, window_start)
                DO UPDATE SET count = count + 1
                """,
                (str(bucket), window_start),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT count FROM rate_limits WHERE bucket = ? AND window_start = ?",
                (str(bucket), window_start),
            ).fetchone()
        count = int(row["count"]) if row is not None else 1
        allowed = count <= limit
        remaining = max(0, limit - count)
        retry_after = 0
        if not allowed:
            retry_after = max(1, (window_start + window_seconds) - now)
        return allowed, remaining, retry_after

    def reset(self, bucket: Optional[str] = None) -> None:
        with self._lock:
            if bucket is None:
                self.conn.execute("DELETE FROM rate_limits")
            else:
                self.conn.execute(
                    "DELETE FROM rate_limits WHERE bucket = ?", (str(bucket),)
                )
            self.conn.commit()


#: Scope for a single (username, source address) pair.
LOGIN_SCOPE_USER_IP = "user_ip"
#: Scope for a whole account (only set when failures span several addresses).
LOGIN_SCOPE_USER = "user"


class LoginGuard:
    """Persistent login-failure ledger + temporary lock (Group 8B / T25).

    Two properties matter:

    * **Persistence** -- the counters and lock deadlines are rows, so a restart
      or a second worker sees exactly the same state.
    * **Anti-DoS** -- the per-account lock is never set by failures from a
      single address; a caller must observe failures from several distinct
      addresses before the whole account is throttled. One attacker therefore
      cannot lock a legitimate admin out.
    """

    def __init__(self, db: Any = None, now_fn: Any = None):
        self.conn = ensure_auth_tables(_coerce_connection(db))
        self._lock = threading.Lock()
        self.now_fn = now_fn or time.time

    @staticmethod
    def pair_key(username: Any, ip: Any) -> str:
        return f"{username}|{ip if ip is not None else 'unknown'}"

    def _window_start(self, window_seconds: int, now: int) -> int:
        w = max(1, int(window_seconds))
        return now - (now % w)

    def note_failure(
        self, scope: str, key: str, window_seconds: int
    ) -> int:
        """Record one failure and return the count inside the current window.

        A new window resets the counter but NEVER clears an active lock.
        """
        now = int(self.now_fn())
        start = self._window_start(window_seconds, now)
        with self._lock:
            row = self.conn.execute(
                "SELECT failures, window_start FROM login_guard"
                " WHERE scope = ? AND key = ?", (str(scope), str(key)),
            ).fetchone()
            if row is None:
                failures = 1
                self.conn.execute(
                    "INSERT INTO login_guard(scope, key, failures, window_start,"
                    " last_failure_at, locked_until) VALUES (?, ?, 1, ?, ?, NULL)",
                    (str(scope), str(key), start, now),
                )
            elif int(row["window_start"] or 0) != start:
                failures = 1
                self.conn.execute(
                    "UPDATE login_guard SET failures = 1, window_start = ?,"
                    " last_failure_at = ? WHERE scope = ? AND key = ?",
                    (start, now, str(scope), str(key)),
                )
            else:
                failures = int(row["failures"]) + 1
                self.conn.execute(
                    "UPDATE login_guard SET failures = ?, last_failure_at = ?"
                    " WHERE scope = ? AND key = ?",
                    (failures, now, str(scope), str(key)),
                )
            self.conn.commit()
        return failures

    def failures(self, scope: str, key: str, window_seconds: int) -> int:
        start = self._window_start(window_seconds, int(self.now_fn()))
        row = self.conn.execute(
            "SELECT failures, window_start FROM login_guard"
            " WHERE scope = ? AND key = ?", (str(scope), str(key)),
        ).fetchone()
        if row is None or int(row["window_start"] or 0) != start:
            return 0
        return int(row["failures"] or 0)

    def locked_until(self, scope: str, key: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT locked_until FROM login_guard WHERE scope = ? AND key = ?",
            (str(scope), str(key)),
        ).fetchone()
        if row is None or row["locked_until"] is None:
            return None
        return int(row["locked_until"])

    def lock(self, scope: str, key: str, until: int) -> None:
        """Set/extend the lock deadline for one bucket."""
        now = int(self.now_fn())
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO login_guard(scope, key, failures, window_start,
                                        last_failure_at, locked_until)
                VALUES (?, ?, 0, ?, ?, ?)
                ON CONFLICT(scope, key) DO UPDATE SET
                    locked_until = MAX(COALESCE(login_guard.locked_until, 0),
                                       excluded.locked_until)
                """,
                (str(scope), str(key), self._window_start(1, now), now,
                 int(until)),
            )
            self.conn.commit()

    def clear(self, scope: str, key: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM login_guard WHERE scope = ? AND key = ?",
                (str(scope), str(key)),
            )
            self.conn.commit()
        return cur.rowcount

    def clear_account(self, username: str) -> int:
        """Clear every counter/lock for one account (both scopes)."""
        user = str(username)
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM login_guard WHERE key = ? OR"
                " (scope = ? AND substr(key, 1, length(?) + 1) = ? || '|')",
                (user, LOGIN_SCOPE_USER_IP, user, user),
            )
            self.conn.commit()
        return cur.rowcount

    def account_failures(
        self, username: str, window_seconds: int
    ) -> Tuple[int, int]:
        """Return ``(total_failures, distinct_addresses)`` for one account.

        Uses an EXACT prefix comparison (``substr``) rather than ``LIKE`` so a
        username containing ``_``/``%`` cannot widen the match.
        """
        user = str(username)
        start = self._window_start(window_seconds, int(self.now_fn()))
        rows = self.conn.execute(
            "SELECT key, failures FROM login_guard"
            " WHERE scope = ? AND window_start = ?"
            " AND substr(key, 1, length(?) + 1) = ? || '|'",
            (LOGIN_SCOPE_USER_IP, start, user, user),
        ).fetchall()
        total = sum(int(r["failures"] or 0) for r in rows)
        distinct = len([r for r in rows if int(r["failures"] or 0) > 0])
        return total, distinct

    def reset(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM login_guard")
            self.conn.commit()
