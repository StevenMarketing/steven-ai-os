"""Narrow, explicit opt-in gate for a **live** ``queue_issue`` (Issue #51).

Phase 1 (see ``commands.py`` / ``executor.py``) accepts ``queue_issue`` **only** as a
dry run: labelling or enqueueing real work is a mutation that needs its own owner
approval. This module is that approval, expressed in code as a single, auditable,
**default-OFF** switch plus a fail-closed admission pipeline.

Design rules (enforced, not documented by convention):

* **Default OFF.** With no activation record -- missing, unreadable, malformed,
  ``enabled`` not exactly ``true``, wrong operation/repository or expired -- live
  ``queue_issue`` is refused with ``live_queue_disabled``. There is no implicit
  enablement and no environment variable that flips it on by accident.
* **Authorize on GitHub metadata, never on payload claims.** The caller must pass a
  :class:`gbp.bridge.identity.TrustedContext` that is already ``verified``; the
  self-declared ``actor``/``origin`` are only compared.
* **Target is bounded.** The target must be an *open* issue in the authorized
  repository carrying the trusted ``hermes-ready`` label, with all five mandatory
  headings, no tripwire match, not already processed/claimed/closed and no duplicate
  open PR.
* **Budget is still the worker's.** A missing or failing ``budget_guard`` blocks the
  command; the gate never guesses and never takes a hold itself.
* **Idempotent and replay safe.** A ``request_id``/``idempotency_key`` already seen is
  refused as ``already_processed``; the admission receipt is emitted at most once.
* **Nothing else is unlocked.** Everything outside ``queue_issue`` stays exactly as
  Phase 1 left it.

Standard library only; deterministic (callers pass ``now`` in).
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional

from gbp.bridge.commands import AUTHORIZED_ACTORS
from gbp.bridge.identity import TrustedContext, redact_login
from gbp.bridge.intake import REQUIRED_HEADINGS, preflight

__all__ = [
    "LIVE_OPERATION",
    "AUTHORIZED_REPOSITORY",
    "REQUIRED_ACTIVATION_FIELDS",
    "DEFAULT_ACTIVATION_PATH",
    "parse_activation",
    "load_activation",
    "activation_status",
    "evaluate_live_queue",
    "render_receipt",
]

#: The one operation this gate can ever enable.
LIVE_OPERATION = "queue_issue"

#: Only this repository may be a live-queue target.
AUTHORIZED_REPOSITORY = "StevenMarketing/steven-ai-os"

#: Fields an activation record must carry. A record missing any of them is ignored
#: (fail closed), so a truncated or hand-edited file can never enable the gate.
REQUIRED_ACTIVATION_FIELDS = ("enabled", "operation", "repository", "approved_by")

#: Where the owner-installed activation record lives (mode 600 on the VPS). Nothing
#: writes this file automatically; enabling live ``queue_issue`` is a deliberate,
#: owner-maintained deployment step.
DEFAULT_ACTIVATION_PATH = os.environ.get(
    "STEVEN_LIVE_QUEUE_ACTIVATION", "/opt/data/ops/bridge/live-queue-activation.json"
)

#: GitHub-visible link base for receipts.
_WEB = "https://github.com"

#: Minimal fallback tripwire set, used only when the worker's canonical
#: ``issue_worker_lib.INJECTION_PATTERNS`` cannot be imported (kept small on
#: purpose: the canonical list is the source of truth and is preferred).
_FALLBACK_PATTERNS = (
    ("override_instructions", r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?|context)"),
    ("system_prompt", r"\bsystem prompt\b"),
    ("credential_request", r"\b(?:reveal|print|show|dump|exfiltrate|send|paste|leak)\b[^\n]{0,30}\b(?:token|api[\s_-]?key|secret|credential|password|\.env)\b"),
    ("token_export", r"\bexport\s+GITHUB_TOKEN\b"),
    ("push_main", r"\b(?:push|commit|merge)\b[^\n]{0,40}\b(?:to\s+|into\s+|onto\s+)?main\b"),
    ("force_push", r"force[\s_-]?push"),
)


def _resolve_patterns(patterns: Any) -> Any:
    """Prefer the worker's canonical tripwire list; fall back to a small built-in set."""
    if patterns is not None:
        return patterns
    try:
        import issue_worker_lib  # type: ignore

        return tuple(getattr(issue_worker_lib, "INJECTION_PATTERNS", _FALLBACK_PATTERNS))
    except Exception:
        return _FALLBACK_PATTERNS


