# GReply — Offline Reply Workflow (draft → edit → approve → publish-intent)

This document describes the offline, synthetic-only reply workflow added under
`src/gbp/replies/`. Nothing here touches the network, Google, OAuth, the
scheduler, the worker, the budget caps or any protected path.

## Lifecycle

```
draft ──edit──▶ edited ──approve──▶ approved ──publish request──▶ publish-intent
  │                 ▲                                              (terminal)
  └──────approve────┘
        approved ──edit──▶ draft        (edit after approval invalidates it)
```

States and edges are the single source of truth in `gbp.replies.states`:

| from            | to                    |
|-----------------|-----------------------|
| `draft`         | `edited`, `approved`  |
| `edited`        | `edited`, `approved`  |
| `approved`      | `draft`, `publish-intent` |
| `publish-intent`| — (terminal)          |

Any other transition raises `InvalidTransition` **before** state changes, so a
refused transition leaves the reply untouched.

## Human approval is mandatory

`ReplyWorkflow.approve(reply_id, approver, revision)` requires:

* a **non-empty approver identity**, and
* the **exact current revision**.

An approval for a superseded revision is refused (`ApprovalRefused`). The
approval record binds the revision, the approver, the timestamp and a SHA-256
hash of the exact approved text (`gbp.replies.models.Approval`).

Editing a reply **after** approval invalidates it: the reply returns to `draft`,
the old `Approval` is cleared, and a fresh approval is required.

## Publishing is disabled and fails closed

`ReplyWorkflow.publish(reply_id)` (and its alias `send`) is deliberately
unimplemented:

1. it records the `approved → publish-intent` bookkeeping transition;
2. then it raises `PublishDisabled` — **no network call is made**.

A second publish request for the same reply is refused as `DuplicatePublish`.

There is no auto-approve, no auto-publish and no timer-driven path anywhere in
this module. A model-backed draft generator can be injected by satisfying
`gbp.replies.generator.DraftGenerator`, but the offline
`DeterministicDraftGenerator` is the default and never calls a model.

## Audit trail

`gbp.replies.audit.AuditTrail` is append-only. One entry is recorded per
transition with: reply id, review id, location id, from-state, to-state, actor,
timestamp, revision and text hash. The actor string is passed through
`gbp.security.redact.redact_text`, and the trail never stores the raw text or
any credential material.

## Malformed fixtures

`gbp.replies.fixtures.validate_review` / `parse_reviews` refuse a fixture with a
missing review id, a missing location id, empty review text, a rating outside
1..5 or an unknown starting state. `parse_reviews` validates the whole
collection before returning anything, so a bad fixture never causes a partial
write.

## Tests

`tests/test_replies_workflow.py` is offline and uses only synthetic fixtures
(also mirrored at `tests/fixtures/replies_offline.json`). It covers draft
creation, edit, approval, refusal of a superseded-revision approval, refusal of
an empty approver, edit-invalidates-approval, publish refusal while live access
is disabled, duplicate publish refusal, invalid-transition refusal, malformed
fixture handling, audit-trail completeness and redaction, and a transport guard
proving no live call is attempted.

Run it with:

```
cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_replies_workflow
```
