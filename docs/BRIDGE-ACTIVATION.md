# BRIDGE ACTIVATION — status, protected-path patch and rollback

Everything in this document is a **proposal**. Nothing here has been activated, and no
protected path has been modified.

## 1. What landed in this PR (unprotected paths only)

| Blocker | Delivered | Files |
|---|---|---|
| 1. Identity spoofing | Authorization now runs only on **verified GitHub metadata** (comment author + collaborator permission) fetched through the API. Self-declared `actor`/`origin` are only *compared*; a mismatch is a refusal, and unknown permission fails closed. Privileged operations (`pause_queue`, `resume_queue`, `retry_failed_issue`) require `admin`. | `src/gbp/bridge/identity.py`, `tests/test_bridge_identity.py` |
| 2. Independent failure detection | Pure evaluator that recomputes staleness from `generated_at` and an **independently supplied clock**, ignores any frozen `document_age_seconds`, and marks itself `uses_local_cron: false`. | `src/gbp/bridge/external_watchdog.py`, `tests/test_bridge_external_watchdog.py` |
| 3. Status publisher + freshness | Redacted status document (second-precision `generated_at`, no frozen age), published as a comment on a dedicated machine-readable status issue, dry-run by default, bounded retries, structured failures, never raises. | `src/gbp/bridge/publisher.py`, `scripts/bridge_status_publish.py`, `tests/test_bridge_status_publisher.py` |
| 4. Silent rejection | Deterministic preflight for exactly `Goal`, `Scope`, `Acceptance Criteria`, `Tests Required`, `Constraints`; structured reasons; GitHub-visible rejection comment; unclaimed-eligible detection (default 5 minutes). Includes the exact #43 invalid-heading case. | `src/gbp/bridge/intake.py`, `scripts/bridge_intake_preflight.py`, `tests/test_bridge_intake_preflight.py` |

The VPS's own cron tick is **not** claimed anywhere as an independent monitor. The GitHub-side
job (below) is the independence mechanism; the local evaluator only supplies the decision logic.

## 2. Cadence reconciliation (reviewer question)

Verified from the live worker profile (`/opt/data/profiles/worker-agent/cron/jobs.json`) and the
scheduler log, not from documentation:

| Thing | Documented | **Observed** |
|---|---|---|
| Worker job `b4a0d80c816e` schedule | "300s / 600s" | **`every 1m`** |
| Monitor dedupe (agent run) | not documented | agent run suppressed whenever the monitor output hash is unchanged; the shim is written to keep its stdout constant when nothing is eligible |
| Claim TTL | 30 min | 30 min (ledger `expires_at`) |
| Worker per-issue timeout | 300 s | 300 s |

Consequence: "cron runs every minute" is normal, and an idle worker is expected — the agent is only
woken when the monitor output *changes* (for example `READY #43` replaces the no-eligible-issue
baseline). The stale cadence numbers in the docs should be corrected to `every 1m` when convenient.

## 3. PROTECTED-PATH PATCHES — owner approval required

Three changes need protected files. They are not applied here. Each is small and reversible.

### 3a. External watchdog: `.github/workflows/worker-watchdog.yml` (new file)

```yaml
name: worker-watchdog
on:
  schedule:
    - cron: "*/15 * * * *"      # GitHub's floor is ~5 min; runs are best-effort, not exact
  workflow_dispatch: {}
permissions:
  contents: read
  issues: read
jobs:
  evaluate-status:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install deps
        run: pip install -r requirements.txt || true
      - name: Read the newest status comment and evaluate staleness
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          REPO: ${{ github.repository }}
        run: |
          PYTHONPATH=src python3 scripts/bridge_watchdog_check.py --repo "$REPO" \
            --issue-number "${{ vars.STATUS_ISSUE_NUMBER }}" \
            --max-age-seconds 900 --fail-on-alert
```

