#!/usr/bin/env python3
"""Poll GitHub for issues this worker is allowed to pick up.

Modes
-----
``--fixture FILE``  offline: read issues + label events from a JSON fixture.
(default)           live: GitHub REST over HTTPS using a token from the
                    environment (``GITHUB_TOKEN``) or ``--token-file``.

Output
------
``--watch``   prints ONLY the deterministic fingerprint. This is the cron
              ``monitor=`` gate: unchanged output means "nothing new" and the
              agent is never started, so polling costs $0.00 in AI usage.
``--json``    prints the full verdict for every candidate issue.
(default)    prints a short human summary.

The token is never printed, logged or written anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import issue_worker_lib as lib  # noqa: E402

API_ROOT = "https://api.github.com"
USER_AGENT = "steven-ai-os-issue-worker/1.0"
TIMEOUT = 30


# --------------------------------------------------------------------------
# Live API
# --------------------------------------------------------------------------
def _request(path: str, token: str) -> Any:
    url = path if path.startswith("http") else API_ROOT + path
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def read_token(token_file: Optional[str]) -> str:
    """Token from the environment, or from a file (``.env`` or a bare token).

    Accepts a ``GITHUB_TOKEN=...`` line so the worker can point at the
    Hermes ``.env`` (mode 600) directly. The value is never returned in any
    message, printed, or logged.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    if token_file:
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            raise SystemExit(f"cannot read token file: {exc}")
        for line in raw.splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip() == "GITHUB_TOKEN":
                token = value.strip().strip("'\"").strip()
                break
        if not token:
            token = raw.strip()
    if not token:
        raise SystemExit(
            "no GitHub token: set GITHUB_TOKEN in the environment or pass "
            "--token-file (never paste a token into a command line)")
    return token


def fetch_live(repo: str, token: str, label: str) -> Tuple[List[Dict[str, Any]],
                                                           Dict[str, List[Dict[str, Any]]]]:
    query = urllib.parse.urlencode({"state": "open", "labels": label, "per_page": 50})
    issues = _request(f"/repos/{repo}/issues?{query}", token)
    # The issues endpoint also returns pull requests -- drop them.
    issues = [i for i in issues if "pull_request" not in i]
    events: Dict[str, List[Dict[str, Any]]] = {}
    for issue in issues:
        number = issue.get("number")
        try:
            events[str(number)] = _request(
                f"/repos/{repo}/issues/{number}/events?per_page=100", token)
        except urllib.error.HTTPError as exc:  # events are best-effort
            events[str(number)] = []
            print(f"warning: cannot read events for #{number}: HTTP {exc.code}",
                  file=sys.stderr)
    return issues, events


def load_fixture(path: str) -> Tuple[List[Dict[str, Any]],
                                     Dict[str, List[Dict[str, Any]]]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("issues", []), data.get("events", {})


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------
def collect(issues: List[Dict[str, Any]], events: Dict[str, List[Dict[str, Any]]],
            owners: List[str], state: Optional[lib.StateStore] = None,
            label: str = lib.LABEL_READY) -> Dict[str, Any]:
    verdicts = []
    for issue in issues:
        number = issue.get("number")
        row = lib.eligibility(issue, events.get(str(number), []), owners)
        if row["eligible"] and state is not None and state.is_processed(number):
            row["eligible"] = False
            row["reason"] = "already processed (idempotency guard)"
        verdicts.append(row)
    eligible = [row for row in verdicts if row["eligible"]]

    # The fingerprint also moves when a claim goes stale, so a crashed worker
    # is retried instead of being silently abandoned.
    fingerprint = lib.change_fingerprint([
        {"number": row["number"], "updated_at": row["updated_at"],
         "title": row["title"]} for row in eligible])
    if state is not None:
        stale = ",".join(state.stale_claims())
        fingerprint = lib.change_fingerprint([
            {"number": row["number"], "updated_at": row["updated_at"],
             "title": row["title"]} for row in eligible] + (
            [{"number": "stale", "updated_at": stale, "title": ""}] if stale else []))

    return {"label": label, "owners": owners, "fingerprint": fingerprint,
            "eligible": eligible, "rejected": [r for r in verdicts if not r["eligible"]],
            "counts": {"eligible": len(eligible),
                       "rejected": len(verdicts) - len(eligible),
                       "total": len(verdicts)}}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Poll GitHub for hermes-ready issues.",
        allow_abbrev=False)  # a bare --token must never be accepted
    parser.add_argument("--repo", default="StevenMarketing/steven-ai-os")
    parser.add_argument("--owner", action="append", dest="owners",
                        help="login allowed to apply the approval label (repeatable)")
    parser.add_argument("--label", default=lib.LABEL_READY)
    parser.add_argument("--fixture", help="offline JSON fixture (issues + events)")
    parser.add_argument("--token-file", help="file holding the token (mode 600)")
    parser.add_argument("--state", default=os.path.join(lib.default_state_dir(), "state.json"))
    parser.add_argument("--watch", action="store_true",
                        help="print only the deterministic fingerprint (cron monitor)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-state", action="store_true",
                        help="ignore the processed/claim ledger")
    args = parser.parse_args(argv)

    owners = args.owners or [lib.DEFAULT_OWNER]
    state = None if args.no_state else lib.StateStore(args.state)

    if args.fixture:
        issues, events = load_fixture(args.fixture)
    else:
        try:
            issues, events = fetch_live(args.repo, read_token(args.token_file), args.label)
        except urllib.error.HTTPError as exc:
            print(f"GitHub API error: HTTP {exc.code}", file=sys.stderr)
            return 3
        except urllib.error.URLError as exc:
            print(f"GitHub API unreachable: {exc.reason}", file=sys.stderr)
            return 3

    result = collect(issues, events, owners, state, args.label)

    if args.watch:
        # Deterministic single line -- the agent only wakes up when it changes.
        print(result["fingerprint"])
        return 0
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    print(f"label={result['label']} owners={','.join(result['owners'])} "
          f"eligible={result['counts']['eligible']}/{result['counts']['total']} "
          f"fingerprint={result['fingerprint']}")
    for row in result["eligible"]:
        print(f"  READY   #{row['number']} {row['title']}  ({row['reason']})")
    for row in result["rejected"]:
        detail = row["reason"]
        if row["problems"]:
            detail += " | " + "; ".join(row["problems"])
        if row["injection"]:
            detail += " | injection: " + ",".join(row["injection"])
        print(f"  skipped #{row['number']} {row['title']}  ({detail})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
