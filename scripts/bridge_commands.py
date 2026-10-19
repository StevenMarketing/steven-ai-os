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
        return lq.evaluate_live_queue(validated=validated, trusted=trusted, activation=activation,
                                      issue=issue, processed=processed, ledger=ledger,
                                      budget_guard=budget_guard, open_pr=open_pr, now=now)

    return gate


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

    validated = validate_command(envelope, now=now, ledger=ledger, budget_guard=budget_guard)
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
    comments, _ = request("GET", "/repos/%s/issues/%d/comments?per_page=%d"
                          % (repo, int(issue_number), limit), token)
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
    processed = _load_json(PROCESSED_PATH)

    executor = CommandExecutor(status_document=live_status_document,
                              issue_inspector=issue_inspector_factory(request, token, args.repo),
                              live_gate=live_gate_factory(request, token, args.repo,
                                                          processed=processed, ledger=ledger,
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
                                         budget_guard=budget_guard)
            results[0]["validated"] = {k: validated.get(k) for k in ("accepted", "code", "replay")}
            if validated.get("accepted"):
                results[0]["execution"] = executor.execute(
                    validated, trusted,
                    dry_run_override=(False if args.no_dry_run else None))
    elif args.scan and args.issue:
        results = scan(request, token, args.repo, args.issue, executor=executor, ledger=ledger,
                       processed=processed, dry_run_override=(False if args.no_dry_run else None),
                       budget_guard=budget_guard, post=not args.no_post)
    else:
        results = [{"error": "nothing to do: pass --issue N --scan or --command JSON"}]

    if not args.no_post:
        _save_json(PROCESSED_PATH, processed)
    print(json.dumps(results, indent=2, sort_keys=True) if args.json
          else "bridge: %s" % json.dumps(results, sort_keys=True)[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
