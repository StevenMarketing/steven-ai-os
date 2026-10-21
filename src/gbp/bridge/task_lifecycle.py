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
* **Immutable event IDs.** Only scalar identifiers are accepted; booleans,
  lists, dicts, blanks and oversized values are rejected, never stringified.
  Re-delivering the same id with the same content is idempotent (a no-op);
  re-delivering the same id with *different* content is rejected
  (``conflicting_duplicate``). Unknown event/evidence fields are refused rather
  than silently dropped.
* **Queued provenance.** A task begins with a ``queued`` event carrying the
  per-task ``request_id``; later events may omit it, but a conflicting value is
  refused. The correlation survives a reopen.
* **Event-time ordering.** State moves on persisted *event* timestamps, kept
  distinct from ``recorded_at``. An older delayed event can neither move the
  state nor refresh ``updated_at``/freshness; a newer ``blocked``/resume event
  still applies. Terminal states never reopen.
* **Monotonic, fail-closed transitions.** A known-good edge advances the state.
  An event that would *regress* the state is recorded as ``out_of_order`` (the
  state does not move). Any other illegal edge is ``invalid_transition`` and the
  event is refused -- we never guess.
* **Queue acceptance is not a claim.** A ``queued`` event only records that the
  bridge accepted the task. A ``claimed``/``running`` event is what a worker
  produces. The two are never conflated.
* **Freshness is explicit.** ``evaluate_freshness`` / ``task_freshness`` need a
  supplied clock *and* a supplied heartbeat. An omitted heartbeat is ``unknown``
  -- ``state.updated_at`` is a bookkeeping timestamp, never a liveness signal.
* **Completion needs evidence.** Reaching ``done`` requires at least one safe,
  canonical issue/PR evidence link.
* **No side effects.** :func:`next_safe_action` returns a recommendation string
  and nothing else.

The store is deterministic: callers pass ``now`` (a timezone-aware datetime or a
POSIX timestamp) in, and the same inputs always produce the same rows.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from gbp.security.redact import redact_text

