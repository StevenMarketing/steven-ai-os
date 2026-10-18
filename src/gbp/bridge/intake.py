"""Structured intake preflight (blocker 4).

The original silent rejection: an issue can carry ``hermes-ready`` yet be unusable
(the #43 case used ``Objective`` / ``Phase 1`` / ``Restrictions`` / ``Success`` and so
matched none of the five mandatory headings). The poller then printed nothing usable,
the monitor saw "no change", and the owner was never told why.

This module is the deterministic, offline half of the fix:
  * ``preflight`` validates exactly ``Goal``, ``Scope``, ``Acceptance Criteria``,
    ``Tests Required``, ``Constraints`` and returns structured rejection reasons.
  * ``render_rejection`` produces the GitHub-visible comment.
  * ``unclaimed_eligible_alerts`` detects an eligible issue that nothing claimed.

Wiring the poller itself (``scripts/issue_poller.py``) and the cron shim are protected
paths: the exact patch and its activation plan live in ``docs/BRIDGE-ACTIVATION.md``
and require owner approval. Nothing here modifies a protected file.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "REQUIRED_HEADINGS",
    "preflight",
    "render_rejection",
    "unclaimed_eligible_alerts",
    "first_seen_update",
    "epoch_of",
    "label_names",
]

#: Exact heading text required by the worker policy, in order.
REQUIRED_HEADINGS = ("Goal", "Scope", "Acceptance Criteria", "Tests Required", "Constraints")

_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)


def _headings(body: str) -> List[str]:
    return [m.group(1).strip().rstrip(":").strip() for m in _HEADING_RE.finditer(body or "")]


def label_names(issue: Dict[str, Any]) -> List[str]:
    return [str(l.get("name") or "") for l in (issue.get("labels") or [])]


def preflight(body: Any, *, patterns: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """Validate an issue body. Returns structured reasons, never a bare False."""
    text = str(body or "")
    found = _headings(text)
    found_lower = {h.lower() for h in found}
    missing = [h for h in REQUIRED_HEADINGS if h.lower() not in found_lower]

    if not text.strip():
        reasons = [{"code": "empty_body", "detail": "issue body is empty"}]
        return {"usable": False, "headings": found, "missing_headings": list(REQUIRED_HEADINGS),
                "tripwire": [], "reasons": reasons}

    reasons: List[Dict[str, str]] = []
    for name in missing:
        reasons.append({"code": "missing_heading", "detail": name,
                        "message": "mandatory heading %r is missing (add `## %s`)" % (name, name)})

    tripwire: List[str] = []
    for pattern in patterns or ():
        name, regex = (pattern if isinstance(pattern, (tuple, list)) else (pattern, pattern))
        try:
            if re.search(regex, text, re.I):
                tripwire.append(str(name))
        except re.error:
            continue
    for name in tripwire:
        reasons.append({"code": "tripwire_pattern", "detail": name,
                        "message": "body matches the refusal pattern %r" % name})

    return {"usable": not reasons, "headings": found, "missing_headings": missing,
            "tripwire": tripwire, "reasons": reasons}


def render_rejection(issue_number: int, verdict: Dict[str, Any]) -> str:
    """GitHub-visible structured rejection comment."""
    lines = [
        "### Worker intake: this issue was skipped (not silently)",
        "",
        "The worker requires five headings exactly: "
        + ", ".join("`%s`" % h for h in REQUIRED_HEADINGS) + ".",
        "",
        "| Check | Result |",
        "|---|---|",
        "| Headings found | %s |" % (", ".join("`%s`" % h for h in verdict.get("headings") or []) or "none"),
        "| Headings missing | %s |" % (", ".join("`%s`" % h for h in verdict.get("missing_headings") or []) or "none"),
        "| Refusal patterns matched | %s |" % (", ".join(verdict.get("tripwire") or []) or "none"),
        "",
        "**Reasons**",
    ]
    for reason in verdict.get("reasons") or []:
        lines.append("- `%s` — %s" % (reason.get("code"), reason.get("message") or reason.get("detail")))
    lines += [
        "",
        "Fix the headings above and keep `hermes-ready`; the next poll (about one minute) "
        "will report `READY #%d` and the worker will claim it." % int(issue_number),
        "",
        "_Structured, deterministic message from `gbp.bridge.intake` — no model was called._",
    ]
    return "\n".join(lines)


def epoch_of(value: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp or an epoch number into epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def ready_since_from_events(
    request: Any,
    token: str,
    repo: str,
    issues: Iterable[Dict[str, Any]],
) -> Dict[str, str]:
    """Trusted ``hermes-ready`` label timestamps, read from GitHub issue events.

    Returns ``{issue_number: created_at}`` for the newest ``labeled`` event whose
    label is ``hermes-ready``. Failures are skipped silently: a missing reference
    simply means the observer falls back to its own first-seen observation.
    """
    out: Dict[str, str] = {}
    for issue in issues or []:
        number = issue.get("number")
        if number is None:
            continue
        try:
            events, _ = request(
                "GET", "/repos/%s/issues/%d/events?per_page=100" % (repo, int(number)), token)
        except Exception:
            continue
        for event in events or []:
            if str(event.get("event") or "") != "labeled":
                continue
            if str((event.get("label") or {}).get("name") or "") != "hermes-ready":
                continue
            stamp = str(event.get("created_at") or "")
            if stamp:
                out[str(number)] = stamp
    return out


def first_seen_update(state: Dict[str, Any], numbers: Iterable[Any], now: float) -> Dict[str, Any]:
    """Record the first time each eligible issue was observed (idempotent)."""
    updated = dict(state or {})
    for number in numbers:
        key = str(number)
        if key not in updated:
            updated[key] = float(now)
    return updated


def unclaimed_eligible_alerts(
    issues: Iterable[Dict[str, Any]],
    *,
    now: float,
    claims: Optional[Dict[str, Any]] = None,
    processed: Optional[Dict[str, Any]] = None,
    ready_since: Optional[Dict[str, Any]] = None,
    first_seen: Optional[Dict[str, Any]] = None,
    stale_minutes: float = 5.0,
) -> List[Dict[str, Any]]:
    """Eligible issues (labelled ``hermes-ready``) that nothing has picked up.

    An alert is only emitted once a *reference time* is known and the elapsed time
    reaches the threshold. The reference is taken, in order, from an existing claim,
    the trusted ``hermes-ready`` label event (``ready_since``) or the first time this
    observer saw the issue (``first_seen``). With no reference at all the issue is
    reported as ``eligible_unclaimed_unobserved`` with ``severity="info"``: an
    observation is recorded for the next run, and nothing is cried wolf about.
    """
    claims = claims or {}
    processed = processed or {}
    ready_since = ready_since or {}
    first_seen = first_seen or {}
    threshold = float(stale_minutes) * 60.0
    alerts: List[Dict[str, Any]] = []
    for issue in issues:
        if str(issue.get("state") or "open") != "open":
            continue
        if "hermes-ready" not in label_names(issue):
            continue
        number = str(issue.get("number"))
        if number in processed:
            continue

        reference: Optional[float] = None
        source: Optional[str] = None
        claim = claims.get(number)
        if isinstance(claim, dict):
            claim_epoch = epoch_of(claim.get("claimed_at"))
            if claim_epoch is not None:
                reference, source = claim_epoch, "claim"
        if reference is None:
            label_epoch = epoch_of(ready_since.get(number))
            if label_epoch is not None:
                reference, source = label_epoch, "label_event"
        if reference is None and number in first_seen:
            seen_epoch = epoch_of(first_seen.get(number))
            if seen_epoch is not None:
                reference, source = seen_epoch, "first_seen"

        if reference is None:
            alerts.append({
                "code": "eligible_unclaimed_unobserved",
                "severity": "info",
                "issue": int(number),
                "waiting_seconds": None,
                "reference": None,
                "message": ("issue carries hermes-ready but has no reference time yet; "
                            "observation recorded for the next check"),
            })
            continue

        waiting = float(now) - reference
        if waiting < threshold:
            continue
        alerts.append({
            "code": "eligible_unclaimed",
            "severity": "warning",
            "issue": int(number),
            "waiting_seconds": waiting,
            "reference": source,
            "message": ("issue carries hermes-ready and nothing picked it up for %.0f minutes"
                        % (waiting / 60.0)),
        })
    return alerts
