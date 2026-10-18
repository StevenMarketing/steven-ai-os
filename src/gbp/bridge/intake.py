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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "REQUIRED_HEADINGS",
    "preflight",
    "render_rejection",
    "unclaimed_eligible_alerts",
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


def unclaimed_eligible_alerts(
    issues: Iterable[Dict[str, Any]],
    *,
    now: float,
    claims: Optional[Dict[str, Any]] = None,
    processed: Optional[Dict[str, Any]] = None,
    stale_minutes: float = 5.0,
) -> List[Dict[str, Any]]:
    """Eligible issues (labelled hermes-ready) that nothing has claimed or finished."""
    claims = claims or {}
    processed = processed or {}
    alerts: List[Dict[str, Any]] = []
    for issue in issues:
        if str(issue.get("state") or "open") != "open":
            continue
        if "hermes-ready" not in label_names(issue):
            continue
        number = str(issue.get("number"))
        if number in processed:
            continue
        claim = claims.get(number)
        if claim is None:
            waiting = None
        else:
            claimed_at = claim.get("claimed_at") if isinstance(claim, dict) else None
            waiting = None
            if isinstance(claimed_at, str) and claimed_at:
                text = claimed_at[:-1] + "+00:00" if claimed_at.endswith("Z") else claimed_at
                try:
                    from datetime import datetime, timezone

                    stamp = datetime.fromisoformat(text)
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    waiting = float(now) - stamp.timestamp()
                except ValueError:
                    waiting = None
        if waiting is not None and waiting <= float(stale_minutes) * 60:
            continue
        alerts.append({
            "code": "eligible_unclaimed",
            "severity": "warning",
            "issue": int(number),
            "waiting_seconds": waiting,
            "message": "issue carries hermes-ready but nothing claimed it for over %.0f minutes"
                       % float(stale_minutes),
        })
    return alerts
