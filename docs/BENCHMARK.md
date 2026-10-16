# Offline scale benchmark (Group 5B)

Generated: 2026-10-15T17:33:23Z

Fully offline: in-memory SQLite, an in-memory reviews transport with
fault injection, a virtual (no-wait) clock, and NO AI/model calls.

## Command

```
PYTHONPATH=src python3 scripts/bench_500.py --locations 500 --concurrency 2
```

## Configuration

| Setting | Value |
| --- | --- |
| Locations | 500 |
| Reviews / location | 4 |
| Unique reviews generated | 2000 |
| Raw reviews served (incl. across-page duplicate) | 2375 |
| Queue concurrency cap | 2 |
| QuotaLimiter QPM | 300 |
| Persistent-failure locations | 10 |

## Pass 1 (primary run)

| Metric | Value |
| --- | --- |
| Reviews upserted | 1956 |
| Reviews fetched | 2320 |
| Duplicates stored | 0 |
| DB review rows | 1956 |
| Failed locations (incl. the killed one) | 11 |
| Wall-clock seconds | 0.265 |
| Reviews / second | 7544.7 |
| Peak queue usage (locations in flight) | 2 |

## Checkpoint / resume

- Killed location: `accounts/bench/locations/loc-0250`
- Resumed from checkpoint token: `accounts/bench/locations/loc-0250/kill-p2`
- Reviews upserted on resume: 4

## Pass 2 (idempotent re-run)

| Metric | Value |
| --- | --- |
| Reviews upserted | 0 |
| Duplicates stored | 0 |
| DB review rows | 1960 |
| Failed locations | 10 |

## Alerts

- `should_alert(threshold=3)` fired for 10 locations.
- Alerting locations: accounts/bench/locations/loc-0001, accounts/bench/locations/loc-0051, accounts/bench/locations/loc-0101, accounts/bench/locations/loc-0151, accounts/bench/locations/loc-0201, accounts/bench/locations/loc-0251, accounts/bench/locations/loc-0301, accounts/bench/locations/loc-0351, accounts/bench/locations/loc-0401, accounts/bench/locations/loc-0451

## Transport / quota instrumentation

- Transport calls: 3066
- Pages served: 2940
- Injected 429/5xx responses (retried with backoff): 125
- Simulated kill events: 1
- Peak concurrent transport requests: 1
- QuotaLimiter calls admitted: 2971 (QPM 300)

## Notes

- `incremental_sync` persists a per-location watermark, so pass 2 and 3
  fetch every page but skip every unchanged review: `upserted == 0`.
- The within-walk duplicate (a review repeated on page 2) is de-duplicated
  by `review_name`, so duplicates stored is always 0.
- The killed location is resumed from its stored pagination checkpoint
  (`resumed_from`) with no data loss.