__all__ = [
    "STATES",
    "TERMINAL_STATES",
    "FRESHNESS_STATES",
    "ALLOWED_TRANSITIONS",
    "REQUIRED_EVENT_FIELDS",
    "MAX_IDENTIFIER_LENGTH",
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
#: separately as ``out_of_order`` and is a no-op). ``blocked -> running`` is an
#: allowed edge and is checked *before* the rank test, so a legitimate resume
#: always wins over the sideways-park ranking.
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

#: Upper bound on any identifier, so an oversized blob cannot be persisted.
MAX_IDENTIFIER_LENGTH = 200

#: Free-text fields that are redacted on the way in.
_FREE_TEXT_FIELDS: Tuple[str, ...] = ("detail", "note", "summary", "reason")

#: Every recognised top-level event field. Anything else is refused.
_EVENT_FIELDS = frozenset(
    set(REQUIRED_EVENT_FIELDS)
    | {"request_id", "timestamp", "evidence"}
    | set(_FREE_TEXT_FIELDS)
)

#: Evidence keys this store understands.
_EVIDENCE_FIELDS = frozenset({"issue_url", "pr_url"})

_ALLOWED_EVIDENCE_HOSTS = ("github.com",)

#: String spellings of non-finite numbers that must never be parsed.
_NON_FINITE_STRINGS = frozenset(
    {"nan", "+nan", "-nan", "inf", "+inf", "-inf",
     "infinity", "+infinity", "-infinity"}
)


class TaskLifecycleError(ValueError):
    """Raised only for programmer errors (e.g. unknown status); data errors do
    not raise -- they are returned as a reject code so callers can fail closed
    without a traceback."""


# ---------------------------------------------------------------------------
# Time helpers -- deterministic, UTC, timezone-aware, exception-safe
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def normalize_timestamp(value: Any) -> str:
    """Coerce ``value`` to a canonical UTC ISO-8601 string (``...Z``).

    Accepts an aware/naive datetime, a POSIX timestamp (int/float) or a string.
    Naive datetimes are treated as UTC. Raises :class:`TaskLifecycleError` on
    anything unparseable, non-finite or out of range.
    """
    dt = parse_timestamp(value)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime:
    """Parse ``value`` into a timezone-aware UTC datetime.

    Rejects booleans, non-finite numbers (NaN / +-inf), out-of-range numeric
    timestamps and blank/``nan``-style strings, always with
    :class:`TaskLifecycleError` -- never a leaked ``ValueError``,
    ``OverflowError``, ``OSError`` or ``TypeError``.
    """
    if isinstance(value, datetime):
        try:
            dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, OverflowError, TypeError, OSError) as exc:
            raise TaskLifecycleError("timestamp out of range") from exc
    if isinstance(value, bool):  # bool is an int subclass -- reject explicitly
        raise TaskLifecycleError("boolean is not a timestamp")
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, ValueError, TypeError) as exc:
            raise TaskLifecycleError("timestamp out of range") from exc
        if not math.isfinite(number):
            raise TaskLifecycleError("non-finite timestamp")
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise TaskLifecycleError("timestamp out of range") from exc
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise TaskLifecycleError("blank timestamp")
        if text.lower() in _NON_FINITE_STRINGS:
            raise TaskLifecycleError("non-finite timestamp")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except (ValueError, OverflowError) as exc:
            raise TaskLifecycleError(f"unparseable timestamp: {value!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            return dt.astimezone(timezone.utc)
        except (ValueError, OverflowError, TypeError, OSError) as exc:
            raise TaskLifecycleError("timestamp out of range") from exc
    raise TaskLifecycleError(f"unsupported timestamp type: {type(value).__name__}")


def _coerce_now(now: Any) -> datetime:
    return parse_timestamp(now) if now is not None else utc_now()


def _valid_ttl(value: Any) -> Optional[float]:
    """Return a finite, non-negative TTL as float, else ``None`` (unknown)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def evaluate_freshness(*, now: Any, heartbeat: Any, ttl_seconds: float) -> Dict[str, Any]:
    """Classify a heartbeat against a supplied clock and TTL.

    Returns ``{state, age_seconds, reason}`` where ``state`` is one of
    :data:`FRESHNESS_STATES`. A missing, malformed, future-dated or
    non-comparable heartbeat is ``unknown``; an expired one is ``stale``. It is
    **never** ``fresh`` unless a present heartbeat is inside the TTL window. A
    boolean/string/NaN/infinite TTL or an invalid ``now`` yields ``unknown``
    rather than an uncaught exception.
    """
    ttl = _valid_ttl(ttl_seconds)
    if ttl is None:
        return {"state": "unknown", "age_seconds": None, "reason": "no_valid_ttl"}
    if heartbeat is None or (isinstance(heartbeat, str) and not heartbeat.strip()):
        return {"state": "unknown", "age_seconds": None, "reason": "missing_heartbeat"}
    try:
        hb = parse_timestamp(heartbeat)
    except TaskLifecycleError:
        return {"state": "unknown", "age_seconds": None, "reason": "malformed_heartbeat"}
    try:
        if now is None:
            raise TaskLifecycleError("missing supplied clock")
        now_dt = _coerce_now(now)
    except TaskLifecycleError:
        return {"state": "unknown", "age_seconds": None, "reason": "invalid_now"}
    age = (now_dt - hb).total_seconds()
    if not math.isfinite(age):
        return {"state": "unknown", "age_seconds": None, "reason": "non_finite_age"}
    if age < 0:
        # A heartbeat from the future is not evidence we can trust.
        return {"state": "unknown", "age_seconds": age, "reason": "future_heartbeat"}
    if age > ttl:
        return {"state": "stale", "age_seconds": age, "reason": "heartbeat_expired"}
    return {"state": "fresh", "age_seconds": age, "reason": "ok"}


# ---------------------------------------------------------------------------
# Evidence links
# ---------------------------------------------------------------------------

def _canonical_github_link(url: Any, kind: str) -> Optional[str]:
    """Return the canonical safe URL, or ``None`` if it must be refused.

    Refuses non-strings, control characters, non-HTTPS/non-GitHub hosts, query
    strings, fragments, path params, ambiguous or empty path segments, wrong
    ``issues``/``pull`` kind, non-numeric, zero or leading-zero numbers.
    """
    if not isinstance(url, str):
        return None
    raw = url.strip()
    if not raw:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    try:
        parts = urlparse(raw)
    except ValueError:
        return None
    if parts.scheme != "https":
        return None
    if parts.netloc.lower() not in _ALLOWED_EVIDENCE_HOSTS:
        return None
    if parts.params or parts.query or parts.fragment:
        return None
    segments = parts.path.split("/")
    if segments and segments[0] == "":
        segments = segments[1:]
    # /<owner>/<repo>/(issues|pull)/<number>
    if len(segments) != 4:
        return None
    if any(seg in ("", ".", "..") for seg in segments):
        return None
    expected = "issues" if kind == "issue" else "pull"
    if segments[2] != expected:
        return None
    number_text = segments[3]
    # IDs need no integer conversion; keep hostile long strings bounded.
    if (not number_text or len(number_text) > 20 or
            not number_text.isascii() or not number_text.isdigit() or
            number_text[0] == "0"):
        return None
    return f"https://github.com/{segments[0]}/{segments[1]}/{expected}/{number_text}"


def build_evidence(*, issue: Any = None, pr: Any = None) -> Dict[str, Any]:
    """Validate and expose safe, canonical issue/PR evidence links.

    Malformed or non-GitHub URLs are dropped and listed under ``rejected`` -- the
    caller sees exactly what was refused. ``present`` is True only when at least
    one valid link survived. Only canonical URLs are returned.
    """
    out: Dict[str, Any] = {"issue_url": None, "pr_url": None,
                           "present": False, "rejected": []}
    if issue is not None:
        canonical = _canonical_github_link(issue, "issue")
        if canonical:
            out["issue_url"] = canonical
        else:
            out["rejected"].append("issue_url")
    if pr is not None:
        canonical = _canonical_github_link(pr, "pr")
        if canonical:
            out["pr_url"] = canonical
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
    if not isinstance(status, str) or status not in _NEXT_ACTION:
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
    request_id  TEXT,
    event_ts    TEXT,
    updated_at  TEXT NOT NULL,
    last_event  TEXT NOT NULL
);
"""

# Columns added after the first schema shipped; migrated in place on open.
_STATE_MIGRATIONS = (("request_id", "TEXT"), ("event_ts", "TEXT"))


def _payload_hash(event: Dict[str, Any]) -> str:
    blob = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _validate_identifier(value: Any, field: str) -> str:
    """Return a clean scalar identifier or raise (never silently stringify)."""
    if isinstance(value, bool):
        raise TaskLifecycleError(f"boolean is not a valid {field}")
    if isinstance(value, (list, dict, tuple, set, frozenset)):
        raise TaskLifecycleError(f"{field} must be a scalar identifier")
    if isinstance(value, float):
        raise TaskLifecycleError(f"float is not a valid {field}")
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, int):
        text = str(value)
    else:
        raise TaskLifecycleError(f"unsupported {field} type: {type(value).__name__}")
    if not text:
        raise TaskLifecycleError(f"{field} must not be blank")
    if len(text) > MAX_IDENTIFIER_LENGTH:
        raise TaskLifecycleError(f"{field} is oversized")
    return text