def parse_activation(raw: Any) -> Dict[str, Any]:
    """Validate an activation record. Returns ``{enabled, reason, record}``.

    Never raises: any malformed input yields ``enabled=False`` with a machine
    readable ``reason``.
    """
    if not isinstance(raw, dict):
        return {"enabled": False, "reason": "activation_missing_or_malformed", "record": {}}
    for field in REQUIRED_ACTIVATION_FIELDS:
        if field not in raw:
            return {"enabled": False,
                    "reason": "activation_field_missing: %s" % field, "record": {}}
    if raw.get("enabled") is not True:
        return {"enabled": False, "reason": "activation_not_enabled", "record": {}}
    if raw.get("operation") != LIVE_OPERATION:
        return {"enabled": False, "reason": "activation_operation_mismatch", "record": {}}
    if raw.get("repository") != AUTHORIZED_REPOSITORY:
        return {"enabled": False, "reason": "activation_repository_mismatch", "record": {}}
    if str(raw.get("approved_by") or "") != "StevenMarketing":
        return {"enabled": False, "reason": "activation_not_owner_approved", "record": {}}
    record = {
        "enabled": True,
        "operation": LIVE_OPERATION,
        "repository": AUTHORIZED_REPOSITORY,
        "approved_by": "StevenMarketing",
        "expires_at": raw.get("expires_at"),
        "note": str(raw.get("note") or "")[:200],
    }
    return {"enabled": True, "reason": "enabled", "record": record}


