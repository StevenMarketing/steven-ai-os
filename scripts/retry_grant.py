#!/usr/bin/env python3
"""Owner-authorized, one-time retry eligibility for a previously blocked issue.

Why this exists
---------------
The worker ledger is idempotent: once an issue finished with ``blocked`` (or
``failed``), ``StateStore.is_processed`` refuses to pick it up again.  A scope
revision approved by the owner can make a retry legitimate, but only *once* and
only with a *record* of what the previous run decided.  This module implements
that narrow escape hatch.

It never runs the retry itself -- it only re-opens eligibility for exactly one
issue number (default #19), after:

* an explicit issue number, a non-trivial reason and an explicit owner
  authorization string are all supplied;
* the live issue is confirmed still open with the revised offline-only scope;
* an immutable copy of the prior outcome plus a hash-verified ledger backup are
  written before anything changes;
* the ledger is rewritten atomically (temp file + ``os.replace``) with rollback
  on any failure;
* an append-only audit event is recorded;
* a second invocation carrying the same approval is refused.

Spend limits, the attempt/retry cap, worker policy and every protection stay
exactly as they were: only the ``processed`` idempotency entry is lifted, so the
retry still counts against the existing budgets and attempt cap.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import issue_worker_lib as lib  # noqa: E402

#: the only issue this escape hatch may ever touch
SUPPORTED_ISSUE = 19
#: the only login whose authorization counts
OWNER = "StevenMarketing"
#: the exact authorization phrase the owner must pass with ``--authorization``
AUTHORIZATION = "retry-issue-19-once"
AUDIT_NAME = "retry-grant-audit.jsonl"
MIN_REASON_CHARS = 8

ScopeChecker = Callable[[int], Tuple[bool, str]]


class GrantError(RuntimeError):
    """The request was refused, or the change was applied and rolled back."""


def approval_id(issue: Any, owner: str, authorization: str) -> str:
    raw = f"{int(issue)}|{owner}|{authorization}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def validate_request(issue: Any, reason: Optional[str], owner: Optional[str],
                     authorization: Optional[str], *,
                     supported_issue: int = SUPPORTED_ISSUE,
                     expected_owner: str = OWNER,
                     expected_authorization: str = AUTHORIZATION) -> Dict[str, Any]:
    """All four explicit inputs, or an error.  No defaults, no guessing."""
    if issue is None or str(issue).strip() == "":
        raise GrantError("an explicit issue number is required (--issue)")
    try:
        number = int(str(issue).strip())
    except (TypeError, ValueError):
        raise GrantError(f"issue number must be an integer, got {issue!r}")
    if number != int(supported_issue):
        raise GrantError(
            f"invalid scope: this command may only re-open issue "
            f"#{supported_issue}, not #{number}")
    text = (reason or "").strip()
    if len(text) < MIN_REASON_CHARS:
        raise GrantError(
            f"a reason of at least {MIN_REASON_CHARS} characters is required")
    if (owner or "").strip() != expected_owner:
        raise GrantError(
            f"owner authorization required: only {expected_owner} may grant a retry")
    if (authorization or "").strip() != expected_authorization:
        raise GrantError("missing or incorrect owner authorization phrase")
    return {
        "issue": number,
        "reason": text,
        "owner": expected_owner,
        "authorization": expected_authorization,
        "approval_id": approval_id(number, expected_owner, expected_authorization),
    }


def default_scope_checker(issue: int, token: Optional[str] = None) -> Tuple[bool, str]:
    """Confirm the live issue is open with the revised offline-only scope.

    Fail-closed: without a token, or on any API error, this refuses rather than
    assuming the issue is still eligible.  Tests inject their own checker so the
    suite stays offline.
    """
    import issue_github as gh  # network import kept lazy

    if not token:
        return False, "no GitHub token -- cannot confirm the live scope"
    repo = os.environ.get("STEVEN_WORKER_REPO") or "StevenMarketing/steven-ai-os"
    try:
        data, _ = gh.request("GET", f"/repos/{repo}/issues/{int(issue)}", token)
    except Exception as exc:  # pragma: no cover - network failure path
        return False, f"could not read issue #{issue}: {type(exc).__name__}"
    if not isinstance(data, dict):
        return False, "malformed issue payload"
    if str(data.get("state")) != "open":
        return False, f"issue #{issue} is not open (state={data.get('state')})"
    body = str(data.get("body") or "").lower()
    if "offline" not in body:
        return False, f"issue #{issue} no longer carries the offline-only scope"
    return True, "open with the revised offline-only scope"


class RetryGrant:
    """One-time, audited retry eligibility for a single issue."""

    def __init__(self, state_dir: Optional[str] = None, *,
                 state_path: Optional[str] = None,
                 scope_checker: Optional[ScopeChecker] = None,
                 supported_issue: int = SUPPORTED_ISSUE,
                 owner: str = OWNER,
                 clock: Optional[Callable[[], float]] = None) -> None:
        base = state_dir or lib.default_state_dir()
        self.state_path = state_path or os.path.join(base, "state.json")
        self.state_dir = base
        self.audit_path = os.path.join(base, AUDIT_NAME)
        self.backup_dir = os.path.join(base, "backups", "retry-grant")
        self.scope_checker = scope_checker or default_scope_checker
        self.supported_issue = int(supported_issue)
        self.owner = owner
        self._clock = clock or time.time

    # -- io ---------------------------------------------------------------
    def _now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("version", 1)
        data.setdefault("processed", {})
        data.setdefault("claims", {})
        data.setdefault("usage", {})
        return data

    def _write_atomic(self, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        import tempfile

        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.state_path) or ".",
                                   prefix=".retry-grant-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.state_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _append_audit(self, event: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.audit_path) or ".", exist_ok=True)
        row = dict(event)
        row.setdefault("at", self._now())
        row.setdefault("issue", self.supported_issue)
        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    def history(self) -> List[Dict[str, Any]]:
        """Every audit event for this issue, oldest first (append-only file)."""
        rows: List[Dict[str, Any]] = []
        try:
            with open(self.audit_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            continue
        except OSError:
            return []
        return rows

    # -- backup -----------------------------------------------------------
    def _backup(self, data: Dict[str, Any]) -> Tuple[str, str]:
        os.makedirs(self.backup_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self._clock()))
        path = os.path.join(self.backup_dir, f"issue-{self.supported_issue}-{stamp}.json")
        payload = {"prior_outcome": (data.get("processed") or {}).get(str(self.supported_issue)),
                   "ledger": data,
                   "captured_at": self._now()}
        blob = json.dumps(payload, indent=2, sort_keys=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(blob)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        return path, digest

    @staticmethod
    def verify_backup(path: str, digest: str) -> bool:
        with open(path, "r", encoding="utf-8") as fh:
            blob = fh.read()
        return hashlib.sha256(blob.encode("utf-8")).hexdigest() == digest

    # -- the operation ----------------------------------------------------
    def plan(self, issue: Any, reason: Optional[str], owner: Optional[str],
             authorization: Optional[str]) -> Dict[str, Any]:
        """Dry run: validate and describe, never write."""
        req = validate_request(issue, reason, owner, authorization,
                               supported_issue=self.supported_issue,
                               expected_owner=self.owner)
        data = self._read()
        ok, detail = self.scope_checker(req["issue"])
        return {
            "dry_run": True,
            "issue": req["issue"],
            "approval_id": req["approval_id"],
            "reason": req["reason"],
            "scope_ok": bool(ok),
            "scope_detail": detail,
            "prior_outcome": (data.get("processed") or {}).get(str(req["issue"])),
            "state_path": self.state_path,
        }

    def grant(self, issue: Any, reason: Optional[str], owner: Optional[str],
              authorization: Optional[str]) -> Dict[str, Any]:
        req = validate_request(issue, reason, owner, authorization,
                               supported_issue=self.supported_issue,
                               expected_owner=self.owner)
        ok, detail = self.scope_checker(req["issue"])
        if not ok:
            raise GrantError(f"scope not confirmed: {detail}")

        key = str(req["issue"])
        with lib.InterProcessLock(os.path.join(self.state_dir, "worker.lock"),
                                  timeout=10.0):
            data = self._read()
            events = self.history()
            if any(e.get("event") == "granted"
                   and e.get("approval_id") == req["approval_id"] for e in events):
                raise GrantError("this approval was already used for an earlier grant")
            prior = (data.get("processed") or {}).get(key)
            if (data.get("retry_grants") or {}).get(key, {}).get("status") == "available":
                raise GrantError(f"a retry grant for #{key} is already available")

            backup_path, digest = self._backup(data)
            if not self.verify_backup(backup_path, digest):
                raise GrantError("backup verification failed -- nothing was changed")
            self._append_audit({"event": "backup", "approval_id": req["approval_id"],
                                "backup": backup_path, "sha256": digest,
                                "prior_outcome": (prior or {}).get("outcome")})

            before = {"version": data.get("version"),
                      "usage": copy.deepcopy(data.get("usage")),
                      "claims": copy.deepcopy(data.get("claims")),
                      "protected_keys": sorted(set(data) - {"processed", "retry_grants"})}
            new = copy.deepcopy(data)
            new.setdefault("retry_grants", {})
            new["retry_grants"][key] = {
                "status": "available",
                "approval_id": req["approval_id"],
                "reason": req["reason"],
                "owner": req["owner"],
                "granted_at": self._now(),
                "prior_outcome": (prior or {}).get("outcome"),
                "prior_pr": (prior or {}).get("pr"),
                "backup": backup_path,
                "backup_sha256": digest,
            }
            new.setdefault("processed", {}).pop(key, None)

            try:
                self._write_atomic(new)
                self._after_apply(new)
                after = self._read()
                if (after.get("usage") != before["usage"]
                        or after.get("claims") != before["claims"]
                        or after.get("version") != before["version"]
                        or sorted(set(after) - {"processed", "retry_grants"})
                        != before["protected_keys"]):
                    raise GrantError("a protected field changed -- rolling back")
            except Exception as exc:  # roll back to the exact prior ledger
                self._write_atomic(data)
                self._append_audit({"event": "failed", "approval_id": req["approval_id"],
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "rolled_back": True})
                raise GrantError(f"apply failed and was rolled back: {exc}")

            self._append_audit({
                "event": "granted", "approval_id": req["approval_id"],
                "reason": req["reason"], "owner": req["owner"],
                "prior_outcome": (prior or {}).get("outcome"),
                "backup": backup_path, "sha256": digest,
            })
            return {"granted": True, "issue": req["issue"],
                    "approval_id": req["approval_id"], "backup": backup_path,
                    "backup_sha256": digest,
                    "prior_outcome": (prior or {}).get("outcome"),
                    "state_path": self.state_path}

    # test seam: a hook that runs after the ledger is written, before the audit
    def _after_apply(self, data: Dict[str, Any]) -> None:  # pragma: no cover
        return None


# --------------------------------------------------------------------------
def _read_token() -> Optional[str]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    for candidate in ("/opt/data/.env", os.path.expanduser("~/.hermes/.env")):
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as fh:
                    for line in fh:
                        key, sep, value = line.strip().partition("=")
                        if sep and key.strip() == "GITHUB_TOKEN":
                            return value.strip().strip("'\"")
            except OSError:
                continue
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("command", choices=["preview", "grant", "history", "verify-backup"])
    p.add_argument("--issue", default=None, help="explicit issue number (19 only)")
    p.add_argument("--reason", default=None)
    p.add_argument("--owner", default=None)
    p.add_argument("--authorization", default=None)
    p.add_argument("--state-dir", default=None)
    p.add_argument("--backup", default=None, help="verify-backup: backup file")
    p.add_argument("--sha256", default=None, help="verify-backup: expected digest")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    grant = RetryGrant(args.state_dir)
    try:
        if args.command == "preview":
            out = grant.plan(args.issue, args.reason, args.owner, args.authorization)
        elif args.command == "grant":
            grant.scope_checker = _token_scoped_checker(grant.scope_checker)
            out = grant.grant(args.issue, args.reason, args.owner, args.authorization)
        elif args.command == "history":
            out = {"issue": grant.supported_issue, "events": grant.history()}
        else:
            ok = RetryGrant.verify_backup(args.backup or "", args.sha256 or "")
            out = {"backup": args.backup, "verified": ok}
            print(json.dumps(out, indent=2))
            return 0 if ok else 1
    except GrantError as exc:
        print(json.dumps({"refused": str(exc)}, indent=2))
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


def _token_scoped_checker(base: ScopeChecker) -> ScopeChecker:
    token = _read_token()

    def check(issue: int) -> Tuple[bool, str]:
        if base is not default_scope_checker:
            return base(issue)
        return default_scope_checker(issue, token)
    return check


if __name__ == "__main__":
    sys.exit(main())
