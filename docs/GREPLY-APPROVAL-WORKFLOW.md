# GReply — canonical approval workflow and seam

Status: **decided and enforced by regression tests** (`tests/test_greply_approval_seam.py`).

This note removes the ambiguity left after PR #35/#37 between the two approval
implementations in the tree. It is documentation plus regression tests only —
no behavioural change, no protected path touched. Everything is offline and
synthetic; nothing here can publish or deploy.

## The two implementations

| | `gbp.workflow.service.ApprovalService` | `gbp.replies.workflow.ReplyWorkflow` |
|---|---|---|
| Storage | SQLite `reply_drafts` / `approval_records` rows (durable) | in-memory dict of `Reply` objects |
| State machine | `gbp.workflow.states` (`draft`, `needs_review`, `approved`, `ready_to_publish`, `published`, `rejected`, `failed`) | `gbp.replies.states` (`draft`, `edited`, `approved`, `publish-intent`) |
| Idempotency | `idempotency_key` — a replay returns the cached result, writes nothing | none (guarded by `DuplicatePublish`) |
| Concurrency | `expected_version` optimistic check → `ConflictError` | single revision check at approve/publish |
| Audit | one `approval_records` + one `audit_logs` row per mutation | append-only `AuditTrail` |
| Used by | **the JSON API, the web UI, and the integration chain** | nothing outside `src/gbp/replies/` |

## Decision: `ApprovalService` is canonical

The human-in-the-loop approval path for every real caller is
**`gbp.workflow.service.ApprovalService`**, reached through
`gbp.integration.chain.ReviewReplyChain` for the offline sync→reply seam:

```
fixture sync (gbp.sync.fixture_sync.FixtureSync)
  -> create draft            (reply_drafts row, status=draft)
  -> edit                    (ApprovalService.edit_draft, idempotency_key)
  -> approve                 (ApprovalService.approve, expected_version bound)
  -> request publish         (chain.publish_request -> ALWAYS REFUSED)
```

Caller map (verified by test):

| Caller | Module | Uses |
|---|---|---|
| HTTP JSON API | `src/gbp/api/server.py` | `ApprovalService` |
| Offline integration seam | `src/gbp/integration/chain.py` | `ApprovalService` |
| (this document) | `docs/GREPLY-APPROVAL-WORKFLOW.md` | — |

`gbp.replies.workflow.ReplyWorkflow` is an **in-memory prototype** from issue
#34. It is **not imported by any module outside `src/gbp/replies/`**. A
regression test walks `src/gbp` and fails if that ever changes, so it cannot be
silently wired into a production path.

## Approval controls preserved (both paths)

* **Approval is mandatory before any publish request.** Neither path reaches a
  publish step for an unapproved draft.
* **Approval binds the exact revision and text.** Editing an approved reply
  invalidates the approval: `ReplyWorkflow` drops back to `draft`;
  `ReviewReplyChain.edit_after_approval` rejects the approval and re-edits, so
  the reply is not publishable again without a fresh human approval.
* **Stale approvals are refused.** `ApprovalService.approve(expected_version)`
  raises `ConflictError` on a superseded revision; `ReplyWorkflow.approve`
  raises `ApprovalRefused` when the revision no longer matches.
* **Idempotent replay.** Re-running the chain writes no new reviews, no second
  draft and no duplicate audit entries.

## No automatic publishing — enforced in three places

1. `ReviewReplyChain.live_enabled` is `False` by construction;
   `publish_request` returns `{"published": False, "refused": True, "reason":
   "live_access_disabled"}` and performs **no** network call.
2. `ApprovalService.publish_guard` is a **dry-run that refuses**
   (`PublishBlocked`); there is no publish call anywhere in the service.
3. `ReplyWorkflow.publish` fails closed with `PublishDisabled` after recording a
   publish *intent*; it never uses its optional transport object.

The seam regression test injects an `OfflineGuard` transport that raises on any
request and asserts `guard.calls == 0` after a full run.

## Owner decision required (out of scope here)

Fully **consolidating** the prototype into the canonical service — deleting
`ReplyWorkflow`/`gbp.replies.states` or re-pointing its callers — is **not done
here**. It is a larger refactor that touches the API/web wiring and would remove
the in-memory test surface that `tests/test_replies_workflow.py` depends on.
The canonical path is documented and pinned; the prototype is quarantined
(referenced only inside its own package). If the owner wants the prototype
removed, that is a separate issue.

## Verification

```
PYTHONPATH=src python3 -m unittest tests.test_greply_approval_seam   # 7 tests
```

The full suite is owner-run on the branch (the CI-equivalent run takes ~3 min).
