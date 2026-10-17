# One-time retry grant (`scripts/retry_grant.py`)

Owner-authorized, audited escape hatch for a single blocked issue (`#19`).

## What it does

The worker ledger is idempotent: an issue that finished `blocked`/`failed` is
never picked up again. When the owner approves a revised scope the retry must
still be **one-off** and **traceable**. `retry_grant.py` lifts the idempotency
entry for exactly one issue number (`19`) and records everything around the
change:

| guarantee | how |
| --- | --- |
| owner authorization is an **artifact**, not a secret | a mode-600 JSON file the owner creates (`--approval-file`); there is no pass-phrase and no shared secret anywhere |
| operator is separately authorized | `--operator` must appear on `STEVEN_RETRY_GRANT_OPERATORS`; it is never self-declared |
| the **live** scope is confirmed | the issue must be open, its body must hash to the owner-approved revision (`approved_body_sha256`), and its scope must still name the approved directories while excluding protected server files |
| only the worker ledger is touched | the resolved ledger must be the worker-agent profile's (`…/profiles/worker-agent/ops/github-worker/state.json`), or be pinned with `STEVEN_RETRY_GRANT_STATE`; otherwise it refuses |
| a prior `blocked` record must exist | the entry cannot be lifted without it (fail-closed) |
| immutable prior record | `backups/retry-grant/issue-19-<ts>.json` (mode 600) keeps the prior outcome and the whole ledger |
| verified backup | the backup is re-read and its sha256 re-checked before anything is written |
| atomic apply | temp file + `os.replace`; any failure restores the exact prior ledger |
| append-only audit | `retry-grant-audit.jsonl` (fsync'd): `backup`, then `granted` or `failed` |
| crash recoverable | a `retry-grant.pending.json` marker is written before the change and removed after the audit lands; a leftover marker blocks a new grant |
| one-shot | the approval artifact is **consumed** (renamed) on success, and a second invocation with the same approval is refused |
| policy preserved | `usage`, `claims`, `version` and every other key are asserted unchanged |

It never runs the retry, never deploys, never merges and never calls Google. The
retry still counts against the existing daily budget and the attempt/retry cap.

## Owner creates the approval artifact

The owner writes this file themselves (mode 600). `approved_body_sha256` is the
sha256 of the issue body at the moment of approval — it pins the exact revision:

```console
$ python3 -c "import hashlib,urllib.request,json; \
tok=open('.env').read(); print('see retry_grant.body_sha256')"
$ mkdir -p /opt/data/profiles/worker-agent/ops/github-worker/approvals
$ cat > /opt/data/profiles/worker-agent/ops/github-worker/approvals/issue-19-once.approval.json <<'JSON'
{
  "schema_version": 1,
  "owner": "StevenMarketing",
  "issue": 19,
  "approval_nonce": "<random 16+ char string the owner chooses>",
  "approved_body_sha256": "<sha256 of the approved issue body>",
  "issued_at": "<ISO-8601 UTC>"
}
JSON
$ chmod 600 /opt/data/.../approvals/issue-19-once.approval.json
```

## Dry run (no writes)

```console
$ HERMES_HOME=/opt/data/profiles/worker-agent \
  python3 scripts/retry_grant.py preview \
    --issue 19 \
    --reason "owner approved the revised offline-only scope" \
    --operator <operator-login> \
    --approval-file /opt/data/.../approvals/issue-19-once.approval.json
{
  "dry_run": true,
  "state_path": "/opt/data/profiles/worker-agent/ops/github-worker/state.json",
  "audit_path": "/opt/data/profiles/worker-agent/ops/github-worker/retry-grant-audit.jsonl",
  "request_ok": true,
  "approval_id": "…",
  "prior_outcome": {"outcome": "blocked", ...},
  "eligible_prior_blocked": true,
  "scope_ok": true
}
```

Run from the **default** profile (no `HERMES_HOME`, no `STEVEN_RETRY_GRANT_STATE`)
it refuses instead of guessing:

```console
$ python3 scripts/retry_grant.py preview --issue 19 --reason "..." --operator x
{"refused": "refusing to touch a non-worker-agent ledger: …"}
```

## Grant (owner authorization, once)

```console
$ HERMES_HOME=/opt/data/profiles/worker-agent \
  STEVEN_RETRY_GRANT_OPERATORS=<operator-login> \
  python3 scripts/retry_grant.py grant \
    --issue 19 \
    --reason "owner approved the revised offline-only scope" \
    --operator <operator-login> \
    --approval-file /opt/data/.../approvals/issue-19-once.approval.json
```

## Inspect the trail

```console
$ python3 scripts/retry_grant.py history
$ python3 scripts/retry_grant.py verify-backup --backup <path> --sha256 <digest>
```

## Recovery

* **`retry-grant.pending.json` still present** — a run was interrupted between
  the ledger write and the audit append. Inspect it: if the ledger already shows
  `retry_grants["19"].status == "available"` and no `processed["19"]`, the grant
  is in force — append a `granted` audit row by hand and delete the marker. If
  the ledger still shows `processed["19"]`, restore it from the backup named in
  the marker and delete the marker, then re-run `grant`.
* **Audit append fails after the ledger change** — the command detects it,
  restores the exact prior ledger, writes a best-effort `failed` audit event and
  leaves the approval artifact **unconsumed**, so a fixed re-run works. The
  ledger is left untouched: the retry is not armed.

## Tests

```console
$ PYTHONPATH=src python3 -m unittest tests.test_retry_grant
```

Covers a missing/insecure/malformed approval artifact, operator authorization,
the worker-agent ledger guard, fail-closed reads, a missing prior `blocked`
record, exact-scope and approved-revision validation, backup verification,
append-only audit ordering, one-shot enforcement, concurrent access, invalid
scope, and one-time-audit reliability (rollback plus pending marker). Fully
offline (temp state dir + injected scope checker).
