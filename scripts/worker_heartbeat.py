#!/usr/bin/env python3
"""Print a sanitized, deterministic worker status snapshot (offline, no LLM).

Reads the existing worker state/audit files with read-only access and prints a
slate of JSON describing liveness, budget and any blocker -- safe to log or to
send over a notification channel because every field is redacted.

Usage
-----
    python3 scripts/worker_heartbeat.py --state ops/github-worker/state.json
    python3 scripts/worker_heartbeat.py --state ... --audit ... --now 1790000000
    python3 scripts/worker_heartbeat.py --state-missing   # exercise safe failure

Exit code is always 0 for a diagnosable state (including a missing/unreadable
state file, which is reported as a blocker); 2 only for a usage error.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from gbp.ops import heartbeat as hb  # noqa: E402

DEFAULT_DAILY_LIMIT = 0.15
DEFAULT_STALL_SECONDS = hb.DEFAULT_STALL_SECONDS


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Optional[str]) -> tuple:
    """Return ``(payload, error)``; never raises on a missing/broken file."""
    if not path:
        return None, "state-missing"
    if not os.path.isfile(path):
        return None, "state-missing"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            if not isinstance(data, dict):
                return None, "state-unreadable"
            return data, None
    except (OSError, ValueError, OverflowError):
        return None, "state-unreadable"


def _last_audit_ts(path: Optional[str]) -> Optional[float]:
    if not path or not os.path.isfile(path):
        return None
    try:
        last = None
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                last = line
        if not last:
            return None
        row = json.loads(last)
        if not isinstance(row, dict):
            return None
        for key in ("ts", "time", "at", "epoch", "timestamp"):
            value = row.get(key)
            if (not isinstance(value, bool) and isinstance(value, (int, float))
                    and math.isfinite(value)):
                return float(value)
        return None
    except (OSError, ValueError, OverflowError):
        return None


def _parse_epoch(value: Any, ttl: int, now: float) -> Optional[float]:
    """Best-effort epoch from a claim record (``expires_at`` or ISO ``claimed_at``)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            cleaned = value.replace("Z", "+00:00")
            parsed = _dt.datetime.fromisoformat(cleaned)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None
    return None


def _claim_age(value: Dict[str, Any], now: float, ttl: int) -> Optional[float]:
    """Epoch the claim was made, if the claim is still live within its TTL."""
    if not isinstance(value, dict):
        return None
    expires = _parse_epoch(value.get("expires_at"), ttl, now)
    if expires is not None and expires > now:
        return expires - ttl  # claim start = expiry - ttl
    started = _parse_epoch(value.get("claimed_at"), ttl, now)
    if started is None:
        return None
    return started if (now - started) <= ttl else None


def _live_claim(claims: Dict[str, Any], now: float,
                ttl: int) -> Optional[float]:
    """Epoch of the oldest live (non-expired) claim, if any."""
    oldest = None
    for value in (claims or {}).values():
        started = _claim_age(value, now, ttl)
        if started is None:
            continue
        oldest = started if oldest is None else min(oldest, started)
    return oldest


def build(args: argparse.Namespace) -> Dict[str, Any]:
    now = float(args.now) if args.now is not None else _dt.datetime.now(
        _dt.timezone.utc).timestamp()
    state, error = _read_json(None if args.state_missing else args.state)

    blocker: Optional[str] = None
    category: Optional[str] = None
    issues: List[Dict[str, Any]] = []
    budget_view: Dict[str, Any] = {}
    last_poll = _last_audit_ts(args.audit)

    if error:
        blocker = error
        category = hb.classify_problem(error)
    else:
        claims = (state or {}).get("claims") or {}
        for number, row in ((state or {}).get("processed") or {}).items():
            row = row or {}
            issues.append({"number": number, "state": "closed",
                           "outcome": row.get("outcome"), "pr": row.get("pr")})
        today = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%d")
        usage = ((state or {}).get("usage") or {}).get(today) or {}
        spend = float(usage.get("cost") or 0.0)
        budget_view = hb.budget_status(spend, args.daily_limit)
        running_since = _live_claim(claims, now, args.claim_ttl)
        if running_since is None and claims:
            blocker = "scheduling: live claim expired (stale claim)"
            category = hb.classify_problem(blocker)
        state_name = hb.heartbeat_state(
            now=now, last_poll=last_poll, running_since=running_since,
            stall_seconds=args.stall_seconds, blocked=category is not None
            and not hb.is_retryable(category or ""))
        if budget_view.get("state") == "exhausted":
            blocker = blocker or "budget: daily cap reached"
            category = category or hb.BUDGET
            state_name = hb.DEGRADED
        snapshot = hb.status_snapshot(
            generated_at=_iso(now) or "", heartbeat=state_name,
            last_poll_at=_iso(last_poll), issues=issues, budget=budget_view,
            blocker=blocker,
            next_step=hb.safe_next_step(state_name, category))
        return snapshot

    state_name = hb.heartbeat_state(now=now, last_poll=last_poll,
                                    stall_seconds=args.stall_seconds,
                                    blocked=True)
    return hb.status_snapshot(
        generated_at=_iso(now) or "", heartbeat=state_name,
        last_poll_at=_iso(last_poll), issues=issues, budget=budget_view,
        blocker=blocker, next_step=hb.safe_next_step(state_name, category))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0] if __doc__ else "worker heartbeat")
    parser.add_argument("--state", help="path to the worker state.json")
    parser.add_argument("--audit", help="path to the worker audit.jsonl")
    parser.add_argument("--state-missing", action="store_true",
                        help="simulate a missing state file (safe failure)")
    parser.add_argument("--now", type=float, default=None,
                        help="override 'now' as a POSIX timestamp (determinism)")
    parser.add_argument("--stall-seconds", type=int,
                        default=DEFAULT_STALL_SECONDS)
    parser.add_argument("--claim-ttl", type=int, default=1800)
    parser.add_argument("--daily-limit", type=float, default=DEFAULT_DAILY_LIMIT)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    print(json.dumps(build(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