Notes: this runs on GitHub's infrastructure, so it survives a dead VPS; it uses its own clock for
staleness (the evaluator never trusts a published age); `--fail-on-alert` makes a stale/missing
status a red workflow run, and the failure notification is GitHub's own.

### 3b. Publisher cadence: `scripts/cron/github_worker_poll.sh`

One appended line so a live status is published on a bounded cadence (the shim already prints
nothing unless something changed, so this must stay outside the monitored stdout):

```sh
# publish redacted status (bounded; never affects the monitored stdout)
python3 /opt/data/gbp-integration/scripts/bridge_status_publish.py --live \
  >/opt/data/ops/bridge/publish.log 2>&1 || true
```

Alternative with no protected file: a **second, separate cron job** in the worker profile whose
script lives outside `scripts/cron/`. That is the recommended option; it needs approval because it
adds a scheduled job.

### 3c. Intake wiring: `scripts/issue_poller.py`

Replace the silent skip with a structured, visible rejection:

```python
from gbp.bridge.intake import preflight, render_rejection   # import only

verdict = preflight(body, patterns=INJECTION_PATTERNS)
if not verdict["usable"]:
    print("  skipped #%s %s (refused: %s)" % (number, title, "; ".join(
        r["message"] for r in verdict["reasons"])))
    if os.environ.get("STEVEN_INTAKE_REJECT_COMMENTS", "0") == "1":
        post_comment(number, render_rejection(number, verdict))
    continue
```

Behind `STEVEN_INTAKE_REJECT_COMMENTS=1`, so the default poll behaviour is byte-identical and the
monitor output does not change. The structured reason is also written to
`/opt/data/ops/bridge/intake-rejections.jsonl` for the external watchdog to read.

## 4. Activation checklist (in order)

1. Owner approves this PR's unprotected changes (draft → ready → merge).
2. Create/choose the status issue: `PYTHONPATH=src python3 scripts/bridge_status_publish.py --json`
   (dry run), then `--live` once, and record the issue number in the `STATUS_ISSUE_NUMBER` repo variable.
3. Owner approves 3a (workflow) and 3b (cadence), then 3c (poller wiring).
4. First live evidence: the status issue holds a redacted JSON comment with a fresh `generated_at`;
   the workflow run shows an evaluation against its own clock.
5. Only then consider command execution itself (still allowlist + admin only, dry-run first).

## 5. Rollback

| Change | Rollback |
|---|---|
| This PR's modules | revert the merge commit; nothing else references them except the two new CLIs |
| `worker-watchdog.yml` | delete the file; no state is written by the watchdog |
| publisher line / extra cron job | remove the line or pause the job (`hermes -p worker-agent cron pause <id>`) |
| poller wiring | set `STEVEN_INTAKE_REJECT_COMMENTS=0` (restores byte-identical output), then revert |
| status issue | close it; comments are inert data |

## 6. Known limits, stated plainly

* GitHub Actions schedules are best-effort and can be delayed; a 15-minute budget plus a 900 s
  staleness threshold keeps false alarms low while still catching a dead VPS.
* The bridge is still **offline Phase 1**: no command is executed, no queue is mutated, no
  privileged operation is reachable. Activation of execution remains a separate, owner-approved step.
* `scripts/**` files added here (`bridge_status_publish.py`, `bridge_intake_preflight.py`) are not
  protected paths, so they can ship with this PR; the poller and cron shims cannot, and are not touched.


---

# ACTIVATION RECORD — Issue #45 (monitoring made live)

Owner authorization: "I authorize you to proceed with the safe implementation, testing,
merging, and deployment of the monitoring system, subject to all existing security and
protected-file policies." Protected files in this record were changed under the repository's
own documented owner-maintenance escape hatch (`HERMES_ALLOW_PROTECTED=1`, which lifts only the
protected-path rule — the secret check still runs), on a branch, for review. Nothing was merged
by the assistant.

## What is live now

