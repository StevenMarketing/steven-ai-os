#!/usr/bin/env python3
"""Intake preflight: a hermes-ready issue is never skipped silently (Issue #45).

Runs on the VPS (cron) and on GitHub Actions. It answers three questions with
deterministic, offline-testable logic:

  1. Is every ``hermes-ready`` issue usable by the worker? If not, why (structured).
  2. Is an eligible issue sitting unclaimed past the threshold? The reference time comes
     from **trusted GitHub data** (the ``hermes-ready`` label event, or an existing claim)
     so alerting needs no persisted state and works on an ephemeral runner.
  3. Should this run fail loudly? ``--fail-on-alert`` exits non-zero when anything
     blocking was found, which is what turns the external watchdog red.

Usage:
    PYTHONPATH=src python3 scripts/bridge_intake_preflight.py                 # report only
    PYTHONPATH=src python3 scripts/bridge_intake_preflight.py --post          # post rejections
    PYTHONPATH=src python3 scripts/bridge_intake_preflight.py --fail-on-alert --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gbp.bridge.intake import (  # noqa: E402
    first_seen_update,
    preflight,
    ready_since_from_events,
    render_rejection,
    unclaimed_eligible_alerts,
)

DEFAULT_REPO = "StevenMarketing/steven-ai-os"
WORKER_HOME = os.environ.get("STEVEN_WORKER_PROFILE_HOME", "/opt/data/profiles/worker-agent")

#: First-seen observations: a last-resort reference time on a long-lived host only.
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


def _ledger() -> dict:
    path = os.path.join(WORKER_HOME, "ops", "github-worker", "state.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def evaluate_intake(
    request: Callable[..., Any],
    token: str,
    repo: str,
    *,
    now: Optional[float] = None,
    stale_minutes: float = 5.0,
    patterns: Optional[List[Any]] = None,
    observations: Optional[Dict[str, Any]] = None,
    ledger: Optional[Dict[str, Any]] = None,
    post: bool = False,
) -> Dict[str, Any]:
    """Evaluate every open ``hermes-ready`` issue. Pure enough to unit test.

    ``request`` is the repository's authenticated transport
    ``(method, path, token, body=None) -> (payload, meta)``. The returned report carries
    ``blocking`` (the number of findings that must fail a watchdog run) and the flat
    ``reasons`` list, so a caller never has to re-derive why an issue was unusable.
    """
    now = float(now if now is not None else time.time())
    ledger = ledger if ledger is not None else _ledger()
    observations = observations or {}
    report: Dict[str, Any] = {
        "repo": repo, "now": now, "checked": 0, "usable": [], "skipped": [],
        "unclaimed": [], "blocking": 0, "reasons": [], "ready_since": {},
    }

    issues_payload, _ = request(
        "GET", "/repos/%s/issues?state=open&labels=hermes-ready&per_page=50" % repo, token)
    issues = [i for i in (issues_payload or []) if "pull_request" not in i]

    for issue in issues:
        report["checked"] += 1
        verdict = preflight(issue.get("body"), patterns=patterns)
        entry = {"issue": issue["number"], "title": (issue.get("title") or "")[:90],
                 "missing_headings": verdict["missing_headings"],
                 "tripwire": verdict["tripwire"], "reasons": verdict["reasons"]}
        if verdict["usable"]:
            report["usable"].append(entry)
        else:
            report["skipped"].append(entry)
            report["reasons"] += [
                {"code": reason["code"], "issue": issue["number"],
                 "detail": reason.get("detail")}
                for reason in verdict["reasons"]]
            if post:
                request("POST", "/repos/%s/issues/%d/comments" % (repo, issue["number"]), token,
                        body={"body": render_rejection(issue["number"], verdict)})
                entry["rejection_posted"] = True

    # Trusted reference times: the hermes-ready label event (no state needed), then an
    # existing claim, then this observer's first-seen record on a long-lived host.
    report["ready_since"] = ready_since_from_events(request, token, repo, issues)
    observations = first_seen_update(observations, [i["number"] for i in issues], now)
    report["unclaimed"] = unclaimed_eligible_alerts(
        issues, now=now, claims=ledger.get("claims") or {},
        processed=ledger.get("processed") or {},
        ready_since=report["ready_since"], first_seen=observations,
        stale_minutes=stale_minutes)

    blocking = list(report["skipped"]) + [a for a in report["unclaimed"]
                                          if a.get("severity") != "info"]
    report["blocking"] = len(blocking)
    report["observations"] = observations
    return report


def main(argv=None, request: Optional[Callable[..., Any]] = None,
         token: Optional[str] = None, patterns: Optional[List[Any]] = None) -> int:
    """CLI entry point. ``request``/``token``/``patterns`` are injectable for tests."""
    ap = argparse.ArgumentParser(description="Structured intake preflight for hermes-ready issues")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--post", action="store_true", help="post a visible rejection comment")
    ap.add_argument("--dry-run", action="store_true", default=True, help="default: never write")
    ap.add_argument("--stale-minutes", type=float, default=5.0)
    ap.add_argument("--no-record", action="store_true",
                    help="do not persist first-seen observations (ephemeral runners)")
    ap.add_argument("--fail-on-alert", action="store_true",
                    help="exit 1 when an invalid or unclaimed hermes-ready issue is found")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    import issue_github

    if request is None:
        request = issue_github.request
    if patterns is None:
        import issue_worker_lib

        patterns = list(getattr(issue_worker_lib, "INJECTION_PATTERNS", ()))
    if token is None:
        token = os.environ.get("GITHUB_TOKEN") or ""
        if not token:                  # local runs read the repository token file
            import issue_poller

            token = issue_poller.read_token("/opt/data/.env")

    observations = {} if args.no_record else _load_observations()
    report = evaluate_intake(
        request, token, args.repo, stale_minutes=args.stale_minutes,
        patterns=patterns, observations=observations, post=args.post)

    if not args.no_record:
        _save_observations(report.get("observations") or {})

    report.pop("observations", None)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("intake preflight: checked=%d usable=%d skipped=%d unclaimed=%d blocking=%d"
              % (report["checked"], len(report["usable"]), len(report["skipped"]),
                 len(report["unclaimed"]), report["blocking"]))
        for entry in report["skipped"]:
            print("  SKIPPED #%s missing=%s tripwire=%s"
                  % (entry["issue"], entry["missing_headings"], entry["tripwire"]))
        for entry in report["unclaimed"]:
            print("  %s #%s waiting=%ss reference=%s"
                  % (entry["code"].upper(), entry["issue"], entry.get("waiting_seconds"),
                     entry.get("reference")))
    if args.fail_on_alert and report["blocking"]:
        print("intake preflight: FAIL - %d blocking finding(s)" % report["blocking"],
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
