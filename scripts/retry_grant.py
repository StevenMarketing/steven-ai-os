#!/usr/bin/env python3
"""Owner-authorized, one-time retry eligibility for a previously blocked issue.

Why this exists
---------------
The worker ledger is idempotent: once an issue finished with ``blocked`` (or
``failed``), ``StateStore.is_processed`` refuses to pick it up again.  A scope
revision approved by the owner can make a retry legitimate, but only *once* and
only with a *record* of what the previous run decided.  This module implements
that narrow escape hatch for exactly one issue number (``SUPPORTED_ISSUE``).

It never runs the retry itself -- it only re-opens eligibility, and only after
every one of these holds:

* an explicit issue number and a non-trivial reason are supplied;
* the operator is named on the ``STEVEN_RETRY_GRANT_OPERATORS`` allow-list -- a
  permission the operator verifies separately, never self-declared on the CLI;
* an **owner approval artifact** exists on disk: a mode-600 JSON file the owner
  creates, naming the owner, the issue and the exact approved issue revision (a
  body sha256).  There is deliberately **no pass-phrase and no secret**: the
  artifact *is* the authorization, and it is consumed (renamed) the moment it is
  used, so it is single-use;
* the ledger being touched is the **worker-agent profile's** live ledger, never
  the default profile's (which never runs the worker);
* the live issue is open, its body still hashes to the approved revision, and
  its scope still names the approved directories while excluding protected
  server files (fail-closed without a token);
* a prior ``blocked`` record exists for the issue -- without it nothing is
  lifted (fail-closed);
* an immutable copy of the prior outcome plus a hash-verified ledger backup are
  written, the ledger is rewritten atomically with rollback on any failure, and
  an append-only, fsync'd audit event records both the backup and the grant;
* a durable pending marker makes a crash mid-change recoverable, and a leftover
  marker is reported rather than silently ignored.

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
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import issue_worker_lib as lib  # noqa: E402

#: the only issue this escape hatch may ever touch
SUPPORTED_ISSUE = 19
#: the only login whose approval artifact counts
AUTHORIZED_OWNER = "StevenMarketing"
#: schema version of the owner approval artifact
APPROVAL_SCHEMA_VERSION = 1
#: default filename of the owner approval artifact
APPROVAL_FILENAME = "issue-19-once.approval.json"
AUDIT_NAME = "retry-grant-audit.jsonl"
PENDING_NAME = "retry-grant.pending.json"
MIN_REASON_CHARS = 8
MIN_NONCE_CHARS = 16
#: comma-separated list of operator logins allowed to run the command
OPERATORS_ENV = "STEVEN_RETRY_GRANT_OPERATORS"
#: explicit pin of the worker-agent ledger directory
STATE_ENV = "STEVEN_RETRY_GRANT_STATE"
REPO_ENV = "STEVEN_WORKER_REPO"
DEFAULT_REPO = "StevenMarketing/steven-ai-os"

#: path marker proving the ledger belongs to the worker-agent profile rather
#: than the default profile (which never runs the issue worker)
WORKER_PROFILE_MARKER = os.path.join("profiles", "worker-agent", "ops", "github-worker")

#: directory names the revised, owner-approved scope of #19 must still name
REQUIRED_SCOPE_DIRS = ("web/", "tests/", "scripts/")
#: the approved scope must still state that protected server files are excluded
REQUIRED_SCOPE_PHRASE = "protected server files"
#: any of these appearing in the issue body means the scope grew into protected
#: territory and the retry must not be granted
PROTECTED_SCOPE_PATHS = (
    "src/gbp/api/server.py",
    "src/gbp/api/",
    "src/gbp/auth/",
    "src/gbp/authz/",
    "src/gbp/security/",
    "sql/schema.sql",
)

ScopeChecker = Callable[..., Tuple[bool, str]]


class GrantError(RuntimeError):
    """The request was refused, or the change was applied and rolled back."""


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------
def approval_id(issue: Any, owner: str, nonce: str) -> str:
    raw = f"{int(issue)}|{owner}|{nonce}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def body_sha256(body: Optional[str]) -> str:
    """Stable hash of an issue body: the approved-revision identity."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