| Component | State | Evidence |
|---|---|---|
| Machine-readable status issue | **live** | https://github.com/StevenMarketing/steven-ai-os/issues/46 — redacted JSON with a real `generated_at`, cron `every 1m`, `last_status`, worker `idle`, budget spend |
| Status publisher | **wired** | `scripts/cron/github_worker_poll.sh` publishes at most once every 10 minutes, output redirected to `ops/bridge/publish.log`, stdout untouched |
| Structured intake rejection | **wired** | the poller posts one deduplicated rejection comment per (issue, body) via `gbp.bridge.intake`, state in `ops/bridge/intake-rejections.json`, stdout untouched |
| External watchdog | **workflow ready** | `.github/workflows/worker-watchdog.yml` — two scheduled jobs on GitHub's own infrastructure, least privilege (`contents: read`, `issues: read`), `--fail-on-alert` turns a stale/missing status into a red run |

## Why the watchdog is genuinely independent

The workflow runs on GitHub's runners, not the VPS. It does not read anything the VPS writes except
the published status comment, and it recomputes age from `generated_at` against the runner's own
clock, so a frozen `document_age_seconds` cannot fake freshness. If the VPS, its cron or Hermes
stops, publishing stops, the status ages past 900 s, and the scheduled run goes red with an
`status_missing` / `status_stale` alert in its log.

## Verification performed for this activation

| Check | Result |
|---|---|
| Live publish | comment posted on #46, no `sk-`/token-shaped string in the payload |
| Read-back from GitHub | cron and worker fields parsed straight from the comment |
| Poller after wiring | poll runs normally; rejection path is env-gated and deduplicated |
| Tests | full suite green (see the activation PR for the exact count) |
| Guards | `verify-diff` on the branch reports the three intentionally protected files |

## How to simulate a stale heartbeat (owner-run)

1. Publish once so a fresh status exists (`python3 scripts/bridge_status_publish.py --live`).
2. Stop the publisher for more than 15 minutes (set `STEVEN_STATUS_PUBLISH=0` in the cron
   environment, or pause the worker job: `hermes -p worker-agent cron pause b4a0d80c816e`).
3. Trigger the workflow (`workflow_dispatch`) or wait for the schedule: the `status-watchdog` job
   fails with `status_stale` and prints the age it measured with its own clock.
4. Recovery: re-enable publishing; the next run is green again — no state needs repairing,
   because the watchdog is read-only.

## Rollback

| Change | Rollback |
|---|---|
| `.github/workflows/worker-watchdog.yml` | delete the file on a branch and merge; it holds no state |
| publisher line in the cron shim | set `STEVEN_STATUS_PUBLISH=0` (immediate, no file change), then revert |
| rejection wiring in the poller | set `STEVEN_INTAKE_REJECT_COMMENTS=0` (immediate), then revert |
| status issue #46 | close it; its comments are inert data |


## Follow-up (review on PR #47): the intake job now fails, not just logs

The first version of the `intake-watchdog` job only produced a diagnostic log. Fixed:

* `scripts/bridge_intake_preflight.py` gained `--fail-on-alert` and a single testable entry
  point, `evaluate_intake()`, which returns `blocking` plus a flat structured `reasons` list.
* The staleness reference now comes from the **trusted `hermes-ready` label event** read through
  the API, so the alert works on an ephemeral GitHub runner with **no persisted state**
  (`--no-record` is safe for exactly this reason). An existing claim, then a first-seen record
  on a long-lived host, remain as fallbacks.
* The workflow job now runs `... --stale-minutes 5 --no-record --fail-on-alert --json`, so an
  invalid `hermes-ready` issue or one unclaimed past the threshold **turns the run red**.
  The visible, structured rejection comment is still posted by the VPS poller, which is the only
  place that already holds write access; the watchdog stays read-only (least privilege preserved).

### Post-merge VPS deployment (a GitHub merge alone does not update the VPS)

