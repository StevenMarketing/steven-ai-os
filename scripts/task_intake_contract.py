#!/usr/bin/env python3
"""Owner task-intake contract: validate, classify and render a task manifest.

A task arrives as a JSON manifest (Phase A contract of Issue #65). This module is the single
place that decides whether a task may run unattended:

  * strict schema -- all seven fields are required and unknown fields are rejected, so a
    manifest cannot smuggle extra instructions past the validator;
  * scope is bounded -- repository-relative paths only, no absolute paths, no "..", a small
    file cap;
  * classification is fail-closed -- `routine` is escalated to `approval_required` whenever
    the goal, scope or rollback plan mentions production deployment, credentials,
    permissions, cron, budgets, security or anything destructive, and anything asking to
    rewrite history, delete evidence or disable a guard is `forbidden` and refused;
  * nothing here executes work: it validates and renders. Queueing still goes through the
    audited command bridge, and merge/deploy remain owner decisions.

Exit codes for the CLI: 0 = accepted (routine), 2 = approval_required, 3 = refused.
"""
import argparse
import json
import re
import sys

ALLOWED_FIELDS = ("goal", "scope", "approval_class", "idempotency_key", "caps",
                  "rollback_plan", "evidence_url")
SCOPE_FIELDS = ("kind", "paths", "max_files")
CAP_FIELDS = ("budget_usd", "timeout_s", "max_issue_comments")
KINDS = ("docs", "code", "infra")
CLASSES = ("routine", "approval_required", "forbidden")
MAX_FILES_LIMIT = 20
KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")

# Any of these turns a nominally routine task into an approval-required one.
HIGH_RISK = ("deploy", "deployment", "production", "credential", "secret", "api key",
             "permission", "access", "cron", "schedule", "budget", "billing", "security",
             "guard", "firewall", "google live", "oauth", "destructive", "drop", "truncate")
# These are refused outright, whatever the caller claims.
FORBIDDEN = ("force-push", "force push", "history rewrite", "rebase --force", "delete marker",
             "delete evidence", "disable guard", "bypass guard", "disable approval",
             "skip security", "weaken guard", "rm -rf")

HEADINGS = ("## Goal", "## Scope", "## Acceptance Criteria", "## Tests Required", "## Constraints")


def _text(manifest, field, errors):
    value = manifest.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append("%s: must be a non-empty string" % field)
        return ""
    return value.strip()


def _scope(manifest, errors):
    scope = manifest.get("scope")
    if not isinstance(scope, dict):
        errors.append("scope: must be an object")
        return None
    unknown = [k for k in scope if k not in SCOPE_FIELDS]
    if unknown:
        errors.append("scope: unknown field(s) %s" % ", ".join(sorted(unknown)))
    kind = scope.get("kind")
    if kind not in KINDS:
        errors.append("scope.kind: must be one of %s" % ", ".join(KINDS))
    paths = scope.get("paths")
    if not isinstance(paths, list) or not paths:
        errors.append("scope.paths: must be a non-empty list")
        paths = []
    cleaned = []
    for path in paths:
        if not isinstance(path, str) or not path.strip():
            errors.append("scope.paths: entries must be non-empty strings")
            continue
        path = path.strip()
        if path.startswith("/") or path.startswith("~") or ".." in path.split("/"):
            errors.append("scope.paths: %s must be repository-relative without '..'" % path)
            continue
        cleaned.append(path)
    max_files = scope.get("max_files")
    if not isinstance(max_files, int) or isinstance(max_files, bool) or not 1 <= max_files <= MAX_FILES_LIMIT:
        errors.append("scope.max_files: must be an integer between 1 and %d" % MAX_FILES_LIMIT)
    return {"kind": kind, "paths": cleaned, "max_files": max_files}


def _caps(manifest, errors):
    caps = manifest.get("caps")
    if not isinstance(caps, dict):
        errors.append("caps: must be an object")
        return None
    unknown = [k for k in caps if k not in CAP_FIELDS]
    if unknown:
        errors.append("caps: unknown field(s) %s" % ", ".join(sorted(unknown)))
    budget = caps.get("budget_usd")
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
        errors.append("caps.budget_usd: must be a positive number")
    timeout = caps.get("timeout_s")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        errors.append("caps.timeout_s: must be a positive integer")
    comments = caps.get("max_issue_comments")
    if not isinstance(comments, int) or isinstance(comments, bool) or comments < 1:
        errors.append("caps.max_issue_comments: must be a positive integer")
    return {"budget_usd": budget, "timeout_s": timeout, "max_issue_comments": comments}


