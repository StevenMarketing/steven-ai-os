"""Bounded, redacted, GitHub-visible bridge status document (Phase 1).

The document is the *only* thing ChatGPT's hourly GitHub check reads. It is:

* **machine-readable** -- one JSON object, versioned schema;
* **bounded** -- fixed key set, capped list lengths, capped string lengths;
* **redacted** -- every string routes through :func:`gbp.security.redact.redact_text`;
* **honest** -- observed evidence is separated from ``unknown``; an unchanged
  issue/PR feed is never used to infer that the VPS is alive.

States: ``healthy_idle``, ``healthy_busy``, ``degraded``,
``stopped_or_unreachable``, ``unknown``.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Sequence

from gbp.security.redact import redact_text

SCHEMA_VERSION = 1

HEALTHY_IDLE = "healthy_idle"
HEALTHY_BUSY = "healthy_busy"
DEGRADED = "degraded"
STOPPED_OR_UNREACHABLE = "stopped_or_unreachable"
UNKNOWN = "unknown"

ALL_STATES = (HEALTHY_IDLE, HEALTHY_BUSY, DEGRADED, STOPPED_OR_UNREACHABLE,
              UNKNOWN)

#: Where the ChatGPT connector should look (created by the publisher cron once
#: the owner approves activation -- nothing writes it in Phase 1).
STATUS_PATH = "ops/bridge/status.json"

#: Cadence the owner can expect; the document states it explicitly.
EXPECTED_SCHEDULER_CADENCE_SECONDS = 600      # watcher cron tick
EXPECTED_WORKER_CADENCE_SECONDS = 300         # worker poll
EXPECTED_STATUS_CADENCE_SECONDS = 3600        # ChatGPT hourly check

MAX_NOTES = 10
MAX_NOTE_LEN = 200
MAX_EVIDENCE = 12


def iso(epoch: Optional[float]) -> Optional[str]:
    """UTC ISO-8601 seconds, or ``None``."""
    if epoch is None:
        return None
    return _dt.datetime.fromtimestamp(float(epoch), _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _note(text: Any) -> str:
    return redact_text(str(text))[:MAX_NOTE_LEN]


def read_error_state(fetch_error: Optional[str]) -> str:
    """Map a GitHub read failure to a state. A failed read is NEVER 'no work'."""
    if not fetch_error:
        return ""
    low = str(fetch_error).lower()
    if "rate" in low:
        return "rate_limited"
    if "timeout" in low or "unreachable" in low or "connection" in low:
        return STOPPED_OR_UNREACHABLE
    return "read_failed"


def classify(
    *,
    now: float,
    scheduler_last_tick: Optional[float] = None,
    worker_last_poll: Optional[float] = None,
    worker_last_success: Optional[float] = None,
    current_issue: Optional[Any] = None,
    queue_depth: int = 0,
    consecutive_failures: int = 0,
    budget_paused: bool = False,
    fetch_error: Optional[str] = None,
    scheduler_max_age: float = 2 * EXPECTED_SCHEDULER_CADENCE_SECONDS,
    worker_max_age: float = 2 * EXPECTED_WORKER_CADENCE_SECONDS,
) -> Dict[str, Any]:
    """Deterministic presence classification, with evidence and blind spots."""
    observed: Dict[str, Any] = {}
    unknown: List[str] = []
    notes: List[str] = []

    scheduler_age = None if scheduler_last_tick is None else max(
        0.0, now - float(scheduler_last_tick))
    worker_age = None if worker_last_poll is None else max(
        0.0, now - float(worker_last_poll))

    observed["scheduler_last_tick_age_seconds"] = scheduler_age
    observed["worker_last_poll_age_seconds"] = worker_age
    observed["worker_last_success_age_seconds"] = (
        None if worker_last_success is None else max(0.0, now - float(worker_last_success)))
    observed["queue_depth"] = int(queue_depth)
    observed["consecutive_failures"] = int(consecutive_failures)
    observed["budget_paused"] = bool(budget_paused)
    observed["current_issue"] = (None if current_issue is None
                                else redact_text(str(current_issue)))
    observed["db_or_vps_health"] = "not_observed"

    if scheduler_last_tick is None:
        unknown.append("scheduler_last_tick")
    if worker_last_poll is None:
        unknown.append("worker_last_poll")
    unknown.append("vps_host_health")           # never inferred from GitHub
    unknown.append("provider_budget_inside_hermes")  # owned by budget guard

    read_state = read_error_state(fetch_error)
    if read_state:
        notes.append(_note(f"github_read={read_state}"))

    scheduler_stale = (True if scheduler_last_tick is None
                       else (now - float(scheduler_last_tick)) > scheduler_max_age)
    worker_stale = (True if worker_last_poll is None
                    else (now - float(worker_last_poll)) > worker_max_age)

    if read_state in (STOPPED_OR_UNREACHABLE,) and scheduler_last_tick is None:
        state = STOPPED_OR_UNREACHABLE
        notes.append(_note("github unreachable and scheduler never observed"))
    elif scheduler_stale and worker_stale:
        state = STOPPED_OR_UNREACHABLE
        notes.append(_note("scheduler and worker both beyond cadence"))
    elif scheduler_stale:
        state = DEGRADED
        notes.append(_note("scheduler heartbeat stale"))
    elif budget_paused:
        state = DEGRADED
        notes.append(_note("budget guard pause active"))
    elif worker_stale:
        state = DEGRADED
        notes.append(_note("worker heartbeat stale while scheduler alive"))
    elif consecutive_failures >= 3:
        state = DEGRADED
        notes.append(_note("repeated worker failures"))
    elif current_issue is not None:
        state = HEALTHY_BUSY
    elif not scheduler_stale and not worker_stale:
        state = HEALTHY_IDLE
    else:
        state = UNKNOWN
        notes.append(_note("insufficient evidence"))

    return {"state": state, "observed": observed, "unknown": unknown,
            "notes": notes[:MAX_NOTES], "read_state": read_state}


def build_status(
    *,
    now: float,
    presence: Dict[str, Any],
    version: str = "0.1.0",
    generated_at: Optional[float] = None,
    extra_evidence: Optional[Sequence[str]] = None,
    publish_ok: Optional[bool] = None,
    status_age_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Assemble the bounded, redacted, GitHub-visible status document."""
    generated_at = now if generated_at is None else generated_at
    evidence = [redact_text(str(e))[:MAX_NOTE_LEN]
                for e in (extra_evidence or [])][:MAX_EVIDENCE]
    unknown = [redact_text(str(u))[:MAX_NOTE_LEN]
               for u in presence.get("unknown", [])][:MAX_EVIDENCE]
    notes = [_note(n) for n in presence.get("notes", [])][:MAX_NOTES]

    freshness: Dict[str, Any] = {
        "document_age_seconds": status_age_seconds,
        "stale": (status_age_seconds is not None
                  and status_age_seconds > EXPECTED_STATUS_CADENCE_SECONDS),
        "expected_status_cadence_seconds": EXPECTED_STATUS_CADENCE_SECONDS,
    }
    if publish_ok is False:
        notes.append(_note("previous status upload failed"))
        if presence.get("state") in (HEALTHY_IDLE, HEALTHY_BUSY):
            presence = dict(presence, state=UNKNOWN)

    doc = {
        "schema_version": SCHEMA_VERSION,
        "version": redact_text(str(version))[:32],
        "generated_at": iso(generated_at),
        "state": presence.get("state", UNKNOWN),
        "expected_cadence_seconds": {
            "scheduler": EXPECTED_SCHEDULER_CADENCE_SECONDS,
            "worker": EXPECTED_WORKER_CADENCE_SECONDS,
            "status_document": EXPECTED_STATUS_CADENCE_SECONDS,
        },
        "freshness": freshness,
        "observed": presence.get("observed", {}),
        "evidence": evidence,
        "unknown": unknown,
        "notes": notes[:MAX_NOTES],
        "publish_ok": publish_ok,
        "limitations": [
            "cannot observe VPS health directly; a silent VPS looks like a stale document",
            "if GitHub itself is unavailable no status can be published",
            "budget internals stay inside the budget guard and are reported only as a pause flag",
        ],
    }
    return doc


def is_readable_by_connector(doc: Any) -> bool:
    """Cheap structural check the tests (and the owner) can rely on."""
    if not isinstance(doc, dict):
        return False
    if doc.get("state") not in ALL_STATES:
        return False
    for key in ("generated_at", "observed", "unknown", "freshness"):
        if key not in doc:
            return False
    return True


__all__ = [
    "SCHEMA_VERSION", "STATUS_PATH", "ALL_STATES", "HEALTHY_IDLE",
    "HEALTHY_BUSY", "DEGRADED", "STOPPED_OR_UNREACHABLE", "UNKNOWN",
    "EXPECTED_SCHEDULER_CADENCE_SECONDS", "EXPECTED_WORKER_CADENCE_SECONDS",
    "EXPECTED_STATUS_CADENCE_SECONDS", "iso", "classify", "build_status",
    "is_readable_by_connector", "read_error_state",
]