def evaluate_scope(body: str) -> Tuple[bool, str]:
    """Validate the revised offline-only scope of the issue body.

    The approved revision must still (a) name the allowed directories and
    (b) state that protected server files are excluded, and must not mention any
    protected server path.  Pure and offline so it can be tested directly.
    """
    low = (body or "").lower()
    missing = [d for d in REQUIRED_SCOPE_DIRS if d.lower() not in low]
    if missing:
        return False, ("scope no longer names the approved directories: "
                       + ", ".join(missing))
    if REQUIRED_SCOPE_PHRASE not in low:
        return False, ("scope no longer states that protected server files are "
                       "excluded")
    hits = [p for p in PROTECTED_SCOPE_PATHS if p.lower() in low]
    if hits:
        return False, "scope names a protected server path: " + ", ".join(hits)
    return True, ("scope names the approved directories and excludes protected "
                  "server files")


def load_approval(path: Optional[str]) -> Dict[str, Any]:
    """Load and fully validate a owner approval artifact.  Fail-closed.

    The artifact is a JSON file the owner creates (mode 600, owned by the
    operator user).  No secret is involved: it names the owner, the issue, a
    nonce and the approved issue revision.
    """
    if not path:
        raise GrantError("an approval artifact path is required (--approval-file)")
    if not os.path.isfile(path):
        raise GrantError(
            f"approval artifact not found: {path!r} -- the owner must create it")
    st = os.stat(path)
    if st.st_uid != os.getuid():
        raise GrantError("approval artifact is not owned by the operator user")
    if st.st_mode & 0o077:
        raise GrantError(
            "approval artifact must be mode 600 (not group/other accessible)")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise GrantError(
            f"approval artifact is unreadable or malformed: {type(exc).__name__}")
    if not isinstance(data, dict):
        raise GrantError("approval artifact must be a JSON object")
    if int(data.get("schema_version", -1) or -1) != APPROVAL_SCHEMA_VERSION:
        raise GrantError("approval artifact has an unsupported schema_version")
    if str(data.get("owner", "")).strip() != AUTHORIZED_OWNER:
        raise GrantError(f"approval artifact must be issued by {AUTHORIZED_OWNER}")
    try:
        issue = int(str(data.get("issue")))
    except (TypeError, ValueError):
        raise GrantError("approval artifact must name the issue number")
    if issue != SUPPORTED_ISSUE:
        raise GrantError(f"approval artifact may only approve #{SUPPORTED_ISSUE}")
    nonce = str(data.get("approval_nonce", "")).strip()
    if len(nonce) < MIN_NONCE_CHARS:
        raise GrantError(
            f"approval artifact needs an approval_nonce of at least "
            f"{MIN_NONCE_CHARS} characters")
    digest = str(data.get("approved_body_sha256", "")).strip().lower()
    if not _is_hex64(digest):
        raise GrantError(
            "approval artifact must carry the approved issue revision "
            "(approved_body_sha256)")
    if not str(data.get("issued_at", "")).strip():
        raise GrantError("approval artifact must carry issued_at")
    return data