def _clean_event(event: Any) -> Dict[str, Any]:
    """Validate + redact a raw event dict; raise on malformed payloads."""
    if not isinstance(event, dict):
        raise TaskLifecycleError("event must be a mapping")
    for field in REQUIRED_EVENT_FIELDS:
        if event.get(field) is None:
            raise TaskLifecycleError(f"event missing required field: {field}")
    if any(not isinstance(key, str) for key in event):
        raise TaskLifecycleError("event field names must be strings")
    unknown = set(event) - _EVENT_FIELDS
    if unknown:
        raise TaskLifecycleError(f"unknown event field(s): {sorted(unknown)}")
    status = event.get("status")
    if not isinstance(status, str) or status.strip() not in STATES:
        raise TaskLifecycleError(f"unknown status: {status!r}")
    clean: Dict[str, Any] = {
        "event_id": _validate_identifier(event["event_id"], "event_id"),
        "task_id": _validate_identifier(event["task_id"], "task_id"),
        "status": status.strip(),
    }
    if event.get("request_id") not in (None, ""):
        clean["request_id"] = _validate_identifier(event["request_id"], "request_id")
    if event.get("timestamp") not in (None, ""):
        clean["timestamp"] = normalize_timestamp(event["timestamp"])
    for field in _FREE_TEXT_FIELDS:
        if event.get(field) not in (None, ""):
            clean[field] = redact_text(str(event[field]))
    ev = event.get("evidence")
    if ev is not None:
        if not isinstance(ev, dict):
            raise TaskLifecycleError("evidence must be a mapping")
        if any(not isinstance(key, str) for key in ev):
            raise TaskLifecycleError("evidence field names must be strings")
        unknown_ev = set(ev) - _EVIDENCE_FIELDS
        if unknown_ev:
            raise TaskLifecycleError(f"unknown evidence field(s): {sorted(unknown_ev)}")
        evidence = build_evidence(issue=ev.get("issue_url"), pr=ev.get("pr_url"))
        if evidence["rejected"]:
            raise TaskLifecycleError(f"malformed evidence: {evidence['rejected']}")
        clean["evidence"] = evidence
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
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        columns = {row["name"] for row in
                   self._conn.execute("PRAGMA table_info(task_state)")}
        for name, ddl in _STATE_MIGRATIONS:
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE task_state ADD COLUMN {name} {ddl}")

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

        ``duplicate`` is *not* a rejection -- it returns ``accepted=True,
        deduped=True``. A conflicting duplicate is ``accepted=False,
        code="conflicting_duplicate"``.
        """
        try:
            clean = _clean_event(event)
        except TaskLifecycleError as exc:
            return {"accepted": False, "code": "malformed_event", "deduped": False,
                    "task_id": str((event or {}).get("task_id", "")) if isinstance(event, dict) else "",
                    "status": "", "event_id": "", "detail": str(exc)}

        try:
            now_dt = _coerce_now(now)
            recorded_at = normalize_timestamp(now_dt)
        except TaskLifecycleError:
            return {"accepted": False, "code": "invalid_now", "deduped": False,
                    "task_id": clean["task_id"], "status": "",
                    "event_id": clean["event_id"]}
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
            "SELECT status, request_id, event_ts, updated_at FROM task_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        target = clean["status"]
        incoming_ts = clean.get("timestamp") or recorded_at

        if current is None:
            # Provenance: a task opens with a queued bridge acceptance and a
            # stable request_id. Anything else (including done-with-a-link) is
            # refused, so an initial event cannot bypass the lifecycle.
            if target != BRIDGE_QUEUED_STATE:
                return {"accepted": False, "code": "invalid_initial_state",
                        "deduped": False, "task_id": task_id, "status": "",
                        "event_id": event_id}
            request_id = clean.get("request_id")
            if not request_id:
                return {"accepted": False, "code": "missing_request_id",
                        "deduped": False, "task_id": task_id, "status": "",
                        "event_id": event_id}
            new_status = target
            event_ts = incoming_ts
        else:
            current_status = current["status"]
            established_request = current["request_id"]
            established_ts = current["event_ts"] or current["updated_at"]
            incoming_request = clean.get("request_id")
            if incoming_request and established_request and incoming_request != established_request:
                return {"accepted": False, "code": "conflicting_request_id",
                        "deduped": False, "task_id": task_id,
                        "status": current_status, "event_id": event_id}
            request_id = established_request or incoming_request
            if incoming_request is None:
                clean["request_id"] = request_id

            # Event-time ordering: an older delayed event is recorded but may not
            # move the state or refresh updated_at/freshness.
            if _is_older(incoming_ts, established_ts):
                self._insert_event(clean, digest, recorded_at)
                self._conn.commit()
                return {"accepted": True, "code": "out_of_order", "deduped": False,
                        "task_id": task_id, "status": current_status,
                        "event_id": event_id}

            if target == current_status:
                new_status = current_status  # idempotent at the state level
            elif target in ALLOWED_TRANSITIONS.get(current_status, frozenset()):
                new_status = target
            elif _RANK.get(target, 99) < _RANK.get(current_status, 0):
                # Would regress: record the event but do not move the state.
                self._insert_event(clean, digest, recorded_at)
                self._conn.commit()
                return {"accepted": True, "code": "out_of_order", "deduped": False,
                        "task_id": task_id, "status": current_status,
                        "event_id": event_id}
            else:
                return {"accepted": False, "code": "invalid_transition", "deduped": False,
                        "task_id": task_id, "status": current_status,
                        "event_id": event_id}
            event_ts = incoming_ts

        if new_status == "done":
            evidence = clean.get("evidence") or {}
            if not evidence.get("present"):
                return {"accepted": False, "code": "completion_requires_evidence",
                        "deduped": False, "task_id": task_id,
                        "status": current["status"] if current else "",
                        "event_id": event_id}

        if clean.get("request_id") is None:
            clean["request_id"] = request_id
        self._insert_event(clean, digest, recorded_at)
        self._conn.execute(
            "INSERT INTO task_state (task_id, status, request_id, event_ts, updated_at, last_event) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, "
            "request_id=excluded.request_id, event_ts=excluded.event_ts, "
            "updated_at=excluded.updated_at, last_event=excluded.last_event",
            (task_id, new_status, request_id, event_ts, recorded_at, event_id),
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
            "SELECT task_id, status, request_id, event_ts, updated_at, last_event "
            "FROM task_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return {"task_id": task_id, "status": None, "known": False,
                    "updated_at": None, "last_event": None,
                    "request_id": None, "event_ts": None,
                    "next_safe_action": "refuse_unknown_state"}
        return {"task_id": row["task_id"], "status": row["status"], "known": True,
                "updated_at": row["updated_at"], "last_event": row["last_event"],
                "request_id": row["request_id"], "event_ts": row["event_ts"],
                "next_safe_action": next_safe_action(row["status"])}

    def events(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def task_freshness(self, task_id: str, *, now: Any, ttl_seconds: float,
                       heartbeat: Any = None) -> Dict[str, Any]:
        """Freshness of a task, never ``fresh``/``done`` off a bad heartbeat.

        The heartbeat must be supplied explicitly: ``state.updated_at`` is a
        bookkeeping timestamp, not a liveness signal, so an omitted heartbeat is
        ``unknown`` -- never silently substituted from the recorded state.
        """
        state = self.task_state(task_id)
        if not state["known"]:
            return {"state": "unknown", "age_seconds": None, "reason": "unknown_task",
                    "task_id": task_id, "status": None,
                    "next_safe_action": state["next_safe_action"]}
        verdict = evaluate_freshness(now=now, heartbeat=heartbeat,
                                     ttl_seconds=ttl_seconds)
        terminal_done = state.get("status") == "done"
        if terminal_done and verdict["state"] != "fresh":
            # A done task whose only evidence is stale is downgraded to unknown.
            verdict = {"state": "unknown", "age_seconds": verdict.get("age_seconds"),
                       "reason": "done_without_fresh_heartbeat"}
        return {**verdict, "task_id": task_id,
                "status": state.get("status"),
                "next_safe_action": state.get("next_safe_action")}

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


def _is_older(incoming: Any, established: Any) -> bool:
    """True when ``incoming`` is strictly older than ``established``.

    Unparseable values are treated as *not* older so a malformed clock cannot
    freeze a task; the surrounding validation has already refused the payload.
    """
    try:
        return parse_timestamp(incoming) < parse_timestamp(established)
    except TaskLifecycleError:
        return False
