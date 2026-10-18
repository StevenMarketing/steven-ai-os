#!/usr/bin/env python3
"""External watchdog check (blocker 2) — runs anywhere, including GitHub Actions.

Reads the newest status comment from the machine-readable status issue and evaluates
it against **its own clock**. Exit code 1 when an alert fires, so the independent
runner (a GitHub Actions scheduled job) turns red without the VPS being involved.

Usage:
    PYTHONPATH=src python3 scripts/bridge_watchdog_check.py --issue-number 77 --fail-on-alert
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gbp.bridge.external_watchdog import evaluate_external  # noqa: E402
from gbp.bridge.intake import (  # noqa: E402
    ready_since_from_events,
    unclaimed_eligible_alerts,
)

OBSERVATIONS_PATH = os.environ.get("STEVEN_INTAKE_OBSERVATIONS",
                                  "/opt/data/ops/bridge/intake-observations.json")

DEFAULT_REPO = "StevenMarketing/steven-ai-os"


def newest_status_document(
    request: Callable[..., Any],
    token: Any,
    repo: str,
    issue_number: int,
) -> Optional[Dict[str, Any]]:
    """Return the parsed JSON from the most recent status comment, or None."""
    comments, _ = request(
        "GET", "/repos/%s/issues/%d/comments?per_page=100" % (repo, int(issue_number)), token)
    for comment in reversed(comments or []):
        body = str(comment.get("body") or "")
        if "```json" not in body:
            continue
        try:
            payload = body.split("```json", 1)[1].split("```", 1)[0]
            document = json.loads(payload)
        except Exception:
            continue
        if isinstance(document, dict):
            return document
    return None


def collect_and_evaluate(
    request: Callable[..., Any],
    token: Any,
    repo: str,
    issue_number: int,
    *,
    now: float,
    max_age_seconds: float = 900.0,
    stale_minutes: float = 5.0,
) -> Dict[str, Any]:
    """Evaluate the published status and any unclaimed eligible issue."""
    document = newest_status_document(request, token, repo, issue_number)
    verdict = evaluate_external(document, now=now, max_age_seconds=max_age_seconds,
                               expected_repository=repo)
    alerts = list(verdict["alerts"])
    try:
        issues, _ = request(
            "GET", "/repos/%s/issues?state=open&labels=hermes-ready&per_page=50" % repo, token)
        issues = issues or []
        ready_since = ready_since_from_events(request, token, repo, issues)
        try:
            with open(OBSERVATIONS_PATH, encoding="utf-8") as fh:
                observations = json.load(fh)
        except Exception:
            observations = {}
        alerts += unclaimed_eligible_alerts(
            issues, now=now, ready_since=ready_since, first_seen=observations,
            stale_minutes=stale_minutes)
    except Exception:
        pass
    blocking = [a for a in alerts if a.get("severity") != "info"]
    return {"ok": not blocking, "alerts": alerts, "blocking": len(blocking),
            "age_seconds": verdict["age_seconds"],
            "independence": verdict["independence"], "have_status": document is not None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="External (VPS-independent) worker status check")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--issue-number", type=int, default=int(os.environ.get("STATUS_ISSUE_NUMBER", "0") or 0))
    ap.add_argument("--max-age-seconds", type=float, default=900.0)
    ap.add_argument("--stale-minutes", type=float, default=5.0)
    ap.add_argument("--fail-on-alert", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not args.issue_number:
        print(json.dumps({"ok": False, "alerts": [{"code": "status_issue_unconfigured",
              "severity": "critical", "message": "no STATUS_ISSUE_NUMBER configured"}]}))
        return 1 if args.fail_on_alert else 0

    import issue_github
    import issue_poller

    token = issue_poller.read_token("/opt/data/.env")
    report = collect_and_evaluate(issue_github.request, token, args.repo, args.issue_number,
                                  now=time.time(), max_age_seconds=args.max_age_seconds,
                                  stale_minutes=args.stale_minutes)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else
          "external check: ok=%s age=%s alerts=%s" % (report["ok"], report["age_seconds"],
                                                      [a["code"] for a in report["alerts"]]))
    return 1 if (args.fail_on_alert and not report["ok"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
