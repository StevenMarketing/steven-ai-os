"""Durable offline task lifecycle for ChatGPT -> Hermes coordination (#76).

Increment 1 of the control-plane work sketched in issue #74. This module is an
**inert library**: it turns a stream of lifecycle *events* (as data) into an
authoritative task state, durably, and recommends a next safe action. It never
executes anything, never dispatches, never notifies, never opens a socket and
never touches a scheduler. Interactive ChatGPT is not a daemon; a VPS
coordinator can call this library, but nothing here calls out.

Design rules (enforced in code, not by convention):

* **Standard library only.** ``sqlite3`` + ``json`` + pure functions.
* **Events are data.** Every free-text field is passed through
  :func:`gbp.security.redact.redact_text` before it is stored or read back.
* **Immutable event IDs.** An event id is written once. Re-delivering the same
  id with the same content is idempotent (a no-op); re-delivering the same id
  with *different* content is rejected (``conflicting_duplicate``).
* **Monotonic, fail-closed transitions.** A known-good edge advances the state.
  An event that would *regress* the state is recorded as ``out_of_order`` (the
  state does not move). Any other illegal edge is ``invalid_transition`` and the
  event is refused -- we never guess.
* **Queue acceptance is not a claim.** A ``queued`` event only records that the
  bridge accepted the task. A ``claimed``/``running`` event is what a worker
  produces. The two are never conflated.
* **Freshness is explicit.** ``evaluate_freshness`` needs a supplied clock and a
  supplied heartbeat; a missing/expired heartbeat is ``unknown``/``stale``,
  never ``fresh`` -- and a task is never reported ``done`` on a stale heartbeat.
* **Completion needs evidence.** Reaching ``done`` requires at least one safe
  issue/PR evidence link.
* **No side effects.** :func:`next_safe_action` returns a recommendation string
  and nothing else.

The store is deterministic: callers pass ``now`` (a timezone-aware datetime or a
POSIX timestamp) in, and the same inputs always produce the same rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from gbp.security.redact import redact_text

__all__ = [
    "STATES",
    "TERMINAL_STATES",
    "FRESHNESS_STATES",
    "ALLOWED_TRANSITIONS",
    "REQUIRED_EVENT_FIELDS",
    "TaskLifecycleError",
    "normalize_timestamp",
    "utc_now",
    "parse_timestamp",
    "evaluate_freshness",
    "next_safe_action",
    "build_evidence",
    "TaskLifecycleStore",
]

#: The complete lifecycle vocabulary, in the order the issue specifies.
STATES: Tuple[str, ...] = (
    "queued",
    "claimed",
    "running",
    "testing",
    "blocked",
    "review_ready",
    "done",
    "failed",
)

#: States from which no further transition is possible.
TERMINAL_STATES: Tuple[str, ...] = ("done", "failed")

#: States a task enters only after the *bridge accepted* the request (``queued``)
#: versus after a *worker* actually picked the task up (``claimed`` and beyond).
BRIDGE_QUEUED_STATE = "queued"

#: Freshness verdicts. ``unknown`` is used whenever we cannot prove freshness.
FRESHNESS_STATES: Tuple[str, ...] = ("fresh", "stale", "unknown")

#: Directed graph of allowed forward transitions. Anything not listed here is a
#: fail-closed ``invalid_transition`` (a move to an *earlier* state is reported
#: separately as ``out_of_order`` and is a no-op).
ALLOWED_TRANSITIONS: Dict[str, frozenset] = {
    "queued": frozenset({"claimed", "blocked", "failed"}),
    "claimed": frozenset({"running", "blocked", "failed"}),
    "running": frozenset({"testing", "review_ready", "blocked", "failed"}),
    "testing": frozenset({"review_ready", "blocked", "failed"}),
    "blocked": frozenset({"running", "testing", "failed"}),
    "review_ready": frozenset({"done", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
}

#: Forward rank used to decide whether an out-of-order event would regress state.
#: ``blocked`` shares testing's rank because it is a sideways park, not progress.
_RANK: Dict[str, int] = {
    "queued": 0,
    "claimed": 1,
    "running": 2,
    "testing": 3,
    "blocked": 3,
    "review_ready": 4,
    "done": 5,
    "failed": 5,
}

#: Every event must carry these fields; anything else is a malformed payload.
REQUIRED_EVENT_FIELDS: Tuple[str, ...] = ("event_id", "task_id", "status")

#: Free-text fields that are redacted on the way in.
_FREE_TEXT_FIELDS: Tuple[str, ...] = ("detail", "note", "summary", "reason")

_ALLOWED_EVIDENCE_HOSTS = ("github.com",)


class TaskLifecycleError(ValueError):
    """Raised only for programmer errors (e.g. unknown status); data errors do
    not raise -- they are returned as a reject code so callers can fail closed
    without a traceback."""


# ---------------------------------------------------------------------------
# Time helpers -- deterministic, UTC, timezone-aware
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def normalize_timestamp(value: Any) -> str:
    """Coerce ``value`` to a canonical UTC ISO-8601 string (``...Z``).

    Accepts an aware/naive datetime, a POSIX timestamp (int/float) or a string.
    Naive datetimes are treated as UTC. Raises :class:`TaskLifecycleError` on
    anything unparseable.
    """
    dt = parse_timestamp(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_timestamp(value: Any) -> datetime:
    """Parse ``value`` into a timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, bool):  # bool is an int subclass -- reject explicitly
        raise TaskLifecycleError("boolean is not a timestamp")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:  # pragma: no cover - message passthrough
            raise TaskLifecycleError(f"unparseable timestamp: {value!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise TaskLifecycleError(f"unsupported timestamp type: {type(value).__name__}")


def _coerce_now(now: Any) -> datetime:
    return parse_timestamp(now) if now is not None else utc_now()


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def evaluate_freshness(*, now: Any, heartbeat: Any, ttl_seconds: float) -> Dict[str, Any]:
    """Classify a heartbeat against a supplied clock and TTL.

    Returns ``{state, age_seconds, reason}`` where ``state`` is one of
    :data:`FRESHNESS_STATES`. A missing, malformed or future-dated heartbeat is
    ``unknown``; an expired one is ``stale``. It is **never** ``fresh`` unless a
    present heartbeat is inside the TTL window.
    """
    if ttl_seconds is None or ttl_seconds < 0:
        return {"state": "unknown", "age_seconds": None,
                "reason": "no_valid_ttl"}
    if heartbeat is None or (isinstance(heartbeat, str) and not heartbeat.strip()):
        return {"state": "unknown", "age_seconds": None, "reason": "missing_heartbeat"}
    try:
        hb = parse_timestamp(heartbeat)
    except TaskLifecycleError:
        return {"state": "unknown", "age_seconds": None, "reason": "malformed_heartbeat"}
    now_dt = _coerce_now(now)
    age = (now_dt - hb).total_seconds()
    if age < 0:
        # A heartbeat from the future is not evidence we can trust.
        return {"state": "unknown", "age_seconds": age, "reason": "future_heartbeat"}
    if age > float(ttl_seconds):
        return {"state": "stale", "age_seconds": age, "reason": "heartbeat_expired"}
    return {"state": "fresh", "age_seconds": age, "reason": "ok"}


# ---------------------------------------------------------------------------
# Evidence links
# ---------------------------------------------------------------------------

def _valid_github_link(url: Any, kind: str) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return False
    if parts.scheme != "https" or parts.netloc.lower() not in _ALLOWED_EVIDENCE_HOSTS:
        return False
    segments = [s for s in parts.path.split("/") if s]
    # /<owner>/<repo>/(issues|pull)/<number>
    if len(segments) != 4:
        return False
    expected = "issues" if kind == "issue" else "pull"
    return segments[2] == expected and segments[3].isdigit()


def build_evidence(*, issue: Any = None, pr: Any = None) -> Dict[str, Any]:
    """Validate and expose safe issue/PR evidence links.

    Malformed or non-GitHub URLs are dropped and listed under ``rejected`` -- the
    caller sees exactly what was refused. ``present`` is True only when at least
    one valid link survived.
    """
    out: Dict[str, Any] = {"issue_url": None, "pr_url": None,
                           "present": False, "rejected": []}
    if issue is not None:
        if _valid_github_link(issue, "issue"):
            out["issue_url"] = issue.strip()
        else:
            out["rejected"].append("issue_url")
    if pr is not None:
        if _valid_github_link(pr, "pr"):
            out["pr_url"] = pr.strip()
        else:
            out["rejected"].append("pr_url")
    out["present"] = bool(out["issue_url"] or out["pr_url"])
    return out


# ---------------------------------------------------------------------------
# Next safe action -- recommendation only
# ---------------------------------------------------------------------------

_NEXT_ACTION: Dict[str, str] = {
    "queued": "await_worker_claim",
    "claimed": "begin_run",
    "running": "continue_run",
    "testing": "finish_tests_or_publish_evidence",
    "blocked": "resolve_blocker_then_resume",
    "review_ready": "await_human_review",
    "done": "no_action_terminal",
    "failed": "await_human_retry_decision",
}


def next_safe_action(status: Any) -> str:
    """Return the single recommended next action for ``status`` (never executes)."""
    if status not in _NEXT_ACTION:
        return "refuse_unknown_state"
    return _NEXT_ACTION[str(status)]


# ---------------------------------------------------------------------------
# Durable store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    task_id      TEXT NOT NULL,
    request_id   TEXT,
    status       TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload      TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_state (
    task_id     TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    last_event  TEXT NOT NULL
);
"""


def _payload_hash(event: Dict[str, Any]) -> str:
    blob = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _clean_event(event: Any) -> Dict[str, Any]:
    """Validate + redact a raw event dict; raise on malformed payloads."""
    if not isinstance(event, dict):
        raise TaskLifecycleError("event must be a mapping")
    for field in REQUIRED_EVENT_FIELDS:
        value = event.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise TaskLifecycleError(f"event missing required field: {field}")
    status = str(event["status"]).strip()
    if status not in STATES:
        raise TaskLifecycleError(f"unknown status: {status!r}")
    clean: Dict[str, Any] = {
        "event_id": str(event["event_id"]).strip(),
        "task_id": str(event["task_id"]).strip(),
        "status": status,
    }
    if event.get("request_id") not in (None, ""):
        clean["request_id"] = str(event["request_id"]).strip()
    if event.get("timestamp") not in (None, ""):
        clean["timestamp"] = normalize_timestamp(event["timestamp"])
    for field in _FREE_TEXT_FIELDS:
        if event.get(field) not in (None, ""):
            clean[field] = redact_text(str(event[field]))
    ev = event.get("evidence")
    if ev is not None:
        if not isinstance(ev, dict):
            raise TaskLifecycleError("evidence must be a mapping")
        clean["evidence"] = build_evidence(issue=ev.get("issue_url"), pr=ev.get("pr_url"))
    return clean


class TaskLifecycleStore:
    """A standard-library SQLite event store plus a pure status reducer.

    Open the same path twice (a "restart") and the state and dedupe set survive.
    ``path=":memory:"`` (the default) is for tests and throwaway evaluation.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- context manager ---------------------------------------------------
    def __enter__(self) -> "TaskLifecycleStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass

    # -- writes ------------------------------------------------------------
    def append_event(self, event: Any, *, now: Any = None) -> Dict[str, Any]:
        """Record one event and reduce it into the task state.

        Returns a result dict::

            {"accepted": bool, "code": str, "deduped": bool,
             "task_id": str, "status": str, "event_id": str}

        Reject codes (``accepted=False``): ``malformed_event``, ``duplicate``
        is *not* a rejection -- it returns ``accepted=True, deduped=True``.
        A conflicting duplicate is ``accepted=False, code="conflicting_duplicate"``.
        """
        try:
            clean = _clean_event(event)
        except TaskLifecycleError as exc:
            return {"accepted": False, "code": "malformed_event", "deduped": False,
                    "task_id": str((event or {}).get("task_id", "")) if isinstance(event, dict) else "",
                    "status": "", "event_id": "", "detail": str(exc)}

        now_dt = _coerce_now(now)
        recorded_at = normalize_timestamp(now_dt)
        event_id = clean["event_id"]
        task_id = clean["task_id"]
        digest = _payload_hash(clean)

        existing = self._conn.execute(
            "SELECT payload_hash FROM task_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] == digest:
                state = self.task_state(task_id)
                return {"accepted": True, "code": "duplicate", "deduped": True,
                        "task_id": task_id, "status": state["status"],
                        "event_id": event_id}
            return {"accepted": False, "code": "conflicting_duplicate", "deduped": False,
                    "task_id": task_id, "status": "", "event_id": event_id}

        current = self._conn.execute(
            "SELECT status FROM task_state WHERE task_id = ?", (task_id,)
        ).fetchone()
        current_status = current["status"] if current else None
        target = clean["status"]

        if current_status is None:
            new_status = target
        elif target == current_status:
            new_status = current_status  # idempotent at the state level
        elif target in ALLOWED_TRANSITIONS.get(current_status, frozenset()):
            new_status = target
        elif _RANK.get(target, 99) < _RANK.get(current_status, 0):
            # Would regress: record the event but do not move the state.
            self._insert_event(clean, digest, recorded_at)
            self._conn.commit()
            return {"accepted": True, "code": "out_of_order", "deduped": False,
                    "task_id": task_id, "status": current_status, "event_id": event_id}
        else:
            return {"accepted": False, "code": "invalid_transition", "deduped": False,
                    "task_id": task_id, "status": current_status, "event_id": event_id}

        if new_status == "done":
            evidence = clean.get("evidence") or {}
            if not evidence.get("present"):
                return {"accepted": False, "code": "completion_requires_evidence",
                        "deduped": False, "task_id": task_id,
                        "status": current_status, "event_id": event_id}

        self._insert_event(clean, digest, recorded_at)
        self._conn.execute(
            "INSERT INTO task_state (task_id, status, updated_at, last_event) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, "
            "updated_at=excluded.updated_at, last_event=excluded.last_event",
            (task_id, new_status, recorded_at, event_id),
        )
        self._conn.commit()
        return {"accepted": True, "code": "applied", "deduped": False,
                "task_id": task_id, "status": new_status, "event_id": event_id}

    def _insert_event(self, clean: Dict[str, Any], digest: str, recorded_at: str) -> None:
        event_ts = clean.get("timestamp") or recorded_at
        self._conn.execute(
            "INSERT INTO task_events (event_id, task_id, request_id, status, timestamp, "
            "payload_hash, payload, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (clean["event_id"], clean["task_id"], clean.get("request_id"),
             clean["status"], event_ts, digest,
             json.dumps(clean, sort_keys=True), recorded_at),
        )

    # -- reads -------------------------------------------------------------
    def task_state(self, task_id: str) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT task_id, status, updated_at, last_event FROM task_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return {"task_id": task_id, "status": None, "known": False,
                    "updated_at": None, "last_event": None,
                    "next_safe_action": "refuse_unknown_state"}
        return {"task_id": row["task_id"], "status": row["status"], "known": True,
                "updated_at": row["updated_at"], "last_event": row["last_event"],
                "next_safe_action": next_safe_action(row["status"])}

    def events(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def task_freshness(self, task_id: str, *, now: Any, ttl_seconds: float,
                       heartbeat: Any = None) -> Dict[str, Any]:
        """Freshness of a task, never ``fresh``/``done`` off a bad heartbeat."""
        state = self.task_state(task_id)
        hb = heartbeat if heartbeat is not None else state.get("updated_at")
        verdict = evaluate_freshness(now=now, heartbeat=hb, ttl_seconds=ttl_seconds)
        terminal_done = state.get("status") == "done"
        if terminal_done and verdict["state"] != "fresh":
            # A done task whose only evidence is stale is downgraded to unknown.
            verdict = {"state": "unknown", "age_seconds": verdict.get("age_seconds"),
                       "reason": "done_without_fresh_heartbeat"}
        return {**verdict, "task_id": task_id,
                "status": state.get("status"), "next_safe_action": state.get("next_safe_action")}

    def snapshot(self) -> Dict[str, Any]:
        """A small, offline, JSON-safe snapshot (the issue's example artifact)."""
        rows = self._conn.execute(
            "SELECT task_id, status FROM task_state ORDER BY task_id"
        ).fetchall()
        tasks = {r["task_id"]: {"status": r["status"],
                                "next_safe_action": next_safe_action(r["status"])}
                 for r in rows}
        count = self._conn.execute("SELECT COUNT(*) AS n FROM task_events").fetchone()["n"]
        return {"tasks": tasks, "task_count": len(tasks), "event_count": int(count)}
