"""External, VPS-independent failure detection (blocker 2).

The local cron tick and the local watchdog live on the same VPS: when the VPS or the
cron stops, neither can publish anything. This module is the *evaluator* half of a
GitHub-side external watchdog: it is a pure function over a status document and an
*independently supplied* current time, so it can run anywhere (a GitHub Actions
scheduled job, a laptop, another host) without the VPS being alive.

Deliberate design points:
  * The age is recomputed here from ``generated_at`` and ``now``. A published
    ``freshness.document_age_seconds`` is ignored: it is a value frozen at
    generation time and cannot prove freshness to an external reader.
  * ``independence`` records that the evaluator is not the VPS's own cron, so we
    never claim the local cron tick is an independent monitor of a stopped cron.
  * The evaluator never talks to GitHub and never raises: it returns alerts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "INDEPENDENCE",
    "parse_timestamp",
    "document_age_seconds",
    "evaluate_external",
]

#: A status older than this is stale. The cron tick is every minute; 15 minutes of
#: silence already means several consecutive ticks were lost.
DEFAULT_MAX_AGE_SECONDS = 900

#: Machine-readable marker put into the alert payload so a reader can see that this
#: evaluation did not depend on the VPS.
INDEPENDENCE = {
    "evaluator": "external",
    "uses_local_cron": False,
    "vps_required": False,
}


def parse_timestamp(value: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp (``Z`` or offset) into epoch seconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def document_age_seconds(document: Optional[Dict[str, Any]], now: float) -> Optional[float]:
    """Age of ``document`` at the independently supplied ``now`` (epoch seconds)."""
    if not isinstance(document, dict):
        return None
    stamp = parse_timestamp(document.get("generated_at"))
    if stamp is None:
        return None
    return float(now) - stamp


def _alert(code: str, severity: str, message: str, **extra: Any) -> Dict[str, Any]:
    entry = {"code": code, "severity": severity, "message": message,
             "independence": dict(INDEPENDENCE)}
    entry.update(extra)
    return entry


def evaluate_external(
    document: Optional[Dict[str, Any]],
    *,
    now: float,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    required_fields: Tuple[str, ...] = ("generated_at", "worker", "cron", "budget"),
    expected_repository: Optional[str] = None,
    clock_skew_seconds: float = 120.0,
) -> Dict[str, Any]:
    """Evaluate a status document from outside the VPS.

    Returns ``{"ok": bool, "alerts": [...], "age_seconds": float|None, "independence": {...}}``.
    An empty ``alerts`` list with ``ok=True`` means "status is present, readable and fresh".
    """
    alerts: List[Dict[str, Any]] = []

    if document is None:
        alerts.append(_alert("status_missing", "critical",
                             "no status document was published at all"))
        return {"ok": False, "alerts": alerts, "age_seconds": None, "independence": dict(INDEPENDENCE)}
    if not isinstance(document, dict):
        alerts.append(_alert("status_unreadable", "critical", "status document is not an object"))
        return {"ok": False, "alerts": alerts, "age_seconds": None, "independence": dict(INDEPENDENCE)}

    for name in required_fields:
        if name not in document:
            alerts.append(_alert("status_unreadable", "critical",
                                 f"status document is missing the field {name!r}", field=name))

    if expected_repository and str(document.get("repository") or "") not in ("", expected_repository):
        alerts.append(_alert("status_wrong_repository", "warning",
                             "status document names a different repository"))

    age = document_age_seconds(document, now)
    if age is None:
        alerts.append(_alert("status_unreadable", "critical",
                             "status document has no parsable generated_at timestamp"))
    elif age < -float(clock_skew_seconds):
        alerts.append(_alert("clock_skew", "warning",
                             "status timestamp is in the future beyond the skew allowance",
                             age_seconds=age))
    elif age > float(max_age_seconds):
        alerts.append(_alert("status_stale", "critical",
                             "status is older than the allowed age",
                             age_seconds=age, max_age_seconds=float(max_age_seconds)))

    freshness = document.get("freshness")
    if isinstance(freshness, dict) and "document_age_seconds" in freshness:
        alerts.append(_alert("frozen_age_ignored", "warning",
                             "published document_age_seconds was ignored; age recomputed locally"))

    return {
        "ok": not alerts,
        "alerts": alerts,
        "age_seconds": age,
        "independence": dict(INDEPENDENCE),
    }
