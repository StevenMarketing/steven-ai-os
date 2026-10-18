# Secure GitHub command bridge (ChatGPT → Hermes) — Phase 1 design

Status: **DESIGN + OFFLINE VALIDATION ONLY.** Nothing here is activated, no
cron entry is changed, no public port is opened, and no command is executed.
Activation requires the owner approvals listed at the end.

Why GitHub as the transport: the owner's ChatGPT GitHub integration can already
read issues, labels, comments and files, so commands and status travel over a
channel that exists, is auditable (every change is a signed, attributed event)
and needs **no public Hermes/VPS API**.

## 1. Existing intake — documented, not duplicated

The repo already has a working intake path; this bridge does not add a second
poller:

| Mechanism | File | Role |
| --- | --- | --- |
| eligible-issue report (pre-run) | `scripts/cron/github_worker_poll.sh` → `scripts/issue_poller.py` | reads `hermes-ready` issues owned by `StevenMarketing`, replays the timeline, prints the eligible set |
| change detector | `scripts/cron/github_worker_watch.sh` → `scripts/issue_poller.py --watch` | prints one deterministic fingerprint so unchanged ticks cost $0 |
| worker status snapshot | `scripts/worker_heartbeat.py` | sanitized liveness/budget snapshot (read-only) |
| budget + issue gate | `scripts/worker_gate.py` | provider-aware daily ceiling, per-task retry cap, atomic claim, kill switch |
| path guard | `scripts/issue_guard.py verify-diff` | blocks protected paths and credential-shaped diffs |

The bridge *reuses* these files. It adds schema, authorization, idempotency and
a status document; it never re-polls GitHub and never re-implements the gate.

## 2. Command envelope (strict schema)

A command is one JSON object inside a ```` ```command ```` fence in an issue or
comment body. Prose around the fence is never interpreted.

```json
{
  "request_id": "req-2026-10-18-001",
  "idempotency_key": "idem-43-status-1",
  "operation": "status",
  "actor": "StevenMarketing",
  "origin": "StevenMarketing/steven-ai-os",
  "created_at": 1790000000,
  "ttl_seconds": 300,
  "issue": 43,
  "dry_run": true,
  "note": "hourly check"
}
```

* required: `request_id`, `idempotency_key`, `operation`, `actor`, `origin`,
  `created_at`, `ttl_seconds`
* optional: `issue`, `dry_run`, `note`
* any **unknown field is rejected** (a `shell`, `prompt` or `url` key never
  reaches the queue)
* `operation` ∈ {`status`, `queue_issue`, `pause_queue`, `resume_queue`,
  `retry_failed_issue`}
* planned outputs on the envelope: `result` and `errors` are written back as a
  bounded, redacted comment — never as free text from an untrusted source

Enforced in `src/gbp/bridge/commands.py` (`validate_command`, `describe_schema`).

### Authorization

* Actor must be in `AUTHORIZED_ACTORS` (`StevenMarketing`) and the origin must
  be `StevenMarketing/steven-ai-os`. **Labels are hints, not credentials** — a
  label alone authorizes nothing.
* Privileged work (budget, secrets, permissions, deploy, destructive actions,
  public posting) is in `PHASE1_BLOCKED_OPERATIONS` and is rejected by name.
* Every non-read-only operation is accepted **only as a dry run** in Phase 1.

### Idempotency, replay, TTL

* `fingerprint()` is a stable SHA-256 of the meaningful fields (order-free).
* Same `idempotency_key` + same payload ⇒ `duplicate` (no-op, no second run).
* Same key or `request_id` + different payload ⇒ conflict, rejected.
* `created_at` in the future beyond 300 s of skew ⇒ rejected; expired envelopes
  (`created_at + ttl_seconds <= now`) ⇒ rejected; `ttl_seconds` must be 1–3600.

### Injection rejection

Any string field containing control bytes, shell metacharacters (`; | & $ \`
> < \` \\`), `..`, `$(`, `${`, `&&`, `||`, `>>`, `<<` is rejected
(`unsafe_character`), lengths are capped (128 chars, 200 for `note`). A fenced
block that is not JSON, or that names a blocked/unknown operation, is rejected
by the same schema — parsing is *data handling*, never instruction following.

### Budget-guard integration, fail-closed

For any non-read-only operation the caller must pass `budget_guard(operation,
issue)`, which returns truthy, or `(allowed, reason)`. A **missing guard, an
exception, or a refusal blocks the command** — the bridge never guesses the
budget state and never takes a hold itself. The real accounting stays in
`scripts/worker_gate.py` / `scripts/budget.py`.

## 3. GitHub-visible status document

Path the ChatGPT hourly check reads: **`ops/bridge/status.json`** (schema
`src/gbp/bridge/status.py`, `SCHEMA_VERSION = 1`).

