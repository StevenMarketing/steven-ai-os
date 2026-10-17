# One-time retry grant (`scripts/retry_grant.py`)

Owner-authorized, audited escape hatch for a single blocked issue.

## What it does

The worker ledger is idempotent: an issue that finished `blocked`/`failed` is
never picked up again. When the owner approves a revised scope the retry must
still be **one-off** and **traceable**. `retry_grant.py` lifts the idempotency
entry for exactly one issue number (`19`) after four explicit inputs are given,
and records everything around the change:

| guarantee | how |
| --- | --- |
| explicit inputs | `--issue`, `--reason` (≥8 chars), `--owner`, `--authorization` are all required |
| scope confirmed | the live issue must be *open* and still carry the offline-only scope (fail-closed without a token) |
| immutable prior record | `backups/retry-grant/issue-19-<ts>.json` (mode 600) keeps the prior outcome and the whole ledger |
| verified backup | the backup is re-read and its sha256 re-checked before anything is written |
| atomic apply | temp file + `os.replace`; any failure restores the exact prior ledger |
| append-only audit | `retry-grant-audit.jsonl`: `backup`, then `granted` or `failed` |
| one-shot | a second invocation with the same approval, or a grant still `available`, is refused |
| policy preserved | `usage`, `claims`, `version` and every other key are asserted unchanged |

It never runs the retry, never deploys, never merges and never calls Google. The
retry still counts against the existing daily budget and the attempt/retry cap.

## Dry run (no writes)

```console
$ python3 scripts/retry_grant.py preview \
    --issue 19 \
    --reason "owner approved the revised offline-only scope" \
    --owner StevenMarketing \
    --authorization retry-issue-19-once
{
  "dry_run": true,
  "issue": 19,
  "approval_id": "…",
  "scope_ok": true,
  "scope_detail": "open with the revised offline-only scope",
  "prior_outcome": null,
  "state_path": "/opt/data/ops/github-worker/state.json"
}
```

## Grant (owner only, once)

```console
$ python3 scripts/retry_grant.py grant \
    --issue 19 \
    --reason "owner approved the revised offline-only scope" \
    --owner StevenMarketing \
    --authorization retry-issue-19-once
```

## Inspect the trail

```console
$ python3 scripts/retry_grant.py history
$ python3 scripts/retry_grant.py verify-backup --backup <path> --sha256 <digest>
```

## Tests

```console
$ PYTHONPATH=src python3 -m unittest tests.test_retry_grant
```

Covers missing authorization, incorrect issue number, repeated invocation,
backup verification, audit history, concurrent access, invalid scope and
failure recovery. Fully offline (temp state dir + injected scope checker).
