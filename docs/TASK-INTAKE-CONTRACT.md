# Owner task intake contract (Issue #65)

## What this is
One machine-checkable shape for a task the owner (or a ChatGPT session acting for the owner) hands to
Steven AI OS. The manifest is validated **before** anything is queued, and the validator is the only
place that decides whether a task may run unattended.

## Manifest
```json
{
  "goal": "one sentence",
  "scope": {"kind": "docs|code|infra", "paths": ["docs/x.md"], "max_files": 3},
  "approval_class": "routine|approval_required|forbidden",
  "idempotency_key": "issue65-demo-0001",
  "caps": {"budget_usd": 0.05, "timeout_s": 300, "max_issue_comments": 5},
  "rollback_plan": "how to undo this change",
  "evidence_url": "https://github.com/StevenMarketing/steven-ai-os/pull/0"
}
```

## Rules enforced by `scripts/task_intake_contract.py`
* all seven fields required, **unknown fields rejected** (a manifest cannot smuggle extra instructions);
* `scope.paths` must be repository-relative, no absolute paths, no `..`, and `max_files` is capped;
* `idempotency_key` must be 8-64 characters of `[A-Za-z0-9._:-]`;
* `evidence_url` must be an http(s) URL; every cap must be a positive number/integer;
* **classification is fail-closed.** Any high-risk token (deploy, production, credential, secret,
  permission, cron, budget, security, guard, oauth, destructive, ...) escalates a nominally routine
  task to `approval_required`; a code scope outside docs/tests does the same; anything asking to
  force-push, rewrite history, delete evidence or disable a guard is `forbidden` and refused. A
  manifest that cannot be classified never becomes routine.

## State machine for one task
```
submitted -> validated -> queued (label) -> claimed (worker lease) -> executing
          -> draft_pr -> gates (tests, guards) -> report
failures: rejected (schema/authz) | expired (TTL) | budget_refused
          | unknown_outcome (pending marker, manual review) | aborted (rollback)
```

## Routine vs approval-required
* **Unattended (routine):** documentation, tests, bounded refactors inside the listed paths,
  analysis/reports, Draft PR creation and gate runs.
* **Approval required:** merge, any deployment, cron/budget/credential/permission/security change,
  live Google operations, destructive actions, anything outside the listed scope.
* **Forbidden:** force-push, history rewrite, deleting markers or evidence, disabling guards.

## What this does not do
It does not execute work, does not queue anything by itself, and does not change any guard. Queueing
still goes through the audited command bridge (`queue_issue` with an activation record), and merge and
deployment remain owner decisions.

## Honest statement about continuous operation
Intake is not a ChatGPT daemon. The autonomous part is the VPS: a cron-spawned worker polls GitHub
every minute and a GitHub Actions schedule checks liveness every fifteen minutes from outside the VPS.
ChatGPT is a client that writes to GitHub inside a session; it cannot be described as running 24/7, and
the watchdog observes and alerts rather than executing work.