```json
{
  "schema_version": 1,
  "version": "0.1.0",
  "generated_at": "2026-10-18T16:00:00Z",
  "state": "healthy_idle",
  "expected_cadence_seconds": {"scheduler": 600, "worker": 300, "status_document": 3600},
  "freshness": {"document_age_seconds": 120, "stale": false,
                "expected_status_cadence_seconds": 3600},
  "observed": {"scheduler_last_tick_age_seconds": 40, "worker_last_poll_age_seconds": 20,
               "worker_last_success_age_seconds": 20, "queue_depth": 0,
               "consecutive_failures": 0, "budget_paused": false,
               "current_issue": null, "db_or_vps_health": "not_observed"},
  "evidence": ["os-cron tick 2026-10-18T15:59:20Z"],
  "unknown": ["vps_host_health", "provider_budget_inside_hermes"],
  "notes": [],
  "publish_ok": true,
  "limitations": ["cannot observe VPS health directly", "GitHub outage ⇒ no status"]
}
```

States: `healthy_idle`, `healthy_busy` (an issue is in flight), `degraded`
(worker stale with a live scheduler, repeated failures, or budget pause),
`stopped_or_unreachable` (scheduler **and** worker beyond cadence), `unknown`
(insufficient evidence, or the previous upload failed).

Rules the document obeys:

* observed evidence is separated from `unknown`; **an unchanged issue/PR feed
  is never used to infer that the VPS is alive**;
* every string is redacted and bounded (10 notes, ≤200 chars each);
* a stale document (`document_age_seconds > 3600`) sets `freshness.stale = true`;
* a failed previous upload downgrades `healthy_*` to `unknown` — never to
  "fine".

## 4. Independent watchdog

`src/gbp/bridge/watchdog.py` evaluates a snapshot and returns alerts with
stable dedupe keys. The **independent signal** is an OS-cron tick file
(`ops/bridge/scheduler-tick.txt`) appended by the plain shell cron wrapper —
*not* by Hermes, *not* by the worker and *not* by the AI session. So when
Hermes itself is wedged (killed, out of budget, unable to publish), the tick
file keeps advancing and a missing/stale tick is still detectable.

Detections: `scheduler_heartbeat_missing` / `_stale`, `scheduler_stopped`
(explicit stopped flag with no worker poll), `worker_heartbeat_missing` /
`_stale`, `worker_never_succeeded`, `issue_stuck`, `queue_stuck`,
`repeated_failures`, `budget_paused`.

Blind spots (documented, not hidden):
* whole VPS down ⇒ the tick file stops too; the only evidence left is GitHub-side
  staleness of the status document (`unknown`/stale, never "healthy");
* GitHub unavailable ⇒ the verdict cannot be published at all;
* host clock skew shifts every age computation.

## 5. Alerts and low noise

Alerts are emitted at most once per `dedupe_key` per day; no alert is sent for
"nothing to do". Budget notifications are reuse of `scripts/notify.py --event
budget --check-thresholds`. Nothing here sends Telegram on its own in Phase 1.

## 6. Simulated evidence (offline)

`tests/test_command_bridge.py` (45 tests, all passing offline) simulates:
scheduler stopped then recovered, worker crash / never succeeded, VPS
unreachable from the GitHub side, status upload failure, stale status,
stuck queue, duplicate and unauthorized commands, replay/TTL, injection
rejection, redaction, bounded output, budget-guard refusal and fail-closed
behaviour — with **no production cron touched**.

## 7. Security review checklist (owner)

- [ ] Allowlisted operations only; blocked list reviewed by the owner.
- [ ] Actor/origin allowlists match the owner's GitHub login/repo.
- [ ] Labels are treated as hints, never as authorization.
- [ ] Idempotency/replay/TTL rules approved (1–3600 s).
- [ ] Fail-closed paths verified: missing guard, guard exception, unknown field,
      malformed fence, unexpected exception.
- [ ] Redaction verified on every published string.
- [ ] No new public network ports; no OAuth/token change; no paid service.
- [ ] Protected paths untouched (`.env*`, `src/gbp/auth/**`, `src/gbp/authz/**`,
      `src/gbp/security/**`, `scripts/issue_*.py`, `scripts/cron/**`,
      `scripts/hooks/**`, `.github/**`, `sql/schema.sql`,
      `tests/test_issue_worker*.py`, `docs/GITHUB-WORKER.md`).
- [ ] Status document contains no credential and is bounded.

## 8. Activation steps (after approval — NOT done here)

1. Owner approves this document and the checklist.
2. Add the tick-file writer to the cron wrapper (a `scripts/cron/**` change →
   **owner-only**, not a worker change).
3. Add a publisher that writes `ops/bridge/status.json` from the snapshot
   (read-only inputs; no new permissions).
4. Point the ChatGPT hourly check at that file; verify one real reading.
5. Enable the watchdog alert path, then confirm a simulated stale tick is
   detected and cleared.

## 9. Owner decisions still needed

* Approve the Phase-1 operation allowlist (and whether `queue_issue` may ever
  stop being dry-run).
* Approve who may widen `AUTHORIZED_ACTORS` beyond `StevenMarketing`.
* Approve the cron/cron-wrapper change for the tick file and the status
  publisher (protected path `scripts/cron/**`).
* Decide the alert threshold per day and the escalation channel.
