# T12 analysis — timeout root cause + benchmark failure classification

## 1. Root cause of the T12 timeout

The T12 card bundled **three independent jobs into one 300 s task**:

1. the end-to-end pipeline test (E2E),
2. the 500-location scale benchmark, and
3. the benchmark smoke test.

Writing and iterating on the E2E test alone needed ~2–3 min (author the file,
run it, fix imports/assertions, run again). Added to the benchmark work, the card
crossed the dispatcher's runtime cap and was auto-blocked before it could finish.

**Fix applied:** one deliverable per card.
- **T13** delivered the E2E pipeline test alone (`tests/test_e2e_pipeline.py`).
- **T17** (this card) delivers the smoke test alone (`tests/test_bench_smoke.py`),
  plus the web security headers (`src/gbp/api/server.py`) and this analysis.

Each card now has a single, small enough scope to complete well inside the cap.

## 2. Classification of the 11 failing locations

`docs/BENCHMARK.md` (Pass 1) reports **"Failed locations (incl. the killed one): 11"**
and **"Persistent-failure locations: 10"**, with **"Simulated kill events: 1"**.

Reading `scripts/bench_500.py`, the failures are all **deliberate fault injections**,
never real bugs:

- **10 persistent-failure locations** — injected in `build_dataset()`,
  `scripts/bench_500.py:201-210`: `persistent` is populated with ~10 locations,
  each mapped to a status of `429` (even k) or `500` (odd k). The transport
  returns that failing status every time in
  `BenchTransport._handle`, `scripts/bench_500.py:322-326` (`if loc in self.persistent`).
  These locations can never succeed, so they fail in every pass and drive the
  consecutive-failure alerting (10 alerts at threshold 3).
- **1 simulated kill** — the location killed mid-run:
  `build_dataset()` reserves `kill_location` = `names[len(names)//2]`
  (`scripts/bench_500.py:189-199`), and `BenchTransport._handle`
  (`scripts/bench_500.py:316-320`) raises `BenchKillError` (a non-retryable
  crash) when that location's page-2 token is requested.

**Conclusion: all 11 Pass-1 failures are intentional — 10 injected
`429`/`500` persistent failures + 1 simulated mid-run kill (`11 = 10 + 1`).**
None is a real bug. The kill location is then resumed from its checkpoint
(`resumed_from = accounts/bench/locations/loc-0250/kill-p2`, 4 upserts), after
which Pass 2 shows the steady-state **10 failed locations** (the persistent ones
only) and **upserted = 0, duplicates = 0**.

## 3. Numbers quoted from `docs/BENCHMARK.md` (not invented)

- Pass 1: upserted 1956, fetched 2320, duplicates 0, failed 11, peak queue 2.
- Resume: `loc-0250`, from `.../kill-p2`, 4 upserts.
- Pass 2: upserted 0, duplicates 0, failed 10.
- Transport: 125 injected 429/5xx retried with backoff, 1 simulated kill.
