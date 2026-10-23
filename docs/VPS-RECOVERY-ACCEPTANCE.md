# VPS worker recovery acceptance (#74)

This is an approval and verification checklist, not a deployed service.
ChatGPT owns GReply, Muse owns SEO, Hermes executes bounded approved tasks.
Shared orchestration changes require coordination before application.

## Candidate changes

- PR #81 replaces the superseded #79 lifecycle proposal. It remains an inert
  SQLite library, with no dispatch, cron, notification or deployment side effect.
- PR #82 fixes two permission-test assumptions; no runtime or guard change.
- The heartbeat fix stops treating the act of reading a ledger as a poll.
  A current claim cannot mask a missing, invalid, future or stale poll. The
  diagnostic CLI reports missing evidence as stalled, rather than fresh idle.
  It does not clear claims, restart workers, retry tasks or spend AI budget.

These changes alone do not activate a complete lifecycle coordinator.
Preserve the existing bridge, worker gate, credentials, provider limits,
idempotency store, protected-path checks and approval settings.

## Observed recovery, 2026-10-21

The existing container was recreated or restarted during the read-only survey;
the cause is not established. At 23:07:06 UTC it had been up ten minutes and
published host port 32769 to container 4860. Worker and command runner in
worker-agent both reported successful scheduled executions at 23:07:03 UTC.
Status publisher reported success at 22:57:06 UTC on its ten-minute schedule.
This demonstrates that the schedules resumed after this particular start;
it does not prove a controlled reboot test or sustained 24/7 availability.
Do not use the changing web port as a permanent orchestration endpoint.

## Approval gate before applying

Owner separately approves each PR merge, exact deployment SHA and the affected
runtime files. This request does not include changing cron, SSH, DNS, firewall,
secrets, permissions, AI models or budget. If any such change becomes necessary,
prepare a separate concrete proposal. Do not deploy from a dirty worktree.

Before deployment, record current SHA and local file hashes; verify the
candidate against the actual deployment base. Preserve durable queue, marker,
budget and claim stores. Rollback means restore the prior reviewed code SHA;
never delete state to make an old command eligible again.

## Acceptance after an approved deployment

1. Check the exact worker profile and active schedules; do not create a second
   scheduler. Record current SHA, container start time and job last results.
2. Invoke worker_heartbeat.py with explicit worker state and audit paths.
   Merely finding state.json must never establish liveness. Use isolated copies
   to test missing/stale audit evidence; never alter live evidence.
3. Send one bounded read-only status request through the existing bridge using
   fresh request_id/idempotency_key/TTL. Record GitHub comment URL, acknowledgement
   and result. Re-reading is not resubmission. Do not manually run a mutating
   bridge runner concurrently with cron.
4. A full execution proof needs a separately approved docs-only task, exact
   allowed path, budget/runtime caps, and a unique idempotency key. Verify one
   claim, one Draft PR, test results and recorded cost. Queue acknowledgement
   is not worker execution, and Draft PR is not a merged/deployed product.
5. Read status from GitHub after closing the management browser. Recompute
   generated_at age using the advertised threshold; stale/missing evidence is
   unknown, never healthy. Verify watchdog evidence independently of VPS.
6. A controlled container/host restart test requires separate approval. After
   restart verify schedules, state persistence and no duplicate execution.
7. Stop and report blockers; do not clear claims, raise caps, weaken approvals,
   enable provider calls or add public listeners to make acceptance pass.

## Remaining coordinator work

Lifecycle events still need reviewed adapters at the real bridge-accept,
worker-claim, test and review-ready boundaries. They must preserve provenance
and distinguish review-ready from owner-approved completion. Missing heartbeat
must remain unknown/stalled. Persistent retries must use existing grants and
budget gates; a recommendation from the lifecycle reducer is not permission to
execute. Check #65/#74 and active PR heads before implementing those adapters.