def load_activation(path: Optional[str] = None) -> Dict[str, Any]:
    """Read and validate the activation record at ``path``. Unreadable => OFF."""
    target = path if path is not None else DEFAULT_ACTIVATION_PATH
    try:
        with open(target, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return {"enabled": False, "reason": "activation_missing_or_malformed", "record": {}}
    return parse_activation(raw)


def activation_status(activation: Optional[Dict[str, Any]], *, now: float) -> Dict[str, Any]:
    """Is the gate on right now? Expiry is enforced here, fail closed.

    Accepts either the wrapper returned by :func:`parse_activation` /
    :func:`load_activation` (``{"enabled", "reason", "record"}``) or a bare record.
    """
    if not isinstance(activation, dict):
        return {"enabled": False, "reason": "activation_missing_or_malformed"}
    record = activation.get("record")
    if not isinstance(record, dict):
        record = activation
    if not record.get("enabled"):
        reason = activation.get("reason") or "activation_missing_or_malformed"
        return {"enabled": False, "reason": reason}
    expires_at = record.get("expires_at")
    if expires_at is not None:
        try:
            if float(expires_at) <= float(now):
                return {"enabled": False, "reason": "activation_expired"}
        except (TypeError, ValueError):
            return {"enabled": False, "reason": "activation_expiry_invalid"}
    return {"enabled": True, "reason": "enabled"}


def _label_names(issue: Dict[str, Any]) -> List[str]:
    labels = issue.get("labels") or []
    out: List[str] = []
    for label in labels:
        if isinstance(label, dict):
            out.append(str(label.get("name") or ""))
        else:
            out.append(str(label))
    return out


def _expired(validated: Dict[str, Any], now: float) -> bool:
    created = validated.get("created_at")
    ttl = validated.get("ttl_seconds")
    try:
        if created is None or ttl is None:
            return False
        return float(created) + float(ttl) <= float(now)
    except (TypeError, ValueError):
        return False


def evaluate_live_queue(
    *,
    validated: Dict[str, Any],
    trusted: Optional[TrustedContext],
    activation: Optional[Dict[str, Any]],
    issue: Optional[Dict[str, Any]] = None,
    processed: Optional[Dict[str, Any]] = None,
    ledger: Any = None,
    budget_guard: Optional[Callable[[str, Optional[int]], Any]] = None,
    open_pr: bool = False,
    patterns: Any = None,
    now: float = 0.0,
) -> Dict[str, Any]:
    """Decide whether a live ``queue_issue`` may be admitted. Never raises.

    Returns a bounded admission receipt::

        {"admitted": bool, "operation": "queue_issue", "issue": int|None,
         "request_id": str|None, "code": str, "reasons": [...], "links": {...},
         "mutated": bool}
    """
    processed = processed or {}
    receipt: Dict[str, Any] = {
        "admitted": False,
        "operation": LIVE_OPERATION,
        "issue": None,
        "request_id": None,
        "code": "refused",
        "reasons": [],
        "links": {},
        "mutated": False,
    }

    def refuse(code: str, detail: str = "") -> Dict[str, Any]:
        receipt["reasons"].append(code if not detail else "%s: %s" % (code, detail))
        receipt["code"] = receipt["reasons"][0].split(":", 1)[0]
        return receipt

    try:
        if not isinstance(validated, dict):
            return refuse("malformed_envelope")

        operation = str(validated.get("operation") or "")
        request_id = validated.get("request_id")
        receipt["request_id"] = request_id if isinstance(request_id, str) else None
        if operation != LIVE_OPERATION:
            return refuse("operation_not_live_queue_issue", operation or "none")
        if validated.get("dry_run") is True:
            # A dry run is handled by the executor's Phase-1 path; the live gate
            # only ever decides real admissions.
            return refuse("not_a_live_command")

        # 1. Opt-in gate, default OFF unless the owner installed a valid record.
        status = activation_status(activation, now=now)
        if not status["enabled"]:
            return refuse("live_queue_disabled", status["reason"])

        # 2. Identity must come from verified GitHub metadata.
        if trusted is None or not getattr(trusted, "verified", False):
            return refuse("identity_unverified")
        if getattr(trusted, "repository", "") != AUTHORIZED_REPOSITORY:
            return refuse("trusted_repo_mismatch", str(getattr(trusted, "repository", "")))
        if getattr(trusted, "source_kind", "") != "issue_comment":
            return refuse("trusted_source_not_allowed")
        if str(getattr(trusted, "author_login", "")) not in AUTHORIZED_ACTORS:
            return refuse("actor_not_allowlisted")

        # 3. TTL / replay — the schema already enforced these, re-check defensively.
        if _expired(validated, now):
            return refuse("command_expired")
        idem_key = validated.get("idempotency_key")
        if isinstance(request_id, str) and request_id and request_id in processed:
            return refuse("already_processed", request_id)
        if ledger is not None and isinstance(idem_key, str):
            try:
                if ledger.seen(idem_key) is not None:
                    return refuse("replay_duplicate", idem_key)
                if isinstance(request_id, str) and ledger.seen_request(request_id) is not None:
                    return refuse("replay_duplicate", request_id)
            except Exception:  # fail closed on a broken ledger
                return refuse("ledger_error")

        # 4. Target issue must be a bounded, safe, open candidate.
        if not isinstance(issue, dict):
            return refuse("target_missing")
        number = issue.get("number")
        try:
            number = int(number)
        except (TypeError, ValueError):
            return refuse("target_invalid_number")
        receipt["issue"] = number
        receipt["links"] = {"issue": "%s/%s/issues/%d" % (_WEB, AUTHORIZED_REPOSITORY, number)}

        if str(issue.get("repository") or AUTHORIZED_REPOSITORY) != AUTHORIZED_REPOSITORY:
            return refuse("target_repo_mismatch")
        if str(issue.get("state") or "open") != "open":
            return refuse("target_not_open")
        if str(number) in processed or number in processed:
            return refuse("already_processed", str(number))
        labels = _label_names(issue)
        if "hermes-ready" not in labels:
            return refuse("target_missing_ready_label")
        if "hermes-done" in labels or "hermes-blocked" in labels:
            return refuse("target_already_settled")

        verdict = preflight(issue.get("body") or "", patterns=_resolve_patterns(patterns))
        if verdict.get("tripwire"):
            return refuse("target_tripwire", ",".join(verdict["tripwire"]))
        if not verdict.get("usable"):
            missing = ",".join(verdict.get("missing_headings") or []) or "body_unusable"
            return refuse("target_unusable", missing)

        if open_pr:
            return refuse("duplicate_open_pr")

        # 5. Budget stays the worker's. Missing or failing guard blocks -- fail closed.
        if budget_guard is None:
            return refuse("budget_guard_missing")
        try:
            verdict_budget = budget_guard(LIVE_OPERATION, number)
        except Exception:  # noqa: BLE001 - never leak a provider detail
            return refuse("budget_guard_error")
        allowed = verdict_budget
        reason = ""
        if isinstance(verdict_budget, (tuple, list)) and len(verdict_budget) == 2:
            allowed, reason = bool(verdict_budget[0]), str(verdict_budget[1] or "")
        elif isinstance(verdict_budget, dict):
            allowed = bool(verdict_budget.get("allowed"))
            reason = str(verdict_budget.get("reason") or "")
        if not allowed:
            return refuse("budget_blocked", reason)

        receipt["admitted"] = True
        receipt["code"] = "admitted"
        receipt["mutated"] = True
        return receipt
    except Exception:  # noqa: BLE001 - fail closed on ANY unexpected fault
        return refuse("gate_error")


def render_receipt(receipt: Dict[str, Any], *, trusted: Optional[TrustedContext] = None) -> str:
    """GitHub-visible admission receipt (bounded, redacted, no free text)."""
    lines = [
        "### Command result — `claimed -> %s`" % ("queued" if receipt.get("admitted") else "rejected"),
        "",
        "| field | value |",
        "|---|---|",
        "| request_id | `%s` |" % receipt.get("request_id"),
        "| operation | `%s` |" % receipt.get("operation"),
        "| issue | `%s` |" % (receipt.get("issue") if receipt.get("issue") is not None else "—"),
        "| admitted | `%s` |" % bool(receipt.get("admitted")),
        "| code | `%s` |" % receipt.get("code"),
    ]
    if trusted is not None:
        lines.append("| actor (verified) | `%s` |" % redact_login(getattr(trusted, "author_login", "")))
        lines.append("| permission | `%s` |" % getattr(trusted, "permission", ""))
    link = (receipt.get("links") or {}).get("issue")
    if link:
        lines.append("| issue link | %s |" % link)
    if receipt.get("reasons"):
        lines.append("")
        lines.append("**Refusal reasons**" if not receipt.get("admitted") else "**Notes**")
        for reason in receipt["reasons"]:
            lines.append("- `%s`" % reason)
    lines += [
        "",
        "_Live `queue_issue` admission gate (`gbp.bridge.live_queue`). "
        "Authorized from GitHub metadata; nothing outside `queue_issue` is unlocked._",
    ]
    return "\n".join(lines)
