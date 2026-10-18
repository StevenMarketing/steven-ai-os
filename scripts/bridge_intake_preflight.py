#!/usr/bin/env python3
"""Intake preflight: never let a hermes-ready issue be skipped silently (blocker 4).

Lists open issues labelled ``hermes-ready``, runs the deterministic preflight
validator, prints structured rejection reasons, optionally posts a GitHub-visible
comment, and reports eligible issues that nothing claimed.

Usage:
    PYTHONPATH=src python3 scripts/bridge_intake_preflight.py                 # report only
    PYTHONPATH=src python3 scripts/bridge_intake_preflight.py --post          # post rejections
    PYTHONPATH=src python3 scripts/bridge_intake_preflight.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gbp.bridge.intake import (  # noqa: E402
    first_seen_update,
    preflight,
    ready_since_from_events,
    render_rejection,
    unclaimed_eligible_alerts,
)

#: First-seen observations, so an issue is never declared late without a reference.
OBSERVATIONS_PATH = os.environ.get("STEVEN_INTAKE_OBSERVATIONS",
                                  "/opt/data/ops/bridge/intake-observations.json")


def _load_observations() -> dict:
    try:
        with open(OBSERVATIONS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_observations(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(OBSERVATIONS_PATH), exist_ok=True)
        tmp = OBSERVATIONS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, OBSERVATIONS_PATH)
    except Exception:
        pass  # observation bookkeeping must never break the check

DEFAULT_REPO = "StevenMarketing/steven-ai-os"
WORKER_HOME = os.environ.get("STEVEN_WORKER_PROFILE_HOME", "/opt/data/profiles/worker-agent")


def _ledger() -> dict:
    path = os.path.join(WORKER_HOME, "ops", "github-worker", "state.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Structured intake preflight for hermes-ready issues")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--post", action="store_true", help="post a visible rejection comment")
    ap.add_argument("--dry-run", action="store_true", default=True, help="default: never write")
    ap.add_argument("--stale-minutes", type=float, default=5.0)
    ap.add_argument("--no-record", action="store_true",
                    help="do not persist first-seen observations")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    import issue_github
    import issue_poller
    import issue_worker_lib

    token = issue_poller.read_token("/opt/data/.env")
    issues, _ = issue_github.request(
        "GET", "/repos/%s/issues?state=open&labels=hermes-ready&per_page=50" % args.repo, token)

    patterns = list(getattr(issue_worker_lib, "INJECTION_PATTERNS", ()))
    report = {"repo": args.repo, "checked": 0, "usable": [], "skipped": [], "unclaimed": []}
    for issue in issues or []:
        if "pull_request" in issue:
            continue
        report["checked"] += 1
        verdict = preflight(issue.get("body"), patterns=patterns)
        entry = {"issue": issue["number"], "title": (issue.get("title") or "")[:90],
                 "missing_headings": verdict["missing_headings"],
                 "tripwire": verdict["tripwire"],
                 "reasons": verdict["reasons"]}
        if verdict["usable"]:
            report["usable"].append(entry)
        else:
            report["skipped"].append(entry)
            if args.post:
                issue_github.request(
                    "POST", "/repos/%s/issues/%d/comments" % (args.repo, issue["number"]), token,
                    body={"body": render_rejection(issue["number"], verdict)})
                entry["rejection_posted"] = True

    ledger = _ledger()
    eligible = [i["number"] for i in (issues or [])
                if "hermes-ready" in [l.get("name") for l in i.get("labels") or []]]
    now = time.time()
    observations = _load_observations()
    observations = first_seen_update(observations, eligible, now)
    if not args.no_record:
        _save_observations(observations)
    ready_since = ready_since_from_events(issue_github.request, token, args.repo, issues or [])
    report["ready_since"] = ready_since
    report["unclaimed"] = unclaimed_eligible_alerts(
        issues or [], now=now, claims=ledger.get("claims") or {},
        processed=ledger.get("processed") or {}, ready_since=ready_since,
        first_seen=observations, stale_minutes=args.stale_minutes)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("intake preflight: checked=%d usable=%d skipped=%d unclaimed=%d"
              % (report["checked"], len(report["usable"]), len(report["skipped"]),
                 len(report["unclaimed"])))
        for entry in report["skipped"]:
            print("  SKIPPED #%s missing=%s tripwire=%s"
                  % (entry["issue"], entry["missing_headings"], entry["tripwire"]))
        for entry in report["unclaimed"]:
            print("  UNCLAIMED #%s waiting=%ss" % (entry["issue"], entry.get("waiting_seconds")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
