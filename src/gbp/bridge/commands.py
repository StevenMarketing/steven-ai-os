"""Strict, auditable command schema for the GitHub command bridge (Phase 1).

Design rules (all enforced by code, none by convention):

* **Allowlist, never blocklist.** ``operation`` must be one of
  :data:`ALLOWED_OPERATIONS`. Anything else -- including any field that looks
  like a shell command, prompt or URL -- is rejected before it can be stored.
* **Never trust labels.** A label is a hint for the poller; privileged work
  requires an authorized GitHub actor *recorded inside the envelope* and an
  allowlisted origin. Labels alone never authorize anything.
* **Fail closed.** Missing/extra/mistyped fields, unreadable values, a broken
  budget guard or any unexpected exception rejects the command.
* **Idempotent and replay-safe.** A request is identified by
  ``(idempotency_key, fingerprint)``. Repeating the same key with the same
  payload is a no-op; reusing a key or request id with a *different* payload is
  a conflict. Envelopes expire after their TTL.
* **Phase 1 is read-only plus bounded queueing.** ``status`` is free and always
  available; queue operations are allowed but budget-guarded; anything that
  changes budget, secrets, permissions, deploys or publishes is blocked here
  (see :data:`PHASE1_BLOCKED_OPERATIONS`) and needs a separate owner approval.

Standard library only; deterministic (callers pass ``now`` in).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from gbp.security.redact import redact_text

# --------------------------------------------------------------------------
# Allowlists
# --------------------------------------------------------------------------
#: The only operations the bridge accepts in Phase 1.
ALLOWED_OPERATIONS: Tuple[str, ...] = (
    "status",
    "queue_issue",
    "pause_queue",
    "resume_queue",
    "retry_failed_issue",
)

#: Operations that never touch the queue/budget and therefore need no guard.
READ_ONLY_OPERATIONS = frozenset({"status"})

#: Explicitly blocked in Phase 1: budget, secrets, permissions, deploy,
#: destructive actions and public posting. Listed by name so a rejection is
#: self-documenting and a future phase can unlock a name deliberately.
PHASE1_BLOCKED_OPERATIONS = frozenset(
    {
        "set_budget",
        "rotate_secret",
        "set_permissions",
        "deploy",
        "shell",
        "exec",
        "merge_pr",
        "approve_pr",
        "publish_reply",
        "publish_post",
        "edit_cron",
        "delete",
    }
)

#: GitHub logins allowed to originate commands; everything else is unauthorized.
AUTHORIZED_ACTORS = frozenset({"StevenMarketing"})

#: Only this repository may originate commands.
AUTHORIZED_ORIGINS = frozenset({"StevenMarketing/steven-ai-os"})

REQUIRED_FIELDS: Tuple[str, ...] = (
    "request_id",
    "idempotency_key",
    "operation",
    "actor",
    "origin",
    "created_at",
    "ttl_seconds",
)

OPTIONAL_FIELDS: Tuple[str, ...] = ("issue", "dry_run", "note")

_ALL_FIELDS = frozenset(REQUIRED_FIELDS) | frozenset(OPTIONAL_FIELDS)

#: Bounds. A command envelope is small and fixed; anything bigger is rejected.
MAX_TTL_SECONDS = 3600
MIN_TTL_SECONDS = 1
MAX_CLOCK_SKEW_SECONDS = 300
MAX_FIELD_LEN = 128
MAX_NOTE_LEN = 200
MIN_ISSUE_NUMBER = 1
MAX_ISSUE_NUMBER = 1_000_000

#: Rejected inside any string field: shell metacharacters, control bytes,
#: path traversal, redirection and interpolation. Commands are *data*; if a
#: value would be dangerous pasted into a shell it never reaches the queue.
_SHELL_TOKENS = (";", "|", "&", "$", "`", ">", "<", "\n", "\r", "\t", "\\")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_SUBSTRINGS = ("..", "$(", "${", "&&", "||", ">>", "<<")


class CommandError(Exception):
    """Raised for a programming error (never for untrusted input)."""


def _reject(errors: List[str], code: str, detail: str = "") -> None:
    errors.append(code if not detail else f"{code}: {detail}")


def _clean_str(value: Any, *, field: str, errors: List[str],
               max_len: int = MAX_FIELD_LEN) -> Optional[str]:
    """Return a safe bounded string, or ``None`` with an error appended."""
    if not isinstance(value, str):
        _reject(errors, "field_type", field)
        return None
    if len(value) > max_len:
        _reject(errors, "field_too_long", field)
        return None
    if not value.strip():
        _reject(errors, "field_empty", field)
        return None
    if _CONTROL_RE.search(value):
        _reject(errors, "unsafe_character", field)
        return None
    for token in _SHELL_TOKENS:
        if token in value:
            _reject(errors, "unsafe_character", field)
            return None
    for token in _UNSAFE_SUBSTRINGS:
        if token in value:
            _reject(errors, "unsafe_character", field)
            return None
    return value


def fingerprint(payload: Dict[str, Any]) -> str:
    """Stable hash of the *meaningful* envelope fields (order independent)."""
    canonical = {k: payload[k] for k in sorted(_ALL_FIELDS & set(payload))}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class CommandLedger:
    """In-memory (offline) idempotency/replay ledger.

    The production bridge would persist this next to ``ops/github-worker/``;
    Phase 1 keeps it in memory so tests are hermetic and no live state is
    touched.
    """

    def __init__(self) -> None:
        self._by_key: Dict[str, Dict[str, Any]] = {}
        self._by_request: Dict[str, Dict[str, Any]] = {}

    def seen(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        return self._by_key.get(idempotency_key)

    def seen_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self._by_request.get(request_id)

    def record(self, envelope: Dict[str, Any], fp: str, *, now: float) -> None:
        row = {"fingerprint": fp, "first_seen": now,
               "request_id": envelope.get("request_id")}
        self._by_key[envelope["idempotency_key"]] = row
        self._by_request[envelope["request_id"]] = row


def _coerce_issue(value: Any, errors: List[str]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        _reject(errors, "field_type", "issue")
        return None
    if isinstance(value, int):
        issue = value
    elif isinstance(value, str) and value.isdigit():
        issue = int(value)
    else:
        _reject(errors, "field_type", "issue")
        return None
    if not (MIN_ISSUE_NUMBER <= issue <= MAX_ISSUE_NUMBER):
        _reject(errors, "issue_out_of_range", "issue")
        return None
    return issue


def _coerce_number(value: Any, errors: List[str], field: str) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(errors, "field_type", field)
        return None
    return float(value)


def validate_command(
    raw: Any,
    *,
    now: float,
    ledger: Optional[CommandLedger] = None,
    budget_guard: Optional[Callable[[str, Optional[int]], Any]] = None,
) -> Dict[str, Any]:
    """Validate and authorize one envelope. Never raises for untrusted input.

    Returns a bounded, redacted result dict::

        {"accepted": bool, "replay": "fresh|duplicate|conflict",
         "operation": str|None, "issue": int|None, "request_id": str|None,
         "dry_run": bool, "errors": [...], "code": str}

    ``budget_guard(operation, issue)`` must return truthy when the operation may
    proceed (or a ``(allowed, reason)`` pair). A missing or failing guard
    **blocks** every non-read-only operation -- fail closed.
    """
    ledger = ledger if ledger is not None else CommandLedger()
    errors: List[str] = []
    result: Dict[str, Any] = {
        "accepted": False,
        "replay": "fresh",
        "operation": None,
        "issue": None,
        "request_id": None,
        "idempotency_key": None,
        "dry_run": False,
        "errors": errors,
        "code": "rejected",
    }

    try:
        if not isinstance(raw, dict):
            _reject(errors, "envelope_not_object")
            return result

        unknown = sorted(set(raw) - _ALL_FIELDS)
        for field in unknown:
            _reject(errors, "unknown_field", redact_text(str(field)))
        for field in REQUIRED_FIELDS:
            if field not in raw:
                _reject(errors, "field_missing", field)

        operation = raw.get("operation")
        if isinstance(operation, str):
            operation = operation.strip().lower()
        if not isinstance(operation, str) or not operation:
            _reject(errors, "field_type", "operation")
        elif operation in PHASE1_BLOCKED_OPERATIONS:
            _reject(errors, "operation_blocked_phase1", operation)
        elif operation not in ALLOWED_OPERATIONS:
            _reject(errors, "operation_not_allowlisted", redact_text(operation))
        result["operation"] = operation if isinstance(operation, str) else None

        request_id = _clean_str(raw.get("request_id"), field="request_id",
                                errors=errors)
        idem_key = _clean_str(raw.get("idempotency_key"),
                              field="idempotency_key", errors=errors)
        actor = _clean_str(raw.get("actor"), field="actor", errors=errors)
        origin = _clean_str(raw.get("origin"), field="origin", errors=errors)

        if actor is not None and actor not in AUTHORIZED_ACTORS:
            _reject(errors, "actor_unauthorized", redact_text(actor))
        if origin is not None and origin not in AUTHORIZED_ORIGINS:
            _reject(errors, "origin_unauthorized", redact_text(origin))

        created_at = _coerce_number(raw.get("created_at"), errors, "created_at")
        ttl = _coerce_number(raw.get("ttl_seconds"), errors, "ttl_seconds")
        if ttl is not None:
            if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
                _reject(errors, "ttl_out_of_range", "ttl_seconds")
            elif ttl != int(ttl):
                _reject(errors, "ttl_out_of_range", "ttl_seconds")
        if created_at is not None:
            if created_at > now + MAX_CLOCK_SKEW_SECONDS:
                _reject(errors, "created_in_future", "created_at")
            elif ttl is not None and created_at + ttl <= now:
                _reject(errors, "command_expired", "ttl_seconds")

        issue = _coerce_issue(raw.get("issue"), errors)
        result["issue"] = issue
        if operation in ("queue_issue", "retry_failed_issue") and issue is None:
            _reject(errors, "issue_required", "issue")
        if operation not in READ_ONLY_OPERATIONS and raw.get("dry_run") is not True:
            # Phase 1: mutating operations are accepted only as dry runs.
            _reject(errors, "mutation_requires_dry_run", "dry_run")

        dry_run = raw.get("dry_run")
        if dry_run is not None and not isinstance(dry_run, bool):
            _reject(errors, "field_type", "dry_run")
        result["dry_run"] = dry_run is True

        note = raw.get("note")
        if note is not None:
            if _clean_str(note, field="note", errors=errors,
                          max_len=MAX_NOTE_LEN) is None:
                pass

        result["request_id"] = request_id
        result["idempotency_key"] = idem_key

        if request_id is not None and idem_key is not None:
            fp = fingerprint(raw)
            prior = ledger.seen(idem_key)
            if prior is None:
                prior_req = ledger.seen_request(request_id)
                if prior_req is not None:
                    if prior_req["fingerprint"] == fp:
                        result["replay"] = "duplicate"
                        prior = prior_req
                    else:
                        _reject(errors, "request_id_reused", "request_id")
                        result["replay"] = "conflict"
                        return result
            if prior is not None:
                if prior["fingerprint"] == fp:
                    result["replay"] = "duplicate"
                    result["code"] = "duplicate"
                    result["accepted"] = False
                    return result
                _reject(errors, "idempotency_conflict", "idempotency_key")
                result["replay"] = "conflict"
                return result

            if errors:
                return result

            if operation not in READ_ONLY_OPERATIONS:
                if budget_guard is None:
                    _reject(errors, "budget_guard_missing")
                    return result
                try:
                    verdict = budget_guard(str(operation), issue)
                except Exception:  # noqa: BLE001 - fail closed, no detail leak
                    _reject(errors, "budget_guard_error")
                    return result
                allowed = verdict
                reason = ""
                if isinstance(verdict, (tuple, list)) and len(verdict) == 2:
                    allowed, reason = bool(verdict[0]), str(verdict[1] or "")
                if not allowed:
                    _reject(errors, "budget_guard_blocked",
                            redact_text(reason) if reason else "")
                    return result

            ledger.record(raw, fp, now=now)

        if errors:
            return result

        result["accepted"] = True
        result["code"] = ("dry_run" if result["dry_run"]
                          else ("duplicate" if result["replay"] == "duplicate"
                                else "accepted"))
        return result
    except Exception:  # noqa: BLE001 - fail closed on ANY unexpected fault
        errors.append("validation_error")
        result["accepted"] = False
        result["code"] = "rejected"
        return result


def parse_command_body(text: Any) -> Dict[str, Any]:
    """Parse a fenced ``command`` block from an issue/comment body (data only).

    The bridge deliberately accepts one narrow format: a ```command fence
    containing a JSON object. Anything else yields ``{"errors": [...]}``. The
    surrounding prose is never interpreted as instructions.
    """
    out: Dict[str, Any] = {"errors": []}
    if not isinstance(text, str) or len(text) > 20000:
        out["errors"].append("body_unusable")
        return out
    match = re.search(r"```command\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        out["errors"].append("no_command_block")
        return out
    try:
        payload = json.loads(match.group(1))
    except Exception:  # noqa: BLE001
        out["errors"].append("command_block_not_json")
        return out
    if not isinstance(payload, dict):
        out["errors"].append("command_block_not_object")
        return out
    out.update(payload)
    return out


