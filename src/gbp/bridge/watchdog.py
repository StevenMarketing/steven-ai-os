"""Independent watchdog for the GitHub command bridge (Phase 1, offline).

Why it is *independent*
-----------------------
The scheduler heartbeat is written by a plain OS **cron wrapper**
(``scripts/cron/github_worker_watch.sh`` plus a 1-line ``date >>`` tick file),
not by Hermes, not by the worker and not by the agent's own AI session. So even
when Hermes itself is wedged -- out of budget, killed, or unable to publish --
the tick file keeps advancing and the watchdog can tell the difference between
"nothing to do" and "nothing is running".

The watchdog is pure and deterministic: callers pass ``now`` and a snapshot.
It never executes anything; it returns alerts with stable ``dedupe_key``s so a
scheduler can send each alert once.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import status as st

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"

#: Thresholds (seconds unless stated).
DEFAULT_SCHEDULER_MAX_AGE = 2 * st.EXPECTED_SCHEDULER_CADENCE_SECONDS
DEFAULT_WORKER_MAX_AGE = 2 * st.EXPECTED_WORKER_CADENCE_SECONDS
DEFAULT_STUCK_ISSUE_MAX_AGE = 900
DEFAULT_DUPLICATE_FAILURES = 3
DEFAULT_STUCK_QUEUE_DEPTH = 25


def _alert(code: str, severity: str, message: str, dedupe: str) -> Dict[str, str]:
    return {"code": code, "severity": severity, "message": message[:200],
            "dedupe_key": dedupe}


def evaluate(
    snapshot: Dict[str, Any],
    *,
    now: float,
    scheduler_max_age: float = DEFAULT_SCHEDULER_MAX_AGE,
    worker_max_age: float = DEFAULT_WORKER_MAX_AGE,
    stuck_issue_max_age: float = DEFAULT_STUCK_ISSUE_MAX_AGE,
    duplicate_failures: int = DEFAULT_DUPLICATE_FAILURES,
    stuck_queue_depth: int = DEFAULT_STUCK_QUEUE_DEPTH,
) -> Dict[str, Any]:
    """Return ``{"state", "alerts", "independent_signal", "blind_spots"}``."""
    snapshot = snapshot or {}
    alerts: List[Dict[str, str]] = []

    sched = snapshot.get("scheduler_last_tick")
    wpoll = snapshot.get("worker_last_poll")
    wsuc = snapshot.get("worker_last_success")
    issue = snapshot.get("current_issue")
    issue_since = snapshot.get("current_issue_since")
    queue_depth = int(snapshot.get("queue_depth") or 0)
    failures = int(snapshot.get("consecutive_failures") or 0)
    budget_paused = bool(snapshot.get("budget_paused"))
    scheduler_running = snapshot.get("scheduler_running")

    if sched is None:
        alerts.append(_alert(
            "scheduler_heartbeat_missing", SEVERITY_CRITICAL,
            "no scheduler tick has ever been observed", "scheduler_missing"))
    elif (now - float(sched)) > scheduler_max_age:
        alerts.append(_alert(
            "scheduler_heartbeat_stale", SEVERITY_CRITICAL,
            "scheduler tick is older than the expected cadence", "scheduler_stale"))

    if scheduler_running is False and wpoll is None:
        alerts.append(_alert(
            "scheduler_stopped", SEVERITY_CRITICAL,
            "scheduler reports stopped and the worker never polled",
            "scheduler_stopped"))

    if wpoll is None:
        alerts.append(_alert(
            "worker_heartbeat_missing", SEVERITY_WARNING,
            "worker has never recorded a poll", "worker_missing"))
    elif (now - float(wpoll)) > worker_max_age:
        alerts.append(_alert(
            "worker_heartbeat_stale", SEVERITY_CRITICAL,
            "worker poll is older than the expected cadence", "worker_stale"))

    if wsuc is None and wpoll is not None:
        alerts.append(_alert(
            "worker_never_succeeded", SEVERITY_WARNING,
            "worker has polled but never completed a run", "worker_no_success"))

    if issue is not None and issue_since is not None and \
            (now - float(issue_since)) > stuck_issue_max_age:
        alerts.append(_alert(
            "issue_stuck", SEVERITY_WARNING,
            "current issue has exceeded the stall window",
            f"stuck_issue:{issue}"))

    if queue_depth >= stuck_queue_depth:
        alerts.append(_alert(
            "queue_stuck", SEVERITY_WARNING,
            "queue depth exceeds the stuck threshold", "queue_stuck"))

    if failures >= duplicate_failures:
        alerts.append(_alert(
            "repeated_failures", SEVERITY_WARNING,
            "worker failed repeatedly on the same category",
            f"repeated_failures:{snapshot.get('failure_category')}"))

    if budget_paused:
        alerts.append(_alert(
            "budget_paused", SEVERITY_WARNING,
            "budget guard has paused work", "budget_paused"))

    # A cockpit state for humans, derived from the same evidence as status.py.
    presence = st.classify(
        now=now, scheduler_last_tick=sched, worker_last_poll=wpoll,
        worker_last_success=wsuc, current_issue=issue, queue_depth=queue_depth,
        consecutive_failures=failures, budget_paused=budget_paused,
        fetch_error=snapshot.get("fetch_error"))
    state = presence["state"]
    if state == st.HEALTHY_IDLE and alerts:
        state = st.DEGRADED

    return {
        "state": state,
        "alerts": alerts,
        "independent_signal": {
            "source": "os-cron tick file (not Hermes, not the worker)",
            "path": "ops/bridge/scheduler-tick.txt",
            "detects": "a stopped or wedged scheduler even when Hermes cannot report",
        },
        "blind_spots": [
            "entire VPS down: the tick file also stops, so the only evidence is "
            "GitHub-side staleness of the status document (state unknown/stale)",
            "GitHub unavailable: the watchdog cannot publish its verdict anywhere",
            "clock skew on the host shifts every age computation",
        ],
        "unknown": presence["unknown"],
    }


__all__ = [
    "SEVERITY_CRITICAL", "SEVERITY_WARNING", "DEFAULT_SCHEDULER_MAX_AGE",
    "DEFAULT_WORKER_MAX_AGE", "DEFAULT_STUCK_ISSUE_MAX_AGE",
    "DEFAULT_DUPLICATE_FAILURES", "DEFAULT_STUCK_QUEUE_DEPTH", "evaluate",
]
