#!/usr/bin/env python3
"""Publish the redacted worker status to GitHub (blocker 3).

Reads live local sources (worker cron job file, worker ledger, budget guard), builds
the redacted document and publishes it as a comment on the machine-readable status
issue. Offline by default: ``--live`` is required to write anything.

Usage:
    PYTHONPATH=src python3 scripts/bridge_status_publish.py                 # dry run
    PYTHONPATH=src python3 scripts/bridge_status_publish.py --live          # publish
    PYTHONPATH=src python3 scripts/bridge_status_publish.py --json          # machine output
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gbp.bridge.publisher import build_redacted_document, publish_status  # noqa: E402

DEFAULT_REPO = "StevenMarketing/steven-ai-os"
WORKER_HOME = os.environ.get("STEVEN_WORKER_PROFILE_HOME", "/opt/data/profiles/worker-agent")
JOB_ID = "b4a0d80c816e"


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cron_state() -> dict:
    path = os.path.join(WORKER_HOME, "cron", "jobs.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        job = next((j for j in jobs if j.get("id") == JOB_ID), None)
        if not job:
            return {"state": "unknown", "detail": "worker job not found"}
        return {
            "state": "scheduled" if job.get("enabled") else "paused",
            "job_id": JOB_ID,
            "schedule": job.get("schedule_display") or "",
            "last_run_at": job.get("last_run_at") or "",
            "last_status": job.get("last_status") or "",
            "failure_streak": job.get("failure_streak"),
        }
    except Exception as exc:
        return {"state": "unknown", "detail": type(exc).__name__}


def _worker_state() -> dict:
    path = os.path.join(WORKER_HOME, "ops", "github-worker", "state.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        usage = data.get("usage") or {}
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        day = usage.get(today) or {}
        return {
            "state": "idle" if not (data.get("claims") or {}) else "working",
            "claims": len(data.get("claims") or {}),
            "issues_today": day.get("issues"),
            "cost_today": day.get("cost"),
        }
    except Exception as exc:
        return {"state": "unknown", "detail": type(exc).__name__}


def _budget_state() -> dict:
    try:
        import budget  # from scripts/

        usage = budget.provider_usage()
        guard = budget.BudgetGuard()
        spend = guard.spend_today(usage)["spend"]
        return {"spend_today": round(float(spend), 6), "cap": getattr(guard, "daily_usd", None),
                "provider_ok": usage.get("ok")}
    except Exception as exc:
        return {"state": "unknown", "detail": type(exc).__name__}


def build_document(repo: str = DEFAULT_REPO) -> dict:
    """The redacted status document, built from live local sources."""
    return build_redacted_document(
        generated_at=_utc_now(), worker=_worker_state(), cron=_cron_state(),
        budget=_budget_state(), repository=repo)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Publish the redacted Steven AI OS worker status")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--live", action="store_true", help="write to GitHub (default: dry run)")
    ap.add_argument("--issue", type=int, default=None, help="existing status issue number")
    ap.add_argument("--json", action="store_true", help="print machine-readable output")
    args = ap.parse_args(argv)

    document = build_redacted_document(
        generated_at=_utc_now(), worker=_worker_state(), cron=_cron_state(),
        budget=_budget_state(), repository=args.repo,
    )
    result = {"published": False, "reason": "dry_run"}
    if args.live:
        import issue_poller
        import issue_github

        token = issue_poller.read_token("/opt/data/.env")
        result = publish_status(issue_github.request, token, args.repo, document,
                                dry_run=False, issue_number=args.issue)
    if args.json:
        print(json.dumps({"document": document, "publish": result}, indent=2, sort_keys=True))
    else:
        print("status document (redacted):")
        print(json.dumps(document, indent=2, sort_keys=True))
        print("publish:", json.dumps(result, sort_keys=True))
    return 0 if (result.get("ok") or not args.live) else 1


if __name__ == "__main__":
    raise SystemExit(main())
