# Worker heartbeat and status snapshot

Deterministic, offline diagnostics for the unattended GitHub issue worker. The
logic lives in [`src/gbp/ops/heartbeat.py`](../src/gbp/ops/heartbeat.py) and the
read-only CLI in [`scripts/worker_heartbeat.py`](../scripts/worker_heartbeat.py).
Nothing here calls the network, the LLM, Google or GitHub, and nothing is
published.

## What it answers

- **Healthy idle vs stalled** — a poll inside the stall window is `idle`; a live
  claim older than the window (or a poll that never happened) is `stalled`.
- **Problem taxonomy** — every blocker is classified as exactly one of
  `budget`, `provider`, `policy`, `restricted_file`, `scheduling`, `transient`
  or `unknown`.
- **Bounded retries** — only `transient` faults are ever retried, and only while
  below `MAX_TRANSIENT_RETRIES` (2). Nothing else is retried automatically.
- **Sanitized snapshot** — a JSON summary with a timestamp, issue status, PR
  link, budget status, the blocker and one safe next step. Every free-text field
  passes through `redact()` first.
- **No duplicate notifications** — progress events are keyed by a stable
  identity (`event_identity`) and emitted once via `ProgressDeduper`; the
  seen-set can be persisted so de-duplication survives across runs.

## Usage

```sh
python3 scripts/worker_heartbeat.py --state ops/github-worker/state.json \
    --audit ops/github-worker/audit.jsonl
python3 scripts/worker_heartbeat.py --state-missing   # exercise safe failure
```

The CLI exits `0` for any diagnosable state, including a missing or unreadable
state file, which is reported as a `blocker` rather than raised.

## Publishing permissions — for separate owner approval

The snapshot is safe to *produce* today. Only *publishing* it needs approval:

| Capability | Needed for | Status |
|---|---|---|
| Telegram send | pushing a heartbeat/alert message | reused existing channel only; **new alerting requires approval** |
| Scheduler hook | emitting a heartbeat on a fixed schedule | **requires approval** — no cron/worker file is modified by this change |
| GitHub comment | posting the snapshot onto an issue/PR | **requires approval** |

No scheduler, workflow, protected file, budget or production configuration is
changed by this change; the integration above is deliberately left out and must
be authorised separately by the owner.