def describe_schema() -> Dict[str, Any]:
    """Machine-readable schema description, safe to publish in the docs."""
    return {
        "required": list(REQUIRED_FIELDS),
        "optional": list(OPTIONAL_FIELDS),
        "operations": list(ALLOWED_OPERATIONS),
        "blocked_phase1": sorted(PHASE1_BLOCKED_OPERATIONS),
        "authorized_actors": sorted(AUTHORIZED_ACTORS),
        "authorized_origins": sorted(AUTHORIZED_ORIGINS),
        "bounds": {
            "ttl_seconds": [MIN_TTL_SECONDS, MAX_TTL_SECONDS],
            "clock_skew_seconds": MAX_CLOCK_SKEW_SECONDS,
            "issue": [MIN_ISSUE_NUMBER, MAX_ISSUE_NUMBER],
            "max_field_len": MAX_FIELD_LEN,
        },
        "phase1": "read-only + dry-run queue operations; nothing executes",
    }


__all__ = [
    "ALLOWED_OPERATIONS",
    "READ_ONLY_OPERATIONS",
    "PHASE1_BLOCKED_OPERATIONS",
    "AUTHORIZED_ACTORS",
    "AUTHORIZED_ORIGINS",
    "REQUIRED_FIELDS",
    "OPTIONAL_FIELDS",
    "MAX_TTL_SECONDS",
    "CommandError",
    "CommandLedger",
    "fingerprint",
    "validate_command",
    "parse_command_body",
    "describe_schema",
]