def classify(manifest, goal, scope, rollback):
    """Fail-closed classification. Returns (approval_class, reasons)."""
    reasons = []
    haystack = " ".join([goal, rollback, str(scope.get("kind") if scope else "")]).lower()
    for token in FORBIDDEN:
        if token in haystack:
            return "forbidden", ["forbidden token detected: %s" % token]
    declared = manifest.get("approval_class")
    if declared not in CLASSES:
        return "approval_required", ["approval_class missing or unrecognised: fail closed"]
    for token in HIGH_RISK:
        if token in haystack:
            reasons.append("high-risk token detected: %s" % token)
    if scope:
        outside = [p for p in scope["paths"]
                   if not (p.startswith("docs/") or p.startswith("tests/") or p.endswith(".md"))]
        if scope.get("kind") != "docs" and outside:
            reasons.append("scope touches code paths outside docs/tests")
    if reasons:
        return "approval_required", reasons
    if declared == "routine":
        if scope and scope.get("kind") not in ("docs", "code"):
            return "approval_required", ["scope.kind %s is not routine" % scope.get("kind")]
        return "routine", []
    return declared, ["declared %s" % declared]


def validate(manifest):
    """Validate a manifest. Returns a decision dict; never raises."""
    errors = []
    if not isinstance(manifest, dict):
        return {"ok": False, "approval_class": "forbidden", "errors": ["manifest must be an object"],
                "reasons": [], "manifest": None}
    unknown = [k for k in manifest if k not in ALLOWED_FIELDS]
    if unknown:
        errors.append("unknown field(s): %s" % ", ".join(sorted(unknown)))
    missing = [f for f in ALLOWED_FIELDS if f not in manifest]
    if missing:
        errors.append("missing field(s): %s" % ", ".join(missing))
    goal = _text(manifest, "goal", errors)
    rollback = _text(manifest, "rollback_plan", errors)
    evidence = _text(manifest, "evidence_url", errors)
    if evidence and not re.match(r"^https?://\S+$", evidence):
        errors.append("evidence_url: must be an http(s) URL")
    key = _text(manifest, "idempotency_key", errors)
    if key and not KEY_PATTERN.match(key):
        errors.append("idempotency_key: must be 8-64 chars of [A-Za-z0-9._:-]")
    scope = _scope(manifest, errors)
    caps = _caps(manifest, errors)
    if errors:
        return {"ok": False, "approval_class": "rejected", "errors": errors, "reasons": [],
                "manifest": None}
    approval_class, reasons = classify(manifest, goal, scope, rollback)
    if approval_class == "forbidden":
        return {"ok": False, "approval_class": "forbidden", "errors": reasons, "reasons": reasons,
                "manifest": None}
    normalized = {"goal": goal, "scope": scope, "approval_class": approval_class,
                  "idempotency_key": key, "caps": caps, "rollback_plan": rollback,
                  "evidence_url": evidence}
    return {"ok": approval_class == "routine", "approval_class": approval_class,
            "errors": [], "reasons": reasons, "manifest": normalized}


def render_issue_body(manifest):
    """Render a validated manifest as an issue body carrying the five mandatory headings."""
    scope = manifest["scope"]
    caps = manifest["caps"]
    lines = [
        HEADINGS[0], manifest["goal"], "",
        HEADINGS[1],
        "kind: %s" % scope["kind"],
        "allowed paths: %s" % ", ".join(scope["paths"]),
        "max files changed: %d" % scope["max_files"],
        "approval class: %s" % manifest["approval_class"], "",
        HEADINGS[2],
        "One bounded change matching the goal; every file stays inside the allowed paths.",
        "Evidence is published at %s." % manifest["evidence_url"], "",
        HEADINGS[3],
        "Full offline suite plus the security/diff guards must pass; report exact counts.",
        "timeout: %ds" % caps["timeout_s"], "",
        HEADINGS[4],
        "Budget cap: $%.2f. Comment cap: %d." % (caps["budget_usd"], caps["max_issue_comments"]),
        "Rollback: %s" % manifest["rollback_plan"],
        "Idempotency key: %s" % manifest["idempotency_key"],
        "No merge, deployment, cron, credential, permission or security change.",
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="path to the JSON manifest")
    parser.add_argument("--render", action="store_true", help="print the rendered issue body")
    args = parser.parse_args(argv)
    with open(args.manifest, "r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    decision = validate(manifest)
    print(json.dumps({k: decision[k] for k in ("ok", "approval_class", "errors", "reasons")}, indent=2))
    if decision["ok"] and args.render:
        print(render_issue_body(decision["manifest"]))
    if decision["approval_class"] == "forbidden":
        return 3
    return 0 if decision["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
