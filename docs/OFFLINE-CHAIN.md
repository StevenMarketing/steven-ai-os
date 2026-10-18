# Offline sync → reply-workflow chain (fixtures only)

`src/gbp/integration/chain.py` wires the offline review **sync**
(`gbp.sync.fixture_sync.FixtureSync`) into the offline **reply / approval
workflow** (`gbp.workflow.service.ApprovalService`) so a synced review can be
drafted, edited and approved end to end.

It is **offline and fixtures only**: no Google Business Profile call, no OAuth
change, no automatic publishing and no production deployment. The human
approval gate and the disabled publish path are preserved across the whole
chain.

## The chain

```
load fixture dataset
  -> sync (dedup within payload + idempotent upsert)
  -> select a synced review (bound to its own location)
  -> create a draft (at most one per review)
  -> edit
  -> approve (bound to the exact revision and text)
  -> request publish  -> ALWAYS REFUSED (live access disabled)
```

One call — `run_chain(repo, dataset, brand_loader=..., client=...)` or
`ReviewReplyChain(repo, ...).run(dataset)` — runs the whole chain and returns a
machine-readable summary (`{"ok", "published", "sync", "review", "steps", ...}`).

## Guarantees

| Guarantee | How it holds |
|---|---|
| Offline by construction | no HTTP client is imported; an `OfflineGuard` transport that raises can be passed in and the tests prove it is never called (`calls == 0`) |
| Idempotent replay | mutations go through `ApprovalService` `idempotency_key`s; a replay returns the cached result and writes nothing — no new rows, no duplicate audit entries |
| No second draft on resync | a review has at most one draft; an in-place update keeps the same review id, so the draft is not duplicated |
| Multi-location isolation | the draft is keyed on the review's own `review_id`; a review from one location is never drafted or approved against another |
| Approval gate preserved | `ApproveService.approve(expected_version=...)` binds the approval to the exact revision; a superseded revision raises `ConflictError`; an edit after approval invalidates it (`edit_after_approval`) and the reply drops back to a non-publishable draft state |
| Publish refused end to end | `publish_request` refuses with `live_access_disabled` while `live_enabled` is False (always, here); even the service's `publish_guard` is a dry-run that raises |
| Append-only audit | one entry per transition across both layers (`review_sync` for the sync step, `reply_draft` for the reply step) with ids, state names, actor, revision and text hashes |
| Redaction-safe | `redaction_check()` returns any audit entry carrying credential-shaped keys; the tests assert it is empty and only synthetic fixture data is present |

## Tests

```
cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest \
    tests.test_offline_chain_integration
```

Covers: full-chain happy path; sync idempotency; no-duplicate-draft across a
resync; in-place update of a changed review without a second draft;
per-location isolation; approval binding to the exact revision; edit
invalidating an approval; publish refused end to end; malformed-fixture
handling; combined audit-trail completeness and redaction; and the transport
guard proving no live call is attempted.
