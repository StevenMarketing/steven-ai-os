"""Deterministic worker heartbeat and sanitized progress snapshot.

Everything here is pure and offline: pass ``now`` (a POSIX timestamp) in and the
same input always gives the same output -- no clock reads, no network, no LLM.
The goal is to tell, from the outside, whether the unattended GitHub issue
worker is *healthy and idle* or *stalled*, what (if anything) is blocking it,
and one safe next step -- without leaking a credential into the report.

Design rules:
* classify a failure into exactly one of a fixed taxonomy;
* only ever *propose* bounded retries, and only for genuinely transient faults;
* redact anything credential-shaped before it reaches a report, log or message;
* de-duplicate progress events by a stable identity so a report is sent once.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Heartbeat states
# --------------------------------------------------------------------------
IDLE = "idle"          # polled recently, nothing claimed -- healthy
RUNNING = "running"    # an issue is claimed and progressing
STALLED = "stalled"    # beyond the stall window with no progress
DEGRADED = "degraded"  # a non-transient problem is blocking progress

#: default seconds without progress before "idle/running" becomes "stalled"
DEFAULT_STALL_SECONDS = 900

# --------------------------------------------------------------------------
# Problem taxonomy
# --------------------------------------------------------------------------
BUDGET = "budget"
PROVIDER = "provider"
POLICY = "policy"
RESTRICTED_FILE = "restricted_file"
SCHEDULING = "scheduling"
TRANSIENT = "transient"
UNKNOWN = "unknown"

CATEGORIES: Tuple[str, ...] = (
    BUDGET, PROVIDER, POLICY, RESTRICTED_FILE, SCHEDULING, TRANSIENT, UNKNOWN,
)

#: only these are ever retried automatically, and only up to the cap below
RETRYABLE = frozenset({TRANSIENT})

#: maximum *automatic* retries for a transient fault (owner-set bounded policy)
MAX_TRANSIENT_RETRIES = 2

# Ordered keyword probes. First match wins, so put the most specific first.
_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (BUDGET, ("budget", "cap reached", "quota", "hold", "over budget",
              "spend limit")),
    (RESTRICTED_FILE, ("protected path", "restricted file", "protected file",
                       "forbidden path", "not permitted to modify")),
    (SCHEDULING, ("scheduler", "cron", "crontab", "next tick", "schedule",
                  "missed run", "systemd timer")),
    (POLICY, ("approval", "denied", "refused", "policy", "not authorised",
              "not authorized", "permission", "kill switch", "paused")),
    (PROVIDER, ("provider", "openrouter", "api key", "401", "403",
                "unauthorized", "invalid model", "no such model")),
    (TRANSIENT, ("timeout", "timed out", "transient", "temporar", "try again",
                 "connection reset", "econnreset", "temporarily unavailable",
                 "502", "503", "504", "rate limited", "429")),
)


def classify_problem(detail: Optional[str]) -> str:
    """Map a free-text failure detail onto one fixed category.

    Unknown text is ``UNKNOWN`` (never silently treated as retryable).
    """
    text = (detail or "").lower()
    for category, needles in _KEYWORDS:
        if any(needle in text for needle in needles):
            return category
    return UNKNOWN


def is_retryable(category: str) -> bool:
    """True only for categories the owner authorised for bounded retries."""
    return category in RETRYABLE


def retry_decision(category: str, attempts: int,
                   max_retries: int = MAX_TRANSIENT_RETRIES) -> Dict[str, Any]:
    """Bounded, deterministic retry policy.

    Returns ``{"retry": bool, "attempt": int, "reason": str}``. A retry is only
    ever proposed for a transient category and only while ``attempts`` is below
    the cap -- nothing else is retried automatically.
    """
    attempts = max(0, int(attempts))
    if not is_retryable(category):
        return {"retry": False, "attempt": attempts,
                "reason": f"not-retryable:{category}"}
    if attempts >= max_retries:
        return {"retry": False, "attempt": attempts,
                "reason": "retry-cap-reached"}
    return {"retry": True, "attempt": attempts + 1,
            "reason": f"transient:attempt-{attempts + 1}-of-{max_retries}"}


# --------------------------------------------------------------------------
# Heartbeat
# --------------------------------------------------------------------------
def heartbeat_state(*, now: float, last_poll: Optional[float] = None,
                    running_since: Optional[float] = None,
                    stall_seconds: int = DEFAULT_STALL_SECONDS,
                    blocked: bool = False) -> str:
    """Classify the worker's liveness for the given observation time.

    * ``blocked`` -> ``DEGRADED`` (a non-transient problem needs a human);
    * a live claim older than ``stall_seconds`` -> ``STALLED``;
    * a fresh live claim -> ``RUNNING``;
    * a poll within ``stall_seconds`` -> ``IDLE`` (healthy, nothing to do);
    * no poll recorded, or a poll older than ``stall_seconds`` -> ``STALLED``.
    """
    if blocked:
        return DEGRADED
    # A readable ledger or fresh claim is not proof the worker still polls.
    # Reject malformed/future evidence rather than manufacture freshness.
    def valid(value):
        try:
            return (not isinstance(value, bool) and isinstance(value, (int, float))
                    and math.isfinite(value))
        except OverflowError:
            return False
    if (not valid(now) or not valid(stall_seconds) or stall_seconds <= 0
            or not valid(last_poll) or last_poll > now
            or now - last_poll > stall_seconds):
        return STALLED
    if running_since is not None:
        if not valid(running_since) or running_since > now:
            return STALLED
        return STALLED if (now - running_since) > stall_seconds else RUNNING
    if last_poll is None:
        return STALLED
    return STALLED if (now - last_poll) > stall_seconds else IDLE


def heartbeat_age(now: float, last_poll: Optional[float]) -> Optional[float]:
    """Seconds since the last poll, or ``None`` when none was ever recorded."""
    if last_poll is None:
        return None
    return max(0.0, now - last_poll)


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------
def budget_status(spend: float, limit: float, *, reset: bool = False,
                  warn_ratio: float = 0.8) -> Dict[str, Any]:
    """Sanitized budget view; ``reset`` models the daily roll-over.

    When ``reset`` is true the day has turned over, so effective spend is 0 and
    the previous day's spend is reported separately for context.
    """
    limit = float(limit)
    prev_spend = float(spend)
    effective = 0.0 if reset else prev_spend
    ratio = (effective / limit) if limit > 0 else 0.0
    if ratio >= 1.0:
        state = "exhausted"
    elif ratio >= warn_ratio:
        state = "warning"
    else:
        state = "ok"
    return {
        "spend_usd": round(effective, 6),
        "limit_usd": round(limit, 6),
        "remaining_usd": round(max(0.0, limit - effective), 6),
        "ratio": round(ratio, 4),
        "state": state,
        "reset": bool(reset),
        "previous_spend_usd": round(prev_spend, 6) if reset else None,
    }


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------
#: high-confidence credential shapes -- replaced before anything is emitted
_SECRET_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(pattern) for pattern in (
        r"\bsk-or-v1-[A-Za-z0-9]{16,}\b",
        r"\bsk-[A-Za-z0-9]{20,}\b",
        r"\bAIza[0-9A-Za-z\-_]{20,}\b",
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{15,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{15,}\b",
        r"\bAKIA[0-9A-Z]{12,}\b",
        r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b",
        r"\b\d{9,10}:[A-Za-z0-9_\-]{30,}\b",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )
)
#: key=value / "key": "value" pairs whose value is always a secret
_KV_PATTERN = re.compile(
    r"(?i)\b(token|api[_-]?key|secret|password|passwd|credential|bearer|authorization)"
    r"(\s*[:=]\s*|\s+bearer\s+)([^\s,;\"'}]+)")


def redact(text: Any) -> Any:
    """Return ``text`` with credential-shaped substrings masked.

    Non-strings pass through unchanged except that dicts/lists are walked so a
    whole snapshot can be redacted in one call.
    """
    if isinstance(text, str):
        cleaned = _KV_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
        for pattern in _SECRET_PATTERNS:
            cleaned = pattern.sub("***redacted***", cleaned)
        return cleaned
    if isinstance(text, dict):
        return {redact(k): redact(v) for k, v in text.items()}
    if isinstance(text, (list, tuple)):
        return [redact(item) for item in text]
    return text


# --------------------------------------------------------------------------
# Event deduplication
# --------------------------------------------------------------------------
def event_identity(event: str, *, issue: Any = None, detail: Optional[str] = None) -> str:
    """Stable identity for a progress event.

    The same logical event always hashes to the same id across runs, so a report
    is emitted once and never repeated on the next poll.
    """
    material = "|".join([
        str(event or "").strip().lower(),
        str(issue if issue is not None else "").strip(),
        " ".join((detail or "").split()).lower(),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class ProgressDeduper:
    """Tracks which event identities have already been emitted.

    Deterministic and side-effect free in memory; :meth:`save`/:meth:`load`
    persist the seen-set to a small JSON file so de-duplication survives across
    runs. Delivery must never interrupt the worker, so the file helpers swallow
    I/O errors and fail open (treat the event as new) rather than raise.
    """

    def __init__(self, seen: Optional[Iterable[str]] = None) -> None:
        self._seen = set(seen or ())

    def __contains__(self, identity: str) -> bool:
        return identity in self._seen

    def should_emit(self, identity: str) -> bool:
        """True the first time an identity is seen, False afterwards."""
        if identity in self._seen:
            return False
        self._seen.add(identity)
        return True

    def seen(self) -> List[str]:
        return sorted(self._seen)

    def to_json(self) -> str:
        return json.dumps({"version": 1, "seen": self.seen()}, sort_keys=True)

    @classmethod
    def load(cls, path: str) -> "ProgressDeduper":
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return cls(payload.get("seen") or ())
        except (OSError, ValueError):
            return cls()

    def save(self, path: str) -> bool:
        """Best-effort persist; returns False instead of raising on failure."""
        try:
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(self.to_json())
            import os
            os.replace(tmp, path)
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------
# Status snapshot
# --------------------------------------------------------------------------
def _issue_row(issue: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "number": issue.get("number"),
        "state": issue.get("state"),
        "outcome": issue.get("outcome"),
        "pr": issue.get("pr"),
    }


def status_snapshot(*, generated_at: str, heartbeat: str,
                    last_poll_at: Optional[str] = None,
                    issues: Optional[Sequence[Dict[str, Any]]] = None,
                    budget: Optional[Dict[str, Any]] = None,
                    blocker: Optional[str] = None,
                    next_step: Optional[str] = None) -> Dict[str, Any]:
    """A sanitized, machine-readable progress summary.

    Every free-text field is redacted, so the result is safe to log, store or
    send over a notification channel.
    """
    snapshot = {
        "generated_at": generated_at,
        "heartbeat": heartbeat,
        "last_poll_at": last_poll_at,
        "issues": [_issue_row(row) for row in (issues or ())],
        "budget": budget or {},
        "blocker": blocker,
        "next_step": next_step,
    }
    return redact(snapshot)


def safe_next_step(heartbeat: str, category: Optional[str] = None) -> str:
    """The single safe action to take, given the heartbeat classification."""
    if heartbeat == IDLE:
        return "none: healthy idle poll"
    if heartbeat == RUNNING:
        return "wait: issue in progress"
    if heartbeat == STALLED:
        return "investigate: check the runner and re-run the collector"
    decision = retry_decision(category or UNKNOWN, 0)
    if heartbeat == DEGRADED:
        if decision["retry"]:
            return f"bounded retry: {decision['reason']}"
        return "human review required: non-transient blocker"
    return "human review required"
