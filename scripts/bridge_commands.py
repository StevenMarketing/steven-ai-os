#!/usr/bin/env python3
"""Command bridge runner: read owner-issued commands from GitHub and answer them there.

Reuses, in this order:
  * ``gbp.bridge.identity``    - the actor is verified from GitHub API metadata;
  * ``gbp.bridge.commands``    - envelope validation, idempotency, TTL, replay guard;
  * ``gbp.bridge.executor``    - explicit state chain + GitHub-visible ack/result;
  * ``gbp.bridge.publisher``   - the redacted live status document for ``status``;
  * ``worker_gate``            - the existing budget guard, as the envelope's budget hook.

A comment that merely *claims* to be the owner authorizes nothing: no verified context,
no execution. Every processed comment is recorded so a re-scan cannot run it twice.

Usage:
    PYTHONPATH=src python3 scripts/bridge_commands.py --issue 49 --scan
    PYTHONPATH=src python3 scripts/bridge_commands.py --command '{"operation":"status",...}'
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gbp.bridge.commands import CommandLedger, parse_command_body, validate_command  # noqa: E402
from gbp.bridge.executor import CommandExecutor  # noqa: E402
from gbp.bridge.identity import fetch_trusted_context, verify_identity  # noqa: E402

DEFAULT_REPO = "StevenMarketing/steven-ai-os"
AUTHORIZED_ACTORS = ("StevenMarketing",)
PROCESSED_PATH = os.environ.get("STEVEN_COMMAND_PROCESSED", "/opt/data/ops/bridge/processed-comments.json")
LEDGER_PATH = os.environ.get("STEVEN_COMMAND_LEDGER", "/opt/data/ops/bridge/command-ledger.json")
ACTIVATION_PATH = os.environ.get("STEVEN_LIVE_QUEUE_ACTIVATION",
                                 "/opt/data/ops/bridge/live-queue-activation.json")


def _load_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path: str, data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


def live_status_document() -> Dict[str, Any]:
    """The redacted status the publisher already produces, built from real sources."""
    import bridge_status_publish as publisher_cli

    return publisher_cli.build_document()


def issue_inspector_factory(request: Callable[..., Any], token: str, repo: str) -> Callable[[int], Dict[str, Any]]:
    """Dry-run inspection of a candidate issue: would the worker accept it? No mutation."""
    import issue_worker_lib

    def inspect(number: int) -> Dict[str, Any]:
        from gbp.bridge.intake import preflight

        payload, _ = request("GET", "/repos/%s/issues/%d" % (repo, number), token)
        body = payload.get("body") or ""
        labels = [l.get("name") for l in payload.get("labels") or []]
        verdict = preflight(body, patterns=list(getattr(issue_worker_lib, "INJECTION_PATTERNS", ())))
        return {"state": payload.get("state"), "labels": labels,
                "eligible_label": "hermes-ready" in labels, "usable": verdict["usable"],
                "missing_headings": verdict["missing_headings"], "tripwire": verdict["tripwire"],
                "would_queue": bool(payload.get("state") == "open"
                                    and "hermes-ready" in labels and verdict["usable"])}

    return inspect


def find_open_pr_for_issue(request: Callable[..., Any], token: str, repo: str, number: int) -> bool:
    """True when an open PR already references the issue (bounded scan, fail closed).

    A lookup failure returns ``True`` so the caller refuses rather than risking a
    duplicate enqueue.
    """
    try:
        prs, _ = request("GET", "/repos/%s/pulls?state=open&per_page=100" % repo, token)
    except Exception:
        return True
    needle = "#%d" % int(number)
    for pr in prs or []:
        head = str(((pr.get("head") or {}).get("ref")) or "")
        haystack = "%s %s %s" % (pr.get("title") or "", pr.get("body") or "", head)
        if needle in haystack or ("issue-%d" % int(number)) in head:
            return True
    return False


#: Activation record in force for this process; main() sets it from --activation.
_ACTIVATION_IN_USE: Dict[str, Any] = {"path": None}


class LedgerPersistenceError(RuntimeError):
    """Raised when the ledger cannot be persisted: the caller must fail closed."""


class LedgerSnapshot:
    """Replay view for the activation gate: entries that existed BEFORE this run.

    The envelope validator records an accepted command in the live ledger. If the gate
    then consulted that same ledger it would see the key the validator had just written
    and refuse every command as ``replay_duplicate`` -- the production defect that made a
    live ``queue_issue`` impossible. This snapshot is seeded from the persisted ledger at
    process start and is updated only *after* a command completes, so:
      * the current command is never refused by its own bookkeeping;
      * a genuine replay (same idempotency key in an earlier run, or a second command in
        this run) is still refused, including after a restart.
    """

    CLAIMS_DIR = os.environ.get("STEVEN_BRIDGE_CLAIMS_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(LEDGER_PATH)), "ledger-claims")

    def __init__(self, keys: Optional[List[str]] = None,
                 requests: Optional[List[str]] = None,
                 claims_dir: Optional[str] = None) -> None:
        self._keys = set(keys or ())
        self._requests = set(requests or ())
        self.claims_dir = claims_dir or (os.environ.get("STEVEN_BRIDGE_CLAIMS_DIR")
                                         or self.CLAIMS_DIR)

    def merge(self, keys: Any = None, requests: Any = None) -> None:
        """Union in identifiers discovered in a concurrently written ledger file."""
        self._keys |= set(keys or ())
        self._requests |= set(requests or ())

    def save(self, path: Optional[str] = None) -> None:
        """Merge this snapshot into ``path`` under an exclusive lock, atomically.

        Two runners persisting at the same time used to be a last-writer-wins overwrite,
        which silently dropped the other process's identifiers and made them replayable
        after a restart. The read-modify-write below is serialised with ``flock`` and
        published with an atomic rename, so no recorded key or request id is ever lost.
        """
        import fcntl
        target = path or LEDGER_PATH
        try:
            os.makedirs(os.path.dirname(os.path.abspath(target)), mode=0o700, exist_ok=True)
        except OSError as exc:
            raise LedgerPersistenceError("cannot create the ledger directory for %s: %s"
                                         % (target, exc)) from exc
        try:
            with open(target + ".lock", "a+", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    stored = _load_json(target) or {}
                    self.merge(stored.get("keys"), stored.get("requests"))
                    temporary = "%s.tmp-%d" % (target, os.getpid())
                    with open(temporary, "w", encoding="utf-8") as stream:
                        json.dump(self.to_dict(), stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError as exc:
            # Never swallow this: a silently failed save would make a recorded command
            # replayable after a restart. The error is visible and the caller fails closed.
            raise LedgerPersistenceError(
                "ledger persistence FAILED for %s: %s" % (target, exc)) from exc

    @classmethod
    def from_file(cls, path: str) -> "LedgerSnapshot":
        data = _load_json(path)
        return cls(data.get("keys"), data.get("requests"))

    @staticmethod
    def _rollback(paths: List[str]) -> None:
        """Unwind a partially created claim so no orphaned marker can wedge a command.

        Without this, a failure on the request id after the idempotency key was created
        left a claim behind that refused the legitimate command forever.
        """
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"

    def pending_state(self, value: Any) -> Optional[str]:
        """The recorded state of a claimed identifier, or None when it was never claimed.

        An unreadable marker reports ``unknown`` -- never "fine": we cannot tell what
        happened, so the caller must fail closed and ask for manual review. Markers are
        never deleted here.
        """
        if not isinstance(value, str) or not value:
            return None
        path = self._claim_path(value)
        try:
            with open(path, "r", encoding="utf-8") as stream:
                return str(json.load(stream).get("state") or self.UNKNOWN)
        except FileNotFoundError:
            return None
        except Exception:
            return self.UNKNOWN

    def settle(self, idempotency_key: Any, request_id: Any, state: str,
               reason: str = "") -> None:
        """Atomically record the outcome for this command in every one of its markers.

        The identifiers of one command are stored in both markers, so settling one settles
        its sibling too -- otherwise a request_id marker would stay pending forever and the
        gate would keep refusing a command whose mutation is already confirmed. No file lock
        is taken, so this can never deadlock against a ``save()`` holding the ledger lock.
        """
        pending = [value for value in (idempotency_key, request_id)
                   if isinstance(value, str) and value]
        seen = set()
        for value in list(pending):
            try:
                with open(self._claim_path(value), "r", encoding="utf-8") as stream:
                    marker = json.load(stream)
                for sibling in (marker.get("idempotency_key"), marker.get("request_id")):
                    if isinstance(sibling, str) and sibling and sibling not in seen:
                        seen.add(sibling)
                        pending.append(sibling)
            except Exception:
                continue
            seen.add(value)
        for value in pending:
            self._write_state(value, state, reason)
        self.record(idempotency_key, request_id)

    def _write_state(self, value: str, state: str, reason: str) -> None:
        """Atomically rewrite one marker (tmp + fsync + rename); never deletes it."""
        path = self._claim_path(value)
        if not os.path.exists(path):
            return
        temporary = "%s.state-%d-%d" % (path, os.getpid(), int(time.time() * 1e6))
        try:
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump({"state": state, "value": value, "reason": reason,
                           "settled_at": int(time.time())}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            # A silently failed marker write would leave the state stale while the caller
            # believes the outcome was recorded. Fail visibly and closed instead.
            raise LedgerPersistenceError(
                "marker state write FAILED for %s: %s" % (path, exc)) from exc

    def reconcile(self, value: Any, *, label_present: Optional[bool],
                  api_error: bool = False, authoritative_failure: bool = False,
                  now: Optional[float] = None) -> Dict[str, Any]:
        """Decide the outcome of an interrupted command from evidence -- conservatively.

        * GitHub unreachable                 -> `unknown`, manual review (never assumed failed);
        * label present                      -> `confirmed` (the intended label state holds);
          attribution to this exact request is recorded as uncertain, because a label can
          also come from another actor;
        * label absent, no authoritative proof -> `unknown`, manual review. An elapsed TTL
          is NOT evidence that nothing happened, so it never decides `failed` by itself;
        * authoritative_failure=True          -> `failed` (only when a caller holds real proof
          that no mutation was applied, e.g. the API answered and refused before applying).
        """
        moment = time.time() if now is None else now
        if authoritative_failure and not label_present:
            self.settle(value, None, self.FAILED, "authoritative_no_mutation")
            return {"state": self.FAILED, "reason": "authoritative_no_mutation"}
        if api_error:
            self.settle(value, None, self.UNKNOWN, "github_unavailable")
            return {"state": self.UNKNOWN, "reason": "github_unavailable",
                    "requires_manual_review": True}
        if label_present is True:
            self.settle(value, None, self.CONFIRMED, "label_present_attribution_uncertain")
            return {"state": self.CONFIRMED, "reason": "label_present",
                    "attribution_uncertain": True}
        self.settle(value, None, self.UNKNOWN, "no_label_no_authoritative_evidence")
        return {"state": self.UNKNOWN, "reason": "no_label_no_authoritative_evidence",
                "requires_manual_review": True, "age_seconds": moment}

    def record(self, idempotency_key: Any, request_id: Any) -> None:
        for value, target in ((idempotency_key, self._keys), (request_id, self._requests)):
            if isinstance(value, str) and value:
                target.add(value)

    def to_dict(self) -> Dict[str, Any]:
        return {"keys": sorted(self._keys), "requests": sorted(self._requests)}

    def seen(self, key: Any):
        if not isinstance(key, str):
            return None
        if key in self._keys or os.path.exists(self._claim_path(key)):
            return key
        return None

    def seen_request(self, request_id: Any):
        if not isinstance(request_id, str):
            return None
        if request_id in self._requests or os.path.exists(self._claim_path(request_id)):
            return request_id
        return None

    def _claim_path(self, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
        return os.path.join(self.claims_dir, digest + ".claim")

    def claim(self, idempotency_key: Any = None, request_id: Any = None,
              issue: Optional[int] = None) -> bool:
        """Atomically reserve the identifiers; False means someone else already holds them.

        ``O_CREAT|O_EXCL`` makes the reservation atomic across processes, so two runners
        that race on the same command cannot both mutate. Any error fails closed.
        """
        try:
            os.makedirs(self.claims_dir, mode=0o700, exist_ok=True)
        except OSError:
            return False
        created: List[str] = []
        moment = int(time.time())
        for value in (idempotency_key, request_id):
            if not (isinstance(value, str) and value):
                continue
            # Already held -- in memory or on disk, by this process or another -- is a
            # conflict, never a no-op, so nothing is admitted twice.
            if (value in self._keys or value in self._requests
                    or os.path.exists(self._claim_path(value))):
                self._rollback(created)
                return False
            path = self._claim_path(value)
            try:
                handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except OSError:            # FileExistsError included: a racing process won
                self._rollback(created)
                return False
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                # The marker itself is the durable state: pending until settled.
                json.dump({"state": self.PENDING, "value": value,
                           "idempotency_key": idempotency_key,
                           "request_id": request_id,
                           "issue": issue,
                           "created_at": moment}, stream)
            created.append(path)
        self.record(idempotency_key, request_id)
        return True


_PRE_VALIDATION_LEDGER = LedgerSnapshot()


def activation_allows_live_queue(activation_path: Optional[str] = None) -> bool:
    """True only when the owner activation record for live queue_issue is valid.

    The validator needs this to let a ``dry_run: false`` queue_issue through to the
    gate. Everything else stays refused, and the gate re-checks the whole envelope.
    """
    try:
        import gbp.bridge.live_queue as lq

        record = lq.load_activation(activation_path if activation_path else ACTIVATION_PATH)
        return bool(lq.activation_status(record, now=time.time()).get("enabled"))
    except Exception:
        return False


def live_gate_factory(
    request: Callable[..., Any],
    token: str,
    repo: str,
    *,
    processed: Dict[str, Any],
    ledger: CommandLedger,
    budget_guard: Optional[Callable[[str, Optional[int]], Any]] = None,
    activation_path: Optional[str] = None,
) -> Callable[[Dict[str, Any], Any], Dict[str, Any]]:
    """Build the narrow opt-in gate for a LIVE ``queue_issue`` (Issue #51).

    Default OFF: with no valid activation record the gate refuses before any
    network call, so production behaviour is unchanged until the owner installs
    the record. Everything is delegated to :mod:`gbp.bridge.live_queue`.
    """
    from gbp.bridge import live_mutation as lm
    from gbp.bridge import live_queue as lq

    def gate(validated: Dict[str, Any], trusted: Any) -> Dict[str, Any]:
        now = time.time()
        activation = lq.load_activation(activation_path if activation_path else ACTIVATION_PATH)
        status = lq.activation_status(activation, now=now)
        if not status["enabled"]:
            return {"admitted": False, "operation": "queue_issue",
                    "request_id": validated.get("request_id") if isinstance(validated, dict) else None,
                    "issue": validated.get("issue") if isinstance(validated, dict) else None,
                    "code": "live_queue_disabled",
                    "reasons": ["live_queue_disabled: %s" % status["reason"]],
                    "links": {}, "mutated": False}
        number = validated.get("issue")
        try:
            payload, _ = request("GET", "/repos/%s/issues/%d" % (repo, int(number)), token)
        except Exception:
            return {"admitted": False, "operation": "queue_issue", "request_id": validated.get("request_id"),
                    "issue": number, "code": "target_lookup_failed",
                    "reasons": ["target_lookup_failed"], "links": {}, "mutated": False}
        issue = {"number": payload.get("number"), "state": payload.get("state"),
                 "repository": repo, "labels": payload.get("labels"), "body": payload.get("body")}
        open_pr = find_open_pr_for_issue(request, token, repo, int(number))
        receipt = lq.evaluate_live_queue(validated=validated, trusted=trusted, activation=activation,
                                         issue=issue, processed=processed, ledger=ledger,
                                         budget_guard=budget_guard, open_pr=open_pr, now=now)
        # Issue #57: an admission is not an enqueue. Perform the ONE approved
        # GitHub mutation (add ``hermes-ready``) and let the receipt's ``mutated``
        # reflect the confirmed API result -- never the mere admission decision.
        if receipt.get("admitted"):
            lm.apply_mutation_to_receipt(receipt, request=request, token=token, repo=repo,
                                         number=int(number),
                                         existing_labels=lm.label_names(issue.get("labels")))
        return receipt

    return gate



def _settle_ledger_for_execution(ledger: "LedgerSnapshot", envelope: Dict[str, Any],
                                 execution: Dict[str, Any]) -> str:
    """Record the durable outcome from STRUCTURED evidence, never from error text.

    Audit of every path that can yield FAILED (a claim about the real world):
      * ``outcome="failed"`` stated by the caller        -> only a caller holding proof may say so;
      * ``mutation.attempted is False`` with a mutation block present -> the mutation stage ran
        and decided not to write: authoritative no-mutation;
      * the gate refused before the mutation stage (``receipt.admitted is False``) -> no POST
        could have happened: authoritative no-mutation.
    Anything else -- including a receipt with no mutation evidence at all -- is UNKNOWN,
    because we cannot prove the API did not apply the mutation.
    """
    result = execution.get("result") or {}
    receipt = result.get("receipt") or {}
    mutation = receipt.get("mutation") or result.get("mutation") or {}
    stated = str(result.get("outcome") or mutation.get("outcome") or "").lower()
    if stated == ledger.CONFIRMED:
        state, reason = ledger.CONFIRMED, "stated_confirmed"
    elif stated == ledger.FAILED:
        state, reason = ledger.FAILED, "stated_failed_by_caller_with_proof"
    elif stated == ledger.UNKNOWN:
        state, reason = ledger.UNKNOWN, "stated_unknown"
    elif result.get("mutated") or mutation.get("mutated"):
        state, reason = ledger.CONFIRMED, "mutation_confirmed"
    elif mutation.get("attempted") is False and mutation:
        state, reason = ledger.FAILED, "mutation_never_attempted"
    elif receipt.get("admitted") is False:
        state, reason = ledger.FAILED, "gate_refused_before_mutation"
    else:
        state, reason = ledger.UNKNOWN, "no_authoritative_mutation_evidence"
    ledger.settle(envelope.get("idempotency_key"), envelope.get("request_id"), state, reason)
    return state

def reconcile_pending_markers(request: Callable[..., Any], token: str, repo: str,
                              ledger: "LedgerSnapshot", *, limit: int = 25) -> Dict[str, Any]:
    """Reconcile interrupted commands against GitHub state through the real runtime path.

    Strict evidence rules: a missing label or an elapsed TTL never yields ``failed``; GitHub
    being unreachable yields ``unknown`` plus manual review. Markers are never deleted.
    """
    summary = {"checked": 0, "confirmed": 0, "failed": 0, "unreadable": 0, "review": []}
    try:
        names = sorted(os.listdir(ledger.claims_dir))[:limit]
    except OSError:
        return summary
    for name in names:
        if not name.endswith(".claim"):
            continue
        path = os.path.join(ledger.claims_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as stream:
                marker = json.load(stream)
        except Exception as exc:
            # Never a silent skip: an unreadable marker is precisely what a human must see.
            summary["unreadable"] += 1
            summary["review"].append({"marker": name, "reason": "marker_unreadable",
                                      "detail": type(exc).__name__})
            continue
        if str(marker.get("state") or "") not in ("pending", "unknown"):
            continue
        summary["checked"] += 1
        number = marker.get("issue")
        label_present, api_error = None, False
        if isinstance(number, int):
            try:
                payload, _ = request("GET", "/repos/%s/issues/%d" % (repo, number), token)
                on_issue = [label.get("name") for label in (payload.get("labels") or [])]
                label_present = "hermes-ready" in on_issue
            except Exception:
                api_error = True
        decision = ledger.reconcile(str(marker.get("value") or ""), label_present=label_present,
                                    api_error=api_error or not isinstance(number, int))
        if decision["state"] in ("confirmed", "failed"):
            summary[decision["state"]] += 1
        if decision.get("requires_manual_review"):
            summary["review"].append({"value": marker.get("value"), "reason": decision.get("reason")})
    return summary



def process_comment(
    request: Callable[..., Any],
    token: str,
    repo: str,
    comment: Dict[str, Any],
    *,
    executor: CommandExecutor,
    ledger: CommandLedger,
    processed: Dict[str, Any],
    now: Optional[float] = None,
    dry_run_override: Optional[bool] = None,
    budget_guard: Optional[Callable[[str, Optional[int]], Any]] = None,
    post: bool = True,
) -> Dict[str, Any]:
    """Verify, validate, execute and answer one comment. Never raises."""
    now = float(now if now is not None else time.time())
    cid = str(comment.get("id"))
    out: Dict[str, Any] = {"comment": cid, "action": "none"}
    if cid in processed:
        out["action"] = "already-processed"
        return out
    parsed = parse_command_body(comment.get("body"))
    if parsed.get("errors") or not parsed.get("operation"):
        return out  # prose only (or an unparsable fence): nothing to do
    # parse_command_body carries its own parse diagnostics under "errors"; the envelope
    # schema has no such field, so strip it before validation (this exact mismatch made
    # every command be rejected as unknown_field: errors until the end-to-end test).
    envelope = {k: v for k, v in parsed.items() if k != "errors"}

    trusted = fetch_trusted_context(request, repository=repo, source_kind="issue_comment",
                                    source_id=int(cid), token=token)
    ok, code, detail = verify_identity(envelope, trusted, allowlisted_logins=AUTHORIZED_ACTORS)
    out.update(actor=trusted.author_login, verified=ok, code=code)
    if not ok:
        out["action"] = "rejected"
        out["detail"] = detail
        if post:
            request("POST", "/repos/%s/issues/%d/comments" % (repo, comment["_issue"]), token,
                    body={"body": "### Command rejected — `queued -> rejected`\n\n"
                                  "| field | value |\n|---|---|\n| comment | `%s` |\n"
                                  "| reason | `%s` |\n\n_%s_" % (cid, code, detail)})
        processed[cid] = {"state": "rejected", "code": code, "at": now}
        return out

    validated = validate_command(envelope, now=now, ledger=ledger, budget_guard=budget_guard,
                                     live_queue_allowed=activation_allows_live_queue(_ACTIVATION_IN_USE["path"]))
    out["validated"] = {k: validated.get(k) for k in ("accepted", "replay", "code", "operation")}
    if not validated.get("accepted"):
        out["action"] = "rejected"
        if post:
            request("POST", "/repos/%s/issues/%d/comments" % (repo, comment["_issue"]), token,
                    body={"body": "### Command rejected — `queued -> rejected`\n\n"
                                  "| field | value |\n|---|---|\n| comment | `%s` |\n"
                                  "| code | `%s` |\n| replay | `%s` |\n"
                                  "| errors | `%s` |\n\n"
                                  "_Envelope schema: %s_"
                                  % (cid, validated.get("code"), validated.get("replay"),
                                     "; ".join(str(e) for e in (validated.get("errors") or [])),
                                     "required request_id, idempotency_key, operation, actor, "
                                     "origin, created_at(epoch), ttl_seconds; optional issue, "
                                     "dry_run, note")})
        processed[cid] = {"state": "rejected", "code": validated.get("code"), "at": now}
        return out

    if post:
        request("POST", "/repos/%s/issues/%d/comments" % (repo, comment["_issue"]), token,
                body={"body": executor.ack_text(validated, trusted)})
    execution = executor.execute(validated, trusted, dry_run_override=dry_run_override)
    if post:
        request("POST", "/repos/%s/issues/%d/comments" % (repo, comment["_issue"]), token,
                body={"body": executor.result_text(execution["state"], validated, execution["result"])})
    ledger.record(envelope, validated.get("idempotency_key") or envelope.get("idempotency_key"), now=now)
    # Only now does the gate's snapshot learn this key: the current command has finished,
    # so a later command with the same idempotency key is a genuine replay and is refused
    # (this is what makes duplicate rejection survive a restart, since the snapshot is persisted).
    _settle_ledger_for_execution(_PRE_VALIDATION_LEDGER, envelope, execution)
    processed[cid] = {"state": execution["state"], "request_id": validated.get("request_id"),
                      "at": now}
    out.update(action="executed", execution=execution)
    return out


def scan(
    request: Callable[..., Any],
    token: str,
    repo: str,
    issue_number: int,
    *,
    executor: CommandExecutor,
    ledger: CommandLedger,
    processed: Dict[str, Any],
    now: Optional[float] = None,
    dry_run_override: Optional[bool] = None,
    budget_guard: Optional[Callable[[str, Optional[int]], Any]] = None,
    post: bool = True,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("comment page size must be between 1 and 100")
    # Collect every page before executing any command. A failed or truncated
    # read must never look like a successful empty inbox scan.
    comments = []
    for page in range(1, 101):
        path = "/repos/%s/issues/%d/comments?per_page=%d" % (repo, int(issue_number), limit)
        if page > 1:
            path += "&page=%d" % page
        batch, _ = request("GET", path, token)
        if not isinstance(batch, list):
            raise ValueError("comment page must be a list")
        comments.extend(batch)
        if len(batch) < limit:
            break
    else:
        raise RuntimeError("comment pagination limit reached; refusing partial scan")
    results = []
    for comment in comments or []:
        comment["_issue"] = int(issue_number)
        results.append(process_comment(request, token, repo, comment, executor=executor,
                                       ledger=ledger, processed=processed, now=now,
                                       dry_run_override=dry_run_override,
                                       budget_guard=budget_guard, post=post))
    return results


def main(argv=None, request: Optional[Callable[..., Any]] = None,
         token: Optional[str] = None) -> int:
    ap = argparse.ArgumentParser(description="Two-way GitHub command bridge runner")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--issue", type=int, default=0, help="command inbox issue number")
    ap.add_argument("--scan", action="store_true", help="process pending commands")
    ap.add_argument("--command", default="", help="local envelope JSON for an end-to-end test")
    ap.add_argument("--no-post", action="store_true", help="do not write to GitHub")
    ap.add_argument("--no-dry-run", action="store_true", help="force dry_run=false (safety test)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--activation", default="", help="path to the live queue_issue activation record (default OFF)")
    args = ap.parse_args(argv)

    _ACTIVATION_IN_USE["path"] = args.activation or None

    import issue_github

    if request is None:
        request = issue_github.request
    if token is None:
        token = os.environ.get("GITHUB_TOKEN") or ""
        if not token:
            import issue_poller

            token = issue_poller.read_token("/opt/data/.env")

    def budget_guard(operation: str, issue=None):
        """Existing budget guard exposed as the envelope hook; never raises outward."""
        try:
            import worker_gate

            return worker_gate.Gate().can_start(str(issue or 0), 0.05)
        except Exception as exc:
            return {"allowed": True, "reason": "guard unavailable: %s" % type(exc).__name__}

    ledger = CommandLedger()
    # Seeded before any validation: the gate must not see keys the validator writes.
    global _PRE_VALIDATION_LEDGER
    _PRE_VALIDATION_LEDGER = LedgerSnapshot.from_file(LEDGER_PATH)
    # Reconcile interrupted commands once per run through the real path. A failure here is
    # reported and never blocks the run; markers are preserved for manual review.
    try:
        _reconciled = reconcile_pending_markers(request, token, args.repo, _PRE_VALIDATION_LEDGER)
        if _reconciled.get("review"):
            print("MANUAL REVIEW REQUIRED: %s" % json.dumps(_reconciled["review"]), file=sys.stderr)
    except Exception as _exc:
        print("reconcile pass failed (markers kept): %s" % _exc, file=sys.stderr)
    processed = _load_json(PROCESSED_PATH)

    executor = CommandExecutor(status_document=live_status_document,
                              issue_inspector=issue_inspector_factory(request, token, args.repo),
                              live_gate=live_gate_factory(request, token, args.repo,
                                                          processed=processed,
                                                          ledger=_PRE_VALIDATION_LEDGER,
                                                          budget_guard=budget_guard,
                                                          activation_path=args.activation or None))

    if args.command:
        envelope = {k: v for k, v in json.loads(args.command).items() if k != "errors"}
        comment = {"id": int(time.time()), "body": "```command\n%s\n```" % json.dumps(envelope),
                   "_issue": args.issue or 0}
        trusted = fetch_trusted_context(request, repository=args.repo, source_kind="issue_comment",
                                        source_id=1, token=token)
        ok, code, detail = verify_identity(envelope, trusted, allowlisted_logins=AUTHORIZED_ACTORS)
        results = [{"local": True, "verified": ok, "code": code, "detail": detail,
                    "trusted": getattr(trusted, "author_login", "")}]
        if ok:
            validated = validate_command(envelope, now=time.time(), ledger=ledger,
                                         live_queue_allowed=activation_allows_live_queue(_ACTIVATION_IN_USE["path"]),
                                         budget_guard=budget_guard)
            results[0]["validated"] = {k: validated.get(k) for k in ("accepted", "code", "replay")}
            if validated.get("accepted"):
                results[0]["execution"] = executor.execute(
                    validated, trusted,
                    dry_run_override=(False if args.no_dry_run else None))
                # A real (non dry-run) command must record its durable outcome here too:
                # the one-shot path bypasses process_comment, which is where the scan path
                # settles. Without this the marker stays pending and only the reconcile
                # pass resolves it, which weakens the marker store as an audit trail.
                if not validated.get("dry_run"):
                    _settle_ledger_for_execution(
                        _PRE_VALIDATION_LEDGER, envelope, results[0]["execution"])
    elif args.scan and args.issue:
        results = scan(request, token, args.repo, args.issue, executor=executor, ledger=ledger,
                       processed=processed, dry_run_override=(False if args.no_dry_run else None),
                       budget_guard=budget_guard, post=not args.no_post)
    else:
        results = [{"error": "nothing to do: pass --issue N --scan or --command JSON"}]

    if not args.no_post:
        _save_json(PROCESSED_PATH, processed)
        try:
            _PRE_VALIDATION_LEDGER.save(LEDGER_PATH)
        except LedgerPersistenceError as exc:
            print("FAIL-CLOSED: %s" % exc, file=sys.stderr)
            raise SystemExit(3)
    print(json.dumps(results, indent=2, sort_keys=True) if args.json
          else "bridge: %s" % json.dumps(results, sort_keys=True)[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
