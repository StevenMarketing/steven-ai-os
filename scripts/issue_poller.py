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
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import issue_github as gh  # noqa: E402
import issue_worker_lib as lib  # noqa: E402


def read_token(token_file: Optional[str]) -> str:
    """Token from the environment, or from a file (``.env`` or a bare token).

    Accepts a ``GITHUB_TOKEN=...`` line so the worker can point at the Hermes
    ``.env`` (mode 600) directly. If the file is a dotenv file without that key,
    this REFUSES instead of guessing -- falling back to the file's whole
    contents would send every other secret in it as an HTTP header.

    The value is never printed, logged or returned to a caller that reports it.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return _validated(token)
    if token_file:
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            raise SystemExit(f"cannot read token file: {exc}")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        for line in lines:
            key, sep, value = line.partition("=")
            if sep and key.strip() == "GITHUB_TOKEN":
                token = value.strip().strip("'\"").strip()
                break
        if not token:
            if _looks_like_dotenv(lines):
                raise SystemExit(
                    f"{token_file} looks like a dotenv file but has no "
                    "GITHUB_TOKEN= line; refusing to guess (never use a whole "
                    "env file as a token). Add GITHUB_TOKEN=... or point "
                    "--token-file at a file that contains only the token.")
            if len(lines) == 1:
                token = lines[0]
    if not token:
        raise SystemExit(
            "no GitHub token: set GITHUB_TOKEN in the environment or pass "
            "--token-file (never paste a token into a command line)")
    return _validated(token)


def _looks_like_dotenv(lines: Sequence[str]) -> bool:
    for line in lines:
        if line.startswith("#"):
            continue
        key, sep, _value = line.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            return True
    return False


def _validated(token: str) -> str:
    """Reject anything that is not a single opaque token value."""
    if not token or len(token) > 400 or any(ch.isspace() for ch in token):
        raise SystemExit(
            "refusing to use a malformed token value (a token is a single line "
            "of at most 400 characters with no whitespace)")
    return token


def fetch_live(repo: str, token: str, label: str,
               transport: Optional[gh.Transport] = None) -> Tuple[
                   List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """Every open issue carrying ``label`` plus its FULL event timeline.

    Both collections are paginated. Any API failure propagates: a failed read
    must never look like "this issue has no approval" or "there is no work",
    because that would be fail-open on a security decision.
    """
    issues = gh.list_issues(repo, token, label, transport=transport)
    events: Dict[str, List[Dict[str, Any]]] = {}
    for issue in issues:
        number = issue.get("number")
        events[str(number)] = gh.list_issue_events(repo, number, token,
                                                   transport=transport)
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


# --- structured intake rejection (activation of gbp.bridge.intake) -------------
# Wired by Issue #45. Off unless STEVEN_INTAKE_REJECT_COMMENTS=1, posts at most one
# comment per (issue, body) pair, and never raises into the poll.

_INTAKE_STATE = "/opt/data/ops/bridge/intake-rejections.json"


def _intake_rejections_enabled() -> bool:
    return os.environ.get("STEVEN_INTAKE_REJECT_COMMENTS", "0") == "1"


def _load_intake_state() -> Dict[str, Any]:
    try:
        with open(_INTAKE_STATE, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_intake_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(_INTAKE_STATE), exist_ok=True)
        tmp = _INTAKE_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp, _INTAKE_STATE)
    except Exception:
        pass


def _maybe_post_rejection(row: Dict[str, Any], token_file: str, repo: str) -> None:
    """Post one structured rejection comment for an unusable hermes-ready issue."""
    if not _intake_rejections_enabled():
        return
    try:
        import sys as _sys
        import time as _time
        from pathlib import Path as _Path

        _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
        import issue_github as _gh
        from gbp.bridge.intake import (
            preflight,
            record_rejection,
            rejection_key,
            render_rejection,
            should_post_rejection,
        )

        number = int(row["number"])
        token = read_token(token_file)
        issue, _ = _gh.request("GET", "/repos/%s/issues/%d" % (repo, number), token)
        body = issue.get("body") or ""
        verdict = preflight(body, patterns=lib.INJECTION_PATTERNS)
        if verdict["usable"]:
            return
        key = rejection_key(number, body)
        state = _load_intake_state()
        now = _time.time()
        if not should_post_rejection(state, key, now):
            return
        _gh.request("POST", "/repos/%s/issues/%d/comments" % (repo, number), token,
                    body={"body": render_rejection(number, verdict)})
        _save_intake_state(record_rejection(state, key, now))
    except Exception:
        return  # intake reporting must never break the poll


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
            issues, events = fetch_live(args.repo, read_token(args.token_file),
                                        args.label)
        except gh.RateLimited as exc:
            print(f"GitHub rate limited: {exc}", file=sys.stderr)
            if args.watch:
                print("status=rate-limited")
            return 3
        except gh.GitHubError as exc:
            print(f"GitHub API failure (fail-closed, no work assumed): {exc}",
                  file=sys.stderr)
            if args.watch:
                print("status=unavailable")
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
        _maybe_post_rejection(
            row,
            args.token_file or os.environ.get("STEVEN_TOKEN_FILE", "/opt/data/.env"),
            args.repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
