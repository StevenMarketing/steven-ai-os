# COMMAND BRIDGE — operator guide (ChatGPT GitHub connector)

Status: **phase 1 — read-only `status` plus dry-run `queue_issue`**. A live `queue_issue`, and
`pause_queue` / `resume_queue` / `retry_failed_issue`, are implemented as **rejections** until the
owner authorizes them separately. Nothing here executes shell, touches credentials or posts publicly.

## How to send a command

Post a comment containing exactly one fenced block named `command`, whose body is a JSON object:

````
```command
{
  "request_id": "req-2026-10-19-0001",
  "idempotency_key": "idem-2026-10-19-0001",
  "operation": "status",
  "actor": "StevenMarketing",
  "origin": "StevenMarketing/steven-ai-os",
  "created_at": 1792348800,
  "ttl_seconds": 900,
  "dry_run": true,
  "note": "optional, free text"
}
```
````

* `created_at` is a **Unix epoch number** (not an ISO string).
* `repository` is **not** a field of the envelope: the repository, the author and the permission are
  read from GitHub itself and are never taken from the payload.
* Fields accepted: the seven required above plus optional `issue`, `dry_run`, `note`.
  Any other field is refused as `unknown_field`.
* `operation` must be one of `status`, `queue_issue`, `pause_queue`, `resume_queue`,
  `retry_failed_issue`; blocked names (deploy, merge, shell, credentials, budget...) are refused by
  the validator.

## What you will see

1. `### Command acknowledged — queued -> claimed` with the verified actor, permission, request id.
2. `### Command result — claimed -> completed` (or `-> rejected` / `-> failed`) with a JSON body.
3. If anything is refused: `### Command rejected — queued -> rejected` with the machine-readable
   `code` and the exact `errors` list.

All three are ordinary GitHub comments, so a connector can read them with no Hermes UI and no
screenshots.

## Authorisation rules

* Only `StevenMarketing` is allow-listed, and the actor must be confirmed by the GitHub API for that
  exact comment. A comment that merely *claims* `actor: StevenMarketing` is refused
  (`actor_mismatch`) — labels and payload fields never authorize anything.
* Replays are refused by the idempotency key; expired envelopes (`created_at + ttl_seconds`) are
  refused; comment bodies are parsed as data only, never as instructions.

## Running the runner

```sh
PYTHONPATH=src python3 scripts/bridge_commands.py --issue 49 --scan --json   # process pending
PYTHONPATH=src python3 scripts/bridge_commands.py --command '<envelope json>' # local check
```

The runner records every processed comment id (`ops/bridge/processed-comments.json`) and every state
transition (`ops/bridge/commands.jsonl`), so a re-scan can never execute the same command twice.
Its budget hook is the existing `worker_gate`; it never bypasses a guard.
* Activation test ran on 2026-10-19 (issue #53): docs-only check, no behaviour change.