def validate_request(issue: Any, reason: Optional[str],
                     approval: Optional[Dict[str, Any]], operator: Optional[str], *,
                     supported_issue: int = SUPPORTED_ISSUE,
                     expected_owner: str = AUTHORIZED_OWNER,
                     allowed_operators: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """All explicit inputs, or an error.  No defaults, no guessing."""
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
    if not isinstance(approval, dict) or \
            str(approval.get("owner", "")).strip() != expected_owner:
        raise GrantError("a valid owner approval artifact is required")
    op = (operator or "").strip()
    if not op:
        raise GrantError("an operator login is required (--operator)")
    allowed = {str(o).strip() for o in (allowed_operators or []) if str(o).strip()}
    if not allowed:
        raise GrantError(
            f"no operator is authorized: set {OPERATORS_ENV} to the verified "
            "operator login(s)")
    if op not in allowed:
        raise GrantError(f"operator {op!r} is not authorized by {OPERATORS_ENV}")
    nonce = str(approval.get("approval_nonce", ""))
    return {
        "issue": number,
        "reason": text,
        "owner": expected_owner,
        "operator": op,
        "approval_id": approval_id(number, expected_owner, nonce),
        "approved_body_sha256": str(approval["approved_body_sha256"]).strip().lower(),
    }


# --------------------------------------------------------------------------
# scope check (network kept lazy)
# --------------------------------------------------------------------------
def default_scope_checker(issue: int, expected_body_sha256: Optional[str] = None,
                          token: Optional[str] = None) -> Tuple[bool, str]:
    """Confirm the live issue is open at the approved revision with the revised
    offline-only scope.

    Fail-closed: without a token, or on any API error, this refuses rather than
    assuming the issue is still eligible.
    """
    import issue_github as gh  # network import kept lazy

    if not token:
        return False, "no GitHub token -- cannot confirm the live scope"
    repo = os.environ.get(REPO_ENV) or DEFAULT_REPO
    try:
        data, _ = gh.request("GET", f"/repos/{repo}/issues/{int(issue)}", token)
    except Exception as exc:  # pragma: no cover - network failure path
        return False, f"could not read issue #{issue}: {type(exc).__name__}"
    if not isinstance(data, dict):
        return False, "malformed issue payload"
    if str(data.get("state")) != "open":
        return False, f"issue #{issue} is not open (state={data.get('state')})"
    body = data.get("body") or ""
    if expected_body_sha256:
        live = body_sha256(body)
        if live != str(expected_body_sha256).lower():
            return False, ("the live issue body no longer matches the approved "
                           "revision (it changed since the owner approved it)")
    return evaluate_scope(body)


class RetryGrant:
    """One-time, audited retry eligibility for a single issue."""

    def __init__(self, state_dir: Optional[str] = None, *,
                 state_path: Optional[str] = None,
                 scope_checker: Optional[ScopeChecker] = None,
                 supported_issue: int = SUPPORTED_ISSUE,
                 owner: str = AUTHORIZED_OWNER,
                 clock: Optional[Callable[[], float]] = None,
                 approval_path: Optional[str] = None,
                 allowed_operators: Optional[Sequence[str]] = None,
                 require_worker_profile: bool = True) -> None:
        env_state = os.environ.get(STATE_ENV)
        if state_path:
            self.state_path = os.path.abspath(state_path)
            self.state_dir = os.path.dirname(self.state_path)
        else:
            base = state_dir or env_state or lib.default_state_dir()
            self.state_dir = os.path.abspath(base)
            self.state_path = os.path.join(self.state_dir, "state.json")
        self.audit_path = os.path.join(self.state_dir, AUDIT_NAME)
        self.pending_path = os.path.join(self.state_dir, PENDING_NAME)
        self.backup_dir = os.path.join(self.state_dir, "backups", "retry-grant")
        self.approval_path = approval_path or os.path.join(
            self.state_dir, "approvals", APPROVAL_FILENAME)
        self.scope_checker = scope_checker or default_scope_checker
        self.supported_issue = int(supported_issue)
        self.owner = owner
        self._clock = clock or time.time
        self.require_worker_profile = bool(require_worker_profile)
        if allowed_operators is None:
            allowed_operators = [
                part.strip() for part in os.environ.get(OPERATORS_ENV, "").split(",")
                if part.strip()
            ]
        self.allowed_operators = list(allowed_operators)

    # -- worker-profile guard --------------------------------------------
    def worker_profile_reason(self) -> Tuple[bool, str]:
        norm = self.state_path.replace("\\", "/")
        if WORKER_PROFILE_MARKER.replace("\\", "/") in norm:
            return True, f"state_path is the worker-agent ledger: {self.state_path}"
        env_state = os.environ.get(STATE_ENV)
        if env_state and os.path.abspath(env_state) == self.state_dir:
            return True, f"state directory pinned by {STATE_ENV}: {self.state_dir}"
        return False, (
            f"state_path is not the worker-agent profile ledger "
            f"({self.state_path}); run with HERMES_HOME pointing at the "
            f"worker-agent profile or set {STATE_ENV} to its ops directory")

    def assert_worker_profile(self) -> None:
        if not self.require_worker_profile:
            return
        ok, detail = self.worker_profile_reason()
        if not ok:
            raise GrantError("refusing to touch a non-worker-agent ledger: " + detail)

    # -- io ---------------------------------------------------------------
    def _now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))

    def _read(self) -> Dict[str, Any]:
        """Strict, fail-closed read: a missing or malformed ledger is an error."""
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise GrantError(
                f"live worker ledger not found: {self.state_path} (fail-closed)")
        except (OSError, ValueError) as exc:
            raise GrantError(
                f"live worker ledger unreadable or malformed: "
                f"{type(exc).__name__} (fail-closed)")
        if not isinstance(data, dict):
            raise GrantError("live worker ledger is not a JSON object (fail-closed)")
        return data

    def _require_blocked(self, data: Dict[str, Any], key: str) -> Dict[str, Any]:
        record = (data.get("processed") or {}).get(key)
        if not isinstance(record, dict) or str(record.get("outcome")) != "blocked":
            raise GrantError(
                f"issue #{key} has no prior 'blocked' record in {self.state_path}; "
                "the idempotency entry cannot be lifted without it (fail-closed)")
        return record

    def _write_atomic(self, data: Dict[str, Any]) -> None:
        target_dir = os.path.dirname(self.state_path) or "."
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".retry-grant-",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
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
            fh.flush()
            os.fsync(fh.fileno())

    def _audit_best_effort(self, event: Dict[str, Any]) -> None:
        try:
            self._append_audit(event)
        except OSError:
            pass

    def _write_pending(self, payload: Dict[str, Any]) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self.pending_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.pending_path)

    def _clear_pending(self) -> None:
        try:
            os.unlink(self.pending_path)
        except OSError:
            pass

    def pending_marker(self) -> Optional[Dict[str, Any]]:
        """Return an un-cleared pending marker, if a previous run crashed."""
        try:
            with open(self.pending_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

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
        path = os.path.join(self.backup_dir,
                            f"issue-{self.supported_issue}-{stamp}.json")
        payload = {
            "prior_outcome": (data.get("processed") or {}).get(str(self.supported_issue)),
            "ledger": data,
            "captured_at": self._now(),
        }
        blob = json.dumps(payload, indent=2, sort_keys=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        return path, digest

    @staticmethod
    def verify_backup(path: str, digest: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = fh.read()
        except OSError:
            return False
        return hashlib.sha256(blob.encode("utf-8")).hexdigest() == digest

    def _restore(self, data: Dict[str, Any]) -> Tuple[bool, str]:
        """Best-effort restore of the exact prior ledger; report what happened."""
        try:
            self._write_atomic(data)
            after = self._read()
            same = (
                after.get("usage") == data.get("usage")
                and after.get("claims") == data.get("claims")
                and (after.get("processed") or {}) == (data.get("processed") or {})
                and after.get("version") == data.get("version")
            )
            return same, ("ledger restored to the prior state" if same
                          else "ledger restore could not be verified")
        except Exception as exc:  # noqa: BLE001
            return False, (f"ledger restore FAILED ({type(exc).__name__}); "
                           f"recover from {self.pending_path}")

    # -- the operation ----------------------------------------------------
    def _scope_check(self, req: Dict[str, Any]) -> Tuple[bool, str]:
        try:
            return self.scope_checker(req["issue"], req["approved_body_sha256"])
        except TypeError:  # a checker that only takes the issue number
            return self.scope_checker(req["issue"])  # type: ignore[misc]

    def plan(self, issue: Any, reason: Optional[str],
             approval: Optional[Dict[str, Any]],
             operator: Optional[str]) -> Dict[str, Any]:
        """Dry run: validate and describe, never write.

        Still fail-closed on the two things that must never be wrong: the target
        ledger must be the worker-agent ledger, and it must be readable.
        """
        self.assert_worker_profile()
        data = self._read()
        out: Dict[str, Any] = {
            "dry_run": True,
            "state_path": self.state_path,
            "audit_path": self.audit_path,
            "supported_issue": self.supported_issue,
            "pending_marker": self.pending_marker(),
        }
        try:
            req = validate_request(issue, reason, approval, operator,
                                   supported_issue=self.supported_issue,
                                   expected_owner=self.owner,
                                   allowed_operators=self.allowed_operators)
        except GrantError as exc:
            out.update({"request_ok": False, "request_error": str(exc)})
            return out
        prior = (data.get("processed") or {}).get(str(req["issue"]))
        out.update({
            "request_ok": True,
            "issue": req["issue"],
            "approval_id": req["approval_id"],
            "reason": req["reason"],
            "operator": req["operator"],
            "prior_outcome": prior,
            "eligible_prior_blocked": str((prior or {}).get("outcome")) == "blocked",
        })
        ok, detail = self._scope_check(req)
        out.update({"scope_ok": bool(ok), "scope_detail": detail})
        return out

    def grant(self, issue: Any, reason: Optional[str],
              approval: Optional[Dict[str, Any]],
              operator: Optional[str]) -> Dict[str, Any]:
        self.assert_worker_profile()
        if self.pending_marker() is not None:
            raise GrantError(
                "a retry-grant pending marker already exists "
                f"({self.pending_path}); resolve it before granting again")
        req = validate_request(issue, reason, approval, operator,
                               supported_issue=self.supported_issue,
                               expected_owner=self.owner,
                               allowed_operators=self.allowed_operators)
        ok, detail = self._scope_check(req)
        if not ok:
            raise GrantError(f"scope not confirmed: {detail}")

        key = str(req["issue"])
        with lib.InterProcessLock(os.path.join(self.state_dir, "worker.lock"),
                                  timeout=10.0):
            data = self._read()
            record = self._require_blocked(data, key)
            events = self.history()
            if any(e.get("event") == "granted"
                   and e.get("approval_id") == req["approval_id"] for e in events):
                raise GrantError("this approval was already used for an earlier grant")
            if (data.get("retry_grants") or {}).get(key, {}).get("status") == "available":
                raise GrantError(f"a retry grant for #{key} is already available")

            backup_path, digest = self._backup(data)
            if not self.verify_backup(backup_path, digest):
                raise GrantError("backup verification failed -- nothing was changed")
            self._append_audit({"event": "backup", "approval_id": req["approval_id"],
                                "backup": backup_path, "sha256": digest,
                                "prior_outcome": record.get("outcome")})

            before = {
                "version": data.get("version"),
                "usage": copy.deepcopy(data.get("usage")),
                "claims": copy.deepcopy(data.get("claims")),
                "protected_keys": sorted(set(data) - {"processed", "retry_grants"}),
            }
            new = copy.deepcopy(data)
            new.setdefault("retry_grants", {})
            new["retry_grants"][key] = {
                "status": "available",
                "approval_id": req["approval_id"],
                "reason": req["reason"],
                "owner": req["owner"],
                "operator": req["operator"],
                "granted_at": self._now(),
                "prior_outcome": record.get("outcome"),
                "prior_pr": record.get("pr"),
                "backup": backup_path,
                "backup_sha256": digest,
            }
            new.setdefault("processed", {}).pop(key, None)

            # Durable marker BEFORE the change: a crash mid-change is recoverable
            # instead of leaving an unexplained ledger.
            self._write_pending({
                "approval_id": req["approval_id"], "state_path": self.state_path,
                "backup": backup_path, "sha256": digest, "started_at": self._now(),
            })
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
                # The one-time audit MUST land before the grant is considered real.
                self._append_audit({
                    "event": "granted", "approval_id": req["approval_id"],
                    "reason": req["reason"], "owner": req["owner"],
                    "operator": req["operator"],
                    "prior_outcome": record.get("outcome"),
                    "backup": backup_path, "sha256": digest,
                })
            except Exception as exc:  # noqa: BLE001 - roll back to the prior ledger
                restored, note = self._restore(data)
                self._audit_best_effort({
                    "event": "failed", "approval_id": req["approval_id"],
                    "error": f"{type(exc).__name__}: {exc}",
                    "rolled_back": restored, "recovery": note,
                })
                if restored:
                    self._clear_pending()
                raise GrantError(f"apply failed and was rolled back ({note}): {exc}")

            self._consume_approval(req["approval_id"])
            self._clear_pending()
            return {"granted": True, "issue": req["issue"],
                    "approval_id": req["approval_id"], "operator": req["operator"],
                    "backup": backup_path, "backup_sha256": digest,
                    "prior_outcome": record.get("outcome"),
                    "state_path": self.state_path}

    def _consume_approval(self, aid: str) -> Optional[str]:
        """Rename the approval artifact so a second run cannot reuse it."""
        if not self.approval_path or not os.path.isfile(self.approval_path):
            return None
        used = f"{self.approval_path}.used-{aid}"
        try:
            os.replace(self.approval_path, used)
            return used
        except OSError:
            return None

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


def _token_scoped_checker() -> ScopeChecker:
    token = _read_token()

    def check(issue: int, expected_body_sha256: Optional[str] = None):
        return default_scope_checker(issue, expected_body_sha256, token=token)

    return check


def write_approval(path: str, *, owner: str = AUTHORIZED_OWNER,
                   issue: int = SUPPORTED_ISSUE, nonce: str,
                   approved_body_sha256: str,
                   issued_at: Optional[str] = None) -> str:
    """Create a owner approval artifact (used by docs/tests and the owner)."""
    payload = {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "owner": owner,
        "issue": int(issue),
        "approval_nonce": nonce,
        "approved_body_sha256": approved_body_sha256,
        "issued_at": issued_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.chmod(path, 0o600)
    return path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("command", choices=["preview", "grant", "history", "verify-backup"])
    p.add_argument("--issue", default=None, help="explicit issue number (19 only)")
    p.add_argument("--reason", default=None)
    p.add_argument("--operator", default=None,
                   help=f"operator login, must be listed in {OPERATORS_ENV}")
    p.add_argument("--approval-file", default=None,
                   help="path to the owner approval artifact (mode 600)")
    p.add_argument("--state-dir", default=None)
    p.add_argument("--backup", default=None, help="verify-backup: backup file")
    p.add_argument("--sha256", default=None, help="verify-backup: expected digest")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        grant = RetryGrant(args.state_dir, approval_path=args.approval_file)
        if args.command == "preview":
            approval = None
            try:
                approval = load_approval(args.approval_file or grant.approval_path)
            except GrantError:
                approval = None
            out = grant.plan(args.issue, args.reason, approval, args.operator)
        elif args.command == "grant":
            approval = load_approval(args.approval_file or grant.approval_path)
            grant.scope_checker = _token_scoped_checker()
            out = grant.grant(args.issue, args.reason, approval, args.operator)
        elif args.command == "history":
            out = {"issue": grant.supported_issue,
                   "pending_marker": grant.pending_marker(),
                   "events": grant.history()}
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


if __name__ == "__main__":
    sys.exit(main())
