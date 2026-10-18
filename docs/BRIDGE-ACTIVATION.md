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
