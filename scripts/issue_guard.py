#!/usr/bin/env python3
"""Guard rails the worker must pass through before doing anything.

Every hard limit in the design is enforced here, by code -- never by trust:

* kill switch      : ``pause`` / ``resume`` (file next to the state ledger)
* idempotency      : an issue that already finished is never picked up again
* concurrency      : one live claim per issue, claims expire (TTL)
* budget           : per-issue estimate cap, daily cost cap, daily issue cap
* file scope       : ``check-paths`` refuses protected paths (auth, security,
                     workflows, secrets, the guard itself, ...)
* secret scan      : ``check-secrets`` refuses a commit that carries a real
                     credential (pattern match + exact match on live env values)

Usage
-----
    python3 scripts/issue_guard.py status
    python3 scripts/issue_guard.py can-start --issue 42 --estimate 0.04
    python3 scripts/issue_guard.py claim --issue 42
    python3 scripts/issue_guard.py finish --issue 42 --outcome done --pr URL --cost 0.031
    python3 scripts/issue_guard.py pause --reason "maintenance"
    python3 scripts/issue_guard.py check-paths --base origin/main
    python3 scripts/issue_guard.py check-secrets --root .

Exit code 0 = allowed / clean, 1 = refused / finding, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import issue_worker_lib as lib  # noqa: E402

TEXT_EXT = (".py", ".js", ".html", ".css", ".json", ".sql", ".md", ".txt", ".yml",
            ".yaml", ".cfg", ".ini", ".sh", ".toml", ".csv", ".gitignore")
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}


def _store(args: argparse.Namespace) -> lib.StateStore:
    return lib.StateStore(args.state)


def _fail(message: str) -> int:
    print(f"REFUSED: {message}")
    return 1


# --------------------------------------------------------------------------
def cmd_status(args: argparse.Namespace) -> int:
    store = _store(args)
    payload = {
        "state_file": store.path,
        "paused": store.is_paused(),
        "pause_reason": (open(store.pause_path).read().strip()
                         if os.path.exists(store.pause_path) else None),
        "limits": {
            "cost_per_issue": lib.MAX_COST_PER_ISSUE,
            "cost_per_day": lib.MAX_COST_PER_DAY,
            "issues_per_day": lib.MAX_ISSUES_PER_DAY,
            "default_estimate": lib.DEFAULT_ISSUE_ESTIMATE,
            "claim_ttl_seconds": lib.CLAIM_TTL_SECONDS,
        },
        "remaining_today": store.remaining_today(),
        "processed": store.data["processed"],
        "live_claims": {k: v for k, v in store.data["claims"].items()
                        if k not in store.stale_claims()},
        "stale_claims": store.stale_claims(),
        "usage": store.data["usage"],
        "note": "these caps are enforced by this worker; Hermes has no provider-level USD cap",
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_can_start(args: argparse.Namespace) -> int:
    store = _store(args)
    allowed, reason = store.can_start(args.issue, args.estimate)
    print(json.dumps({"issue": args.issue, "estimate": args.estimate,
                      "allowed": allowed, "reason": reason}, indent=2))
    return 0 if allowed else 1


def cmd_claim(args: argparse.Namespace) -> int:
    store = _store(args)
    allowed, reason = store.can_start(args.issue, args.estimate)
    if not allowed:
        return _fail(reason)
    try:
        run_id = store.claim(args.issue, ttl=args.ttl, timeout=args.lock_timeout)
    except lib.ClaimError as exc:
        return _fail(str(exc))
    except lib.LockTimeout as exc:
        return _fail(str(exc))
    store.append_audit({"event": "claim", "issue": args.issue, "run_id": run_id})
    print(json.dumps({"issue": args.issue, "run_id": run_id, "ttl": args.ttl}))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    store = _store(args)
    with store.transaction(timeout=args.lock_timeout):
        store._release_unlocked(str(args.issue))
        store.mark_processed(args.issue, args.outcome, pr=args.pr,
                             cost=args.cost, note=args.note)
        usage = store.record_usage(args.cost)
        store.append_audit({"event": "finish", "issue": args.issue,
                            "outcome": args.outcome, "pr": args.pr,
                            "cost": args.cost, "usage": usage, "note": args.note})
    print(json.dumps({"issue": args.issue, "outcome": args.outcome,
                      "usage_today": usage, "remaining": store.remaining_today()},
                     indent=2))
    return 0


def cmd_reclaim(args: argparse.Namespace) -> int:
    store = _store(args)
    stale = store.reclaim_stale()
    store.save()
    print(json.dumps({"reclaimed": stale}))
    return 0


def cmd_set_pause(args: argparse.Namespace, paused: bool) -> int:
    store = _store(args)
    store.set_paused(paused, getattr(args, "reason", "") or "")
    store.append_audit({"event": "pause" if paused else "resume",
                        "reason": getattr(args, "reason", "")})
    store.save()
    print(f"worker {'PAUSED' if paused else 'RUNNABLE'} ({store.pause_path})")
    return 0


# --------------------------------------------------------------------------
# Mandatory pre-commit / pre-push gate
# --------------------------------------------------------------------------
def _git_lines(cmd: List[str], cwd: Optional[str] = None) -> List[str]:
    try:
        out = subprocess.run(["git", *cmd], capture_output=True, text=True,
                             check=True, cwd=cwd).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def repo_root(cwd: Optional[str] = None) -> str:
    lines = _git_lines(["rev-parse", "--show-toplevel"], cwd)
    return lines[0] if lines else os.getcwd()


def resolve_base(base: str, cwd: Optional[str] = None) -> str:
    """First revision of (base, origin/main, main, HEAD~1) that exists."""
    for candidate in (base, "origin/main", "main", "HEAD~1"):
        if not candidate:
            continue
        if _git_lines(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
                      cwd):
            return candidate
    return ""


def changed_files(base: str = "origin/main", *, staged: bool = True,
                  include_untracked: bool = True,
                  cwd: Optional[str] = None) -> List[str]:
    """The FINAL state of the change set, including new and staged files.

    Committed range vs ``base`` + the index + (unless disabled) untracked files,
    so a file that has not been ``git add``-ed yet is still inspected rather than
    slipping past the gate.
    """
    files = set()
    resolved = resolve_base(base, cwd)
    if resolved:
        files.update(_git_lines(["diff", "--name-only", f"{resolved}...HEAD"], cwd))
    if staged:
        files.update(_git_lines(["diff", "--cached", "--name-only"], cwd))
    if include_untracked:
        files.update(_git_lines(["ls-files", "--others", "--exclude-standard"], cwd))
    return sorted(files)


def cmd_verify_diff(args: argparse.Namespace) -> int:
    """Protected paths + credentials on the exact diff about to be committed."""
    root = args.root or repo_root()
    files = changed_files(args.base, staged=args.staged,
                          include_untracked=not args.no_untracked, cwd=root)
    review = lib.review_paths(files)
    live = _live_values(args.env_file)

    blocking: List[Dict[str, Any]] = []
    test_context: List[Dict[str, Any]] = []
    for path in files:
        full = os.path.join(root, path)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        rows = lib.scan_secrets(text, path) + lib.scan_against_live_values(text, live, path)
        for row in rows:
            (blocking if row["context"] in ("REVIEW", "BLOCK") else test_context).append(row)

    protected = review["blocked"]
    allow_protected = bool(os.environ.get("HERMES_ALLOW_PROTECTED"))
    payload = {
        "base": resolve_base(args.base, root) or "(no base found)",
        "root": root,
        "checked_files": len(files),
        "files": files,
        "protected_paths": protected,
        "secret_findings_blocking": blocking,
        "secret_findings_test_context": test_context,
        "protected_bypass": allow_protected,
    }
    print(json.dumps(payload, indent=2))

    if blocking:
        print("\nREFUSED: a real credential-shaped value is in this diff "
              "(secrets can never be bypassed)")
        return 1
    if protected and not allow_protected:
        print("\nREFUSED: protected paths are part of this diff and need a human:")
        for row in protected:
            print(f"  {row['path']}  <- {row['reason']}")
        print("  Owner maintenance only: re-run with HERMES_ALLOW_PROTECTED=1")
        return 1
    if protected:
        print(f"\nWARNING: {len(protected)} protected path(s) changed and "
              "HERMES_ALLOW_PROTECTED=1 was set - owner maintenance bypass in use")
    print(f"\nverified {len(files)} file(s): no blocking secret, path policy satisfied")
    return 0


def cmd_with_lock(args: argparse.Namespace) -> int:
    """Run a command while holding the global single-worker lock."""
    store = _store(args)
    lock = store.worker_lock(timeout=args.timeout)
    if not lock.acquire():
        print(f"REFUSED: another worker holds {store.lock_path} "
              f"(waited {args.timeout}s)")
        return 1
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    try:
        print(f"lock held: {store.lock_path} (timeout {args.timeout}s)")
        if not command:
            return 0
        return subprocess.run(command).returncode
    finally:
        lock.release()


# --------------------------------------------------------------------------
def cmd_check_paths(args: argparse.Namespace) -> int:
    if args.path:
        paths: List[str] = list(args.path)
    else:
        cmd = ["git", "diff", "--name-only", f"{args.base}...HEAD"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            return _fail(f"cannot diff against {args.base}: {exc}")
        paths = [line for line in out.splitlines() if line.strip()]
    review = lib.review_paths(paths)
    print(json.dumps({"base": args.base, "checked": len(paths), **review}, indent=2))
    if review["blocked"]:
        print("\nBLOCKED PATHS require a human:")
        for row in review["blocked"]:
            print(f"  {row['path']}  <- {row['reason']}")
        return 1
    return 0


def _iter_text_files(root: str) -> List[str]:
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(TEXT_EXT) or name == ".gitignore":
                out.append(os.path.join(dirpath, name))
    return out


def _live_values(env_file: Optional[str]) -> Dict[str, str]:
    """Real credentials, for an exact-substring check. Never printed.

    Both the process environment AND the env file are watched: a rotated token
    often lives in one but not the other, and a single-value slot would silently
    stop checking the other one.
    """
    watch = ("OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "API_SERVER_KEY", "GITHUB_TOKEN")
    values: Dict[str, str] = {}

    def remember(name: str, raw: str) -> None:
        value = (raw or "").strip().strip("'\"")
        if not value or len(value) < 12:
            return
        if name not in values:
            values[name] = value
        elif values[name] != value:
            values[f"{name}#2"] = value

    for name in watch:
        remember(name, os.environ.get(name, ""))
    if env_file and os.path.isfile(env_file):
        try:
            with open(env_file, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    key, _, raw = line.strip().partition("=")
                    if key.strip() in watch:
                        remember(key.strip(), raw)
        except OSError:
            pass
    return values


def cmd_check_secrets(args: argparse.Namespace) -> int:
    files = list(args.path) if args.path else _iter_text_files(args.root)
    live = _live_values(args.env_file)
    findings: List[Dict[str, Any]] = []
    blocking: List[Dict[str, Any]] = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        rows = lib.scan_secrets(text, path) + lib.scan_against_live_values(text, live, path)
        findings.extend(rows)
        blocking.extend(r for r in rows if r["context"] in ("REVIEW", "BLOCK"))
    print(json.dumps({
        "root": args.root,
        "files": len(files),
        "live_credentials_watched": sorted(live.keys()),
        "findings": len(findings),
        "blocking": blocking,
        "test_context": [r for r in findings if r["context"] == "test/fixture"],
    }, indent=2))
    if blocking:
        print("\nREFUSED: credential-shaped content outside test fixtures")
        return 1
    print("\nclean: no blocking credential found")
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guard rails for the issue worker.",
                                     allow_abbrev=False)
    parser.add_argument("--state",
                        default=os.path.join(lib.default_state_dir(), "state.json"))
    parser.add_argument("--lock-timeout", type=float, default=10.0,
                        help="seconds to wait for the inter-process worker lock")
    sub = parser.add_subparsers(dest="command_", required=False)

    sub.add_parser("status", help="show limits, pause switch, usage, claims")
    sub.add_parser("pending", help="alias for status")

    p = sub.add_parser("can-start", help="may this issue be started now?")
    p.add_argument("--issue", required=True)
    p.add_argument("--estimate", type=float, default=lib.DEFAULT_ISSUE_ESTIMATE)
    p.set_defaults(func=cmd_can_start)

    p = sub.add_parser("claim", help="take an exclusive, expiring claim")
    p.add_argument("--issue", required=True)
    p.add_argument("--estimate", type=float, default=lib.DEFAULT_ISSUE_ESTIMATE)
    p.add_argument("--ttl", type=int, default=lib.CLAIM_TTL_SECONDS)
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("finish", help="release the claim and record the outcome")
    p.add_argument("--issue", required=True)
    p.add_argument("--outcome", required=True,
                   choices=["done", "blocked", "failed", "needs-info"])
    p.add_argument("--pr")
    p.add_argument("--cost", type=float, default=0.0)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_finish)

    p = sub.add_parser("reclaim", help="drop claims whose TTL expired")
    p.set_defaults(func=cmd_reclaim)

    p = sub.add_parser("pause", help="kill switch on")
    p.add_argument("--reason", default="")
    p.set_defaults(func=lambda a: cmd_set_pause(a, True))

    p = sub.add_parser("resume", help="kill switch off")
    p.set_defaults(func=lambda a: cmd_set_pause(a, False))

    p = sub.add_parser("check-paths", help="refuse protected paths")
    p.add_argument("--base", default="origin/main")
    p.add_argument("--path", action="append")
    p.set_defaults(func=cmd_check_paths)

    p = sub.add_parser("check-secrets", help="refuse committed credentials")
    p.add_argument("--root", default=".")
    p.add_argument("--path", action="append")
    p.add_argument("--env-file",
                   default=os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), ".env"))
    p.set_defaults(func=cmd_check_secrets)

    p = sub.add_parser("verify-diff",
                       help="MANDATORY gate: protected paths + credentials on the "
                            "final diff (staged, committed and new files)")
    p.add_argument("--base", default="origin/main")
    p.add_argument("--staged", action="store_true", default=True)
    p.add_argument("--no-untracked", action="store_true")
    p.add_argument("--root")
    p.add_argument("--env-file",
                   default=os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), ".env"))
    p.set_defaults(func=cmd_verify_diff)

    p = sub.add_parser("with-lock",
                       help="run a command while holding the global worker lock")
    p.add_argument("--timeout", type=float, default=0.0)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_with_lock)

    sub.choices["status"].set_defaults(func=cmd_status)
    sub.choices["pending"].set_defaults(func=cmd_status)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        return _fail("no subcommand given (try: status, can-start, claim, finish, "
                     "verify-diff, with-lock, check-paths, check-secrets, pause, "
                     "resume, reclaim)")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
