# Offline review sync (fixtures)

`gbp.sync.fixture_sync.FixtureSync` mirrors a **synthetic** review dataset into
the local SQLite store. It is the deterministic, network-free counterpart of
`ReviewSync` and is intended for tests, demos and dry runs — never for live
Google data.

## Dataset shape

A dataset is a JSON object with a `locations` list; each entry carries the
location resource name and its inline reviews:

```json
{
  "locations": [
    {
      "location": "accounts/9001/locations/loc-101",
      "reviews": [
        {"reviewId": "rev-1", "rating": 5, "comment": "Great service!",
         "reviewer": {"displayName": "Ann"},
         "createTime": "2026-01-01T00:00:00Z",
         "updateTime": "2026-01-02T00:00:00Z", "status": "published"}
      ]
    }
  ]
}
```

A review needs a non-empty `reviewId` and an integer `rating` in `1..5`.
`comment`, `reviewer.displayName`, `createTime`, `updateTime` and `status` may be
null. Anything else is treated as **malformed**: it is skipped, counted in
`errors` and never written.

## Usage

```python
from gbp.repo.sqlite_repo import Repository
from gbp.sync.fixture_sync import FixtureSync

repo = Repository(":memory:")
repo.init_db()
summary = FixtureSync(repo).sync_dataset(FixtureSync.load_dataset("dataset.json"))
```

## Summary

`sync_dataset` returns a stable, machine-readable summary:

```
{
  "locations": {"<resource>": {created, updated, unchanged, duplicates, errors, stored}},
  "totals":    {created, updated, unchanged, duplicates, errors, stored},
  "errors":    [{"location": "<resource|null>", "reason": "<text>"}]
}
```

## Guarantees

* **Deduplicated** — the same review id for the same location collapses to one
  record; the newest `updateTime` wins and the extra copies are counted as
  `duplicates`.
* **Idempotent** — replaying the identical dataset reports
  `created == 0` and `updated == 0`; nothing is written, so `first_seen_at`,
  `last_seen_at` and every field stay identical.
* **Multi-location, no leakage** — the internal key is the full resource name
  `<location>/reviews/<id>`, so two locations with the same review id stay two
  separate rows.
* **Read-only** — review state (`published`, `pending`, …) is stored as data
  only. There is no code path that publishes, edits or deletes anything on the
  Google side, and the sync makes no network call at all (the tests pass a
  transport that raises on any request to prove it).
