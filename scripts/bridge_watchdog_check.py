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
from datetime import datetime
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
    """Return the parsed JSON from the most recent status comment, or None.

    GitHub returns issue comments oldest-first unless asked otherwise, so reading page 1
    used to return *ancient* documents and made the watchdog report a false
    ``status_stale`` once the status issue passed 100 comments. We now ask for
    newest-first ordering and, to stay correct even if a caller hands us another
    ordering, prefer the document with the greatest parsable timestamp, falling back to
    the last entry when no document carries one (the legacy behaviour).
    """
    comments, _ = request(
        "GET", "/repos/%s/issues/%d/comments?per_page=100&sort=created&direction=desc"
        % (repo, int(issue_number)), token)
    return _pick_newest_document(comments or [])


def _parsable_status_documents(comments: Any) -> List[Dict[str, Any]]:
    """Every comment body that carries a parsable ``json`` status document, in page order."""
    documents = []
    for comment in comments or []:
        body = str(comment.get("body") or "")
        if "```json" not in body:
            continue
        try:
            payload = body.split("```json", 1)[1].split("```", 1)[0]
            parsed = json.loads(payload)
        except Exception:
            continue
        if isinstance(parsed, dict):
            documents.append(parsed)
    return documents


def _unreadable(reason: str) -> Dict[str, Any]:
    """A status document that must alert: never a silent fallback to an older document."""
    return {"generated_at": None, "unreadable": True, "reason": reason,
            "worker": {}, "cron": {}, "budget": {}}


def _pick_newest_document(comments: Any) -> Optional[Dict[str, Any]]:
    """Authoritative status document, failing closed when the newest one is unusable.

    The page is requested newest-first, so the first fence-bearing comment is the newest
    status. If that comment is malformed (or its document has no parsable ``generated_at``)
    we return an *unreadable* document so the watchdog alerts -- we never quietly fall back
    to an older, healthy-looking document, which would hide a broken publisher. Within the
    page we still prefer the greatest parsable timestamp so a caller that hands us another
    ordering stays correct, and a page with no status comment at all returns None.
    """
    candidates = []
    for comment in comments or []:
        body = str(comment.get("body") or "")
        if "```json" not in body:
            continue
        try:
            parsed = json.loads(body.split("```json", 1)[1].split("```", 1)[0])
        except Exception:
            parsed = None
        candidates.append(parsed if isinstance(parsed, dict) else None)
    if not candidates:
        return None
    newest = candidates[0]
    if newest is None:
        return _unreadable("malformed_newest_status")
    documents = [d for d in candidates if d is not None]
    stamped = []
    for document in documents:
        value = document.get("generated_at")
        if not isinstance(value, str):
            continue
        try:
            stamped.append((datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ"), document))
        except ValueError:
            continue
    if stamped and len(stamped) == len(documents):
        # every candidate is timed: prefer the greatest timestamp (tolerates another ordering)
        picked = max(stamped, key=lambda item: item[0])[1]
    else:
        # the newest status comment decides -- never a silent fallback to an older document
        picked = newest
    value = picked.get("generated_at")
    if not isinstance(value, str):
        return _unreadable("newest_status_without_timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return _unreadable("newest_status_unparsable_timestamp")
    return picked


def _legacy_pick_newest_document(documents: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Kept for callers/tests that pass an already-parsed list."""
    if not documents:
        return None
    stamped = []
    for document in documents:
        value = document.get("generated_at")
        if not isinstance(value, str):
            continue
        try:
            stamped.append((datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ"), document))
        except ValueError:
            continue
    if stamped:
        return max(stamped, key=lambda item: item[0])[1]
    return documents[-1]

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

    token = os.environ.get("GITHUB_TOKEN") or ""
    if not token:                      # local runs read the repo token file
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
