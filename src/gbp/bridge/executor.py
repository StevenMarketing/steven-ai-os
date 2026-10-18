"""Command executor for the two-way GitHub Command Bridge (Issue #49).

The validator (``gbp.bridge.commands``) decides whether an envelope may run at all:
allowlisted actor, trusted permission, idempotency, TTL, no replay, no privileged
operation. This module is the other half: it turns an ACCEPTED envelope into the
explicit state chain ``queued -> claimed -> completed|failed|rejected`` with UTC
timestamps, a GitHub-visible acknowledgement, a GitHub-visible result, and an
append-only local trace.

Phase-1 safety: the only operation that may run is ``status`` (read-only). ``queue_issue``
is accepted **only** as a dry run; a live ``queue_issue`` is rejected with a machine
readable reason, because labelling or enqueueing work is a mutation that needs its own
owner approval. Nothing here executes shell, touches credentials or posts publicly.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

__all__ = ["CommandExecutor", "utc_now", "ExecutionError"]

STATE_QUEUED = "queued"
STATE_CLAIMED = "claimed"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_REJECTED = "rejected"

DEFAULT_TRACE = os.environ.get("STEVEN_COMMAND_TRACE", "/opt/data/ops/bridge/commands.jsonl")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ExecutionError(Exception):
    pass


class CommandExecutor:
    """Executes accepted envelopes and records every state transition."""

    def __init__(
        self,
        *,
        trace_path: Optional[str] = None,
        status_document: Optional[Callable[[], Dict[str, Any]]] = None,
        issue_inspector: Optional[Callable[[int], Dict[str, Any]]] = None,
    ) -> None:
        self.trace_path = trace_path or DEFAULT_TRACE
        self._status_document = status_document
        self._issue_inspector = issue_inspector
        self.notes: list = []

    # -- trace ---------------------------------------------------------------
    def _trace(self, **event: Any) -> None:
        event.setdefault("at", utc_now())
        self.notes.append(event)
        try:
            os.makedirs(os.path.dirname(self.trace_path), exist_ok=True)
            with open(self.trace_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
        except Exception:
            pass  # tracing must never break an execution

    # -- GitHub-visible text -------------------------------------------------
    @staticmethod
    def ack_text(validated: Dict[str, Any], trusted: Any) -> str:
        return (
            "### Command acknowledged — `queued -> claimed`\n\n"
            "| field | value |\n|---|---|\n"
            "| request_id | `%s` |\n| operation | `%s` |\n| actor (verified) | `%s` |\n"
            "| permission | `%s` |\n| idempotency_key | `%s` |\n| dry_run | `%s` |\n"
            "| claimed_at (UTC) | `%s` |\n\n"
            "_Verified from GitHub API metadata, never from the payload._"
        ) % (
            validated.get("request_id"), validated.get("operation"),
            getattr(trusted, "author_login", "?"), getattr(trusted, "permission", "?"),
            validated.get("idempotency_key"), validated.get("dry_run"), utc_now(),
        )

    @staticmethod
    def result_text(state: str, validated: Dict[str, Any], result: Dict[str, Any]) -> str:
        pretty = json.dumps(result, indent=2, sort_keys=True)
        return (
            "### Command result — `claimed -> %s`\n\n"
            "| field | value |\n|---|---|\n| request_id | `%s` |\n| operation | `%s` |\n"
            "| state | `%s` |\n| finished_at (UTC) | `%s` |\n\n```json\n%s\n```"
        ) % (state, validated.get("request_id"), validated.get("operation"), state, utc_now(), pretty)

    # -- operations ----------------------------------------------------------
    def _op_status(self) -> Dict[str, Any]:
        document = self._status_document() if self._status_document else {}
        return {"operation": "status", "read_only": True, "status": document,
                "note": "live worker/cron/budget snapshot, redacted by the status publisher"}

    def _op_queue_issue(self, validated: Dict[str, Any]) -> Dict[str, Any]:
        issue = validated.get("issue")
        if not validated.get("dry_run"):
            raise ExecutionError(
                "phase1_read_only: a live queue_issue is not authorized; send dry_run=true")
        if self._issue_inspector is None or issue is None:
            return {"operation": "queue_issue", "dry_run": True, "issue": issue,
                    "would_queue": False, "mutated": False,
                    "reason": "no inspector configured"}
        inspection = self._issue_inspector(int(issue))
        return {"operation": "queue_issue", "dry_run": True, "issue": int(issue),
                "would_queue": bool(inspection.get("would_queue")),
                "checks": inspection, "mutated": False}

    def execute(
        self,
        validated: Dict[str, Any],
        trusted: Any,
        *,
        dry_run_override: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Run one accepted envelope. Returns the execution record (never raises)."""
        operation = str(validated.get("operation") or "")
        request_id = validated.get("request_id")
        self._trace(event=STATE_QUEUED, request_id=request_id, operation=operation,
                    actor=getattr(trusted, "author_login", ""), dry_run=validated.get("dry_run"))
        self._trace(event=STATE_CLAIMED, request_id=request_id, operation=operation)
        effective = validated
        if dry_run_override is not None:
            effective = dict(validated)
            effective["dry_run"] = bool(dry_run_override)
        try:
            if operation == "status":
                result = self._op_status()
            elif operation == "queue_issue":
                result = self._op_queue_issue(effective)
            else:
                raise ExecutionError("operation %r is not implemented in phase 1" % operation)
        except ExecutionError as exc:
            state, result = STATE_REJECTED, {"reason": str(exc), "mutated": False}
        except Exception as exc:  # bounded, structured failure
            state, result = STATE_FAILED, {"error": type(exc).__name__, "mutated": False}
        else:
            state = STATE_COMPLETED
        self._trace(event=state, request_id=request_id, operation=operation,
                    result_keys=sorted(result.keys()))
        return {"state": state, "request_id": request_id, "operation": operation,
                "result": result, "transitions": [STATE_QUEUED, STATE_CLAIMED, state]}