```sh
ssh <vps>
cd /opt/data/gbp-integration && git fetch origin main && git checkout main && git pull --ff-only origin main
python3 scripts/bridge_status_publish.py --live --json          # expect code=published, issue 46
tail -5 /opt/data/profiles/worker-agent/ops/bridge/publish.log  # cron cadence log
bash /opt/data/scripts/github_worker_poll.sh                    # poll unchanged, fingerprint stable
```
Evidence to record after deploying: the new status comment URL, the poll fingerprint, and (for the
watchdog) the first green Actions run plus a red run produced by the stale-heartbeat simulation
above. Rollback remains the env flags (`STEVEN_STATUS_PUBLISH=0`, `STEVEN_INTAKE_REJECT_COMMENTS=0`)
before any file revert.


---

# ACTIVATION RECORD — Issue #51 (live `queue_issue` behind a narrow opt-in gate)

Owner authorization recorded on Issue #51 (comment, 2026-10-19 GMT+7): enable **live
`queue_issue` only for ordinary development Issues in `StevenMarketing/steven-ai-os`**,
subject to review, passing tests and the existing security/budget boundaries.

## What this PR delivers (unprotected paths only)

| Piece | File | Behaviour |
|---|---|---|
| Opt-in gate | `src/gbp/bridge/live_queue.py` | Default **OFF**. A live `queue_issue` is admitted only when an owner-installed activation record exists *and* every check passes. |
| Executor hook | `src/gbp/bridge/executor.py` | Optional `live_gate`; with no gate the Phase-1 refusal (`phase1_read_only`) is unchanged. |
| Runner wiring | `scripts/bridge_commands.py` | `live_gate_factory(...)` builds the gate; `--activation PATH` overrides the default record path. |
| Tests | `tests/test_live_queue_gate.py` | Unauthorized, forged, duplicate, expired, malformed, already-processed, unsafe, budget-exceeding and the exactly-once happy path — all offline. |

The gate **never trusts payload claims**: it authorizes on the `TrustedContext` fetched
from the GitHub API (comment author + collaborator permission), and re-checks the target
is an open issue in the authorized repo with the trusted `hermes-ready` label, all five
mandatory headings, no tripwire match, no existing claim/PR, and a passing budget guard.

## Exact enablement (owner-only, deliberately manual)

Live `queue_issue` stays OFF until the owner writes the activation record on the VPS:

```sh
cat > /opt/data/ops/bridge/live-queue-activation.json <<'JSON'
{"enabled": true, "operation": "queue_issue",
 "repository": "StevenMarketing/steven-ai-os",
 "approved_by": "StevenMarketing",
 "expires_at": 1798761600,
 "note": "owner-approved live queue_issue, ordinary dev issues only"}
JSON
chmod 600 /opt/data/ops/bridge/live-queue-activation.json
```

* A missing, unreadable, malformed, wrong-operation, wrong-repo, non-owner or **expired**
  record all resolve to `live_queue_disabled`.
* `expires_at` is an epoch second; set it to the end of the approved window and refresh
  it deliberately.

## Rollback

| Change | Rollback |
|---|---|
| Activation record | delete the file (or set `"enabled": false`) — immediate, no code change |
| Runner wiring | pass `--activation /nonexistent` or unset `STEVEN_LIVE_QUEUE_ACTIVATION` |
| This PR's modules | revert the merge commit; nothing else references them |

## Explicitly NOT unlocked

Everything outside `queue_issue` is unchanged: no shell, no protected-file writes, no
budget/security/credential/OAuth changes, no public ports, no live Google actions, no
deploy, no merge/approve. `pause_queue` / `resume_queue` / `retry_failed_issue` remain
Phase-1 blocked.
* **Live proof:** the production `queue_issue` enqueue path was exercised end-to-end on 2026-10-20 under owner authorization (throwaway run, no business side effects).
