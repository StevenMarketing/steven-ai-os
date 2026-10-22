# Offline, simulated ranking measurement framework (issue #90)

Deterministic, offline **rank measurement vocabulary** for the GMap SEO stream.
It defines one rank observation, generates a plausible multi-week position
series from a seed, computes the metrics the stream will report, and packages a
per-location per-period summary.

Implementation: `src/gbp/seo/ranking.py` · Tests: `tests/test_seo_ranking.py`.

> **SIMULATED data only -- enforced, not merely documented.** Every position in
> this document, in every test and in every report produced by the module comes
> from the module's own seeded generator. There is **no** live Google call, **no
> Local Rank Grid in any form**, **no** scraping, **no** rank checking, **no**
> network socket, **no** credential, **no** persistence and **no** dashboard
> work in this package. Non-simulated input is *refused at the data model*:
> `RankObservation` raises `ValueError` for any `source` other than
> `"simulated"`, so a scraper upstream cannot reach these metrics.

## Design in one line

A **measurement layer, not a measurement device**: it does not obtain a
position from anywhere. It holds simulated observations, refuses everything
else, and turns them into the same numbers twice.

## Metric definitions (the contract this module commits to)

The machine-readable copy of this table lives in
`gbp.seo.ranking.METRIC_DEFINITIONS` and is embedded in every report.

| Metric | Rule |
|---|---|
| `coverage` | share of observations in the period with a non-null position (`non_null / total`, 3 decimals). A `None` week is a **known** "outside top 20" reading, so it stays in the denominator. `0.0` for an empty period. |
| `average_position` | arithmetic mean of the **non-null** positions only (3 decimals); `None` when the period has no position at all. An "outside top 20" week is never counted as a number. |
| `best_position` / `worst_position` | smallest / largest non-null position; `None` when there is none. |
| `top_3_share` / `top_10_share` | share of **all** observations in the period whose non-null position is `<= 3` / `<= 10` (3 decimals). Null weeks are in the denominator, so a share cannot be inflated by dropping the weeks the salon was invisible. |
| `week_over_week` | for each pair of **consecutive ISO weeks present in the period**: `delta = average_position(later) - average_position(earlier)` (3 decimals), plus `direction` = `improved` / `worsened` / `flat`. **Negative delta means the average position number fell, i.e. the salon moved up.** A week whose average is `None` yields a `None` delta with `direction: "unknown"` and is listed, never dropped. |
| `visibility_index` | **INTERNAL index -- not a Google metric.** Per keyword, score each observation `max(0, 21 - position) / 20` and each null week `0`, then average over that keyword's observations; the index is the **mean of those per-keyword averages** (4 decimals), so every keyword counts once whatever its number of weeks. `None` for an empty period. Position 1 scores `1.0`, position 20 scores `0.05`, "outside top 20" scores `0`. |

The visibility index is always emitted next to
`gbp.seo.ranking.VISIBILITY_INDEX_LABEL`:
*"internal visibility index (simulated; not a Google metric, not a Google
position and not comparable to any Google-reported value)"*.

## The observation model

```
RankObservation(location_id, keyword, week, position, source="simulated")
```

* `position` is an `int` in `1..20` or `None` ("outside the top 20"). Bools,
  floats, strings and numbers outside the range are rejected with the offending
  value in the message.
* `week` is an ISO week label `YYYY-Www` (`parse_week` / `week_label` /
  `next_week` / `iso_week_range` are provided; `next_week` is correct across
  year boundaries: `2020-W53 -> 2021-W01`).
* `source` must be exactly `"simulated"`. `"scraped"`, `"local-rank-grid"`,
  `"SIMULATED"`, `" simulated "` and `None` are all refused.
* `(location_id, keyword, week)` is the key. A second observation for the same
  cell is refused by `RankSeries.add` (`duplicate (location_id, keyword, week)`),
  because two readings of one cell in one ISO week cannot both be true.

## The seeded generator

```python
from gbp.seo.ranking import simulate_dataset, simulate_keyword_series

series = simulate_dataset(seed=90)              # 2 locations x 3 keywords x 8 weeks
one = simulate_keyword_series("accounts/1001/locations/loc-001", "gel nails",
                              weeks, seed=90)
```

A bounded random walk on a latent integer: it starts in the top 15, drifts by at
most two places per week, and occasionally slips past the top 20 (recording
`None`) or climbs back. The stream is derived from
`sha256(repr(seed), location_id, keyword)` fed through this module's own frozen
SplitMix64 mixer -- **not** `random` -- so:

* the same seed reproduces a byte-identical dataset on any interpreter;
* a keyword's series never depends on the other keywords or on the generation
  order;
* `weeks` is the time axis: its order defines the trajectory (reversing the
  weeks reverses the series, correctly re-attributed), while the order of
  `locations` / `keywords` is irrelevant.

The default fixture is 8 consecutive ISO weeks (`2026-W10 .. 2026-W17`), two
locations (`loc-001`, `loc-002`) and three keywords (`nail salon`,
`gel nails`, `pedicure near me`) -- 48 observations, including `None` weeks for
`loc-002` (`gel nails`, `2026-W16` and `2026-W17`).

## Summaries

`summarize_location(location_id, observations, weeks=None)` and
`summarize_series(series, weeks=None, seed=90)`.

* `weeks` **bounds** the metrics (a summary never mixes two periods); omitted,
  the period is the sorted weeks present in the observations.
* One summary is one location: observations for another location raise.
* `LocationPeriodSummary.simulated` is a **required** field with a value of
  `True`. There is no default: omitting it is a `TypeError`, and
  `simulated=False` (or any falsy value) raises `ValueError`. The same holds for
  `RankingReport`, and a report refuses to carry a summary that lost the marker.
* `as_dict()` / `to_json()` are canonical (sorted keys), so equal input gives
  byte-identical output.

## Sample summary (embedded fixture, `loc-001`, trimmed)

```json
{
  "simulated": true,
  "label": "SIMULATED",
  "label_line": "SIMULATED DATA -- simulated fixture (seeded generator, offline)",
  "location_id": "accounts/1001/locations/loc-001",
  "week_count": 8,
  "keyword_count": 3,
  "observation_count": 24,
  "metrics": {
    "coverage": 1.0,
    "average_position": 6.708,
    "best_position": 2,
    "worst_position": 11,
    "top_3_share": 0.167,
    "top_10_share": 0.875,
    "visibility_index": 0.7146
  },
  "visibility_index_label": "internal visibility index (simulated; not a Google metric, not a Google position and not comparable to any Google-reported value)",
  "visibility_by_keyword": [
    {"keyword": "gel nails", "observations": 8, "in_top_20": 8, "score": 0.7188},
    {"keyword": "nail salon", "observations": 8, "in_top_20": 8, "score": 0.55},
    {"keyword": "pedicure near me", "observations": 8, "in_top_20": 8, "score": 0.875}
  ],
  "week_over_week": [
    {"from_week": "2026-W10", "to_week": "2026-W11", "from_average_position": 7.0,
     "to_average_position": 6.333, "delta": -0.667, "direction": "improved"},
    {"from_week": "2026-W12", "to_week": "2026-W13", "from_average_position": 5.333,
     "to_average_position": 6.667, "delta": 1.334, "direction": "worsened"}
  ]
}
```

(Abridged: the real summary carries all seven `week_over_week` rows and the
metric definitions. For this fixture it is 4 728 bytes, and `to_json()` is
byte-identical across runs.)

## Hand-computed fixture evidence

`loc-001` positions, in fixture order (`gel nails` / `nail salon` /
`pedicure near me`):

```
5 5 5 7 7 7 8 9 | 11 10 8 11 11 10 9 10 | 5 4 3 2 2 2 4 6
```

* sum `161` over 24 observations -> `average_position = 161/24 = 6.7083`;
* non-null everywhere -> `coverage = 1.0`; `best = 2`, `worst = 11`;
* positions `<= 3`: `3, 2, 2, 2` -> `top_3_share = 4/24 = 0.1667`;
  positions `<= 10`: `21` of 24 -> `top_10_share = 0.875`;
* weekly averages `7.0 6.333 5.333 6.667 6.667 6.333 7.0 8.333` -> deltas
  `-0.667 -1.0 1.334 0.0 -0.334 0.667 1.333`;
* per-keyword visibility `115/160 = 0.7188`, `88/160 = 0.55`,
  `140/160 = 0.875` -> index `2.14375/3 = 0.7146`.

`loc-002` has two `None` weeks, so the null-handling rules are visible there:
`coverage = 22/24 = 0.917`, `average_position = 208/22 = 9.455`,
`top_3_share = 0.0`, `top_10_share = 15/24 = 0.625`,
`visibility_index = (0.3625 + 0.6 + 0.625)/3 = 0.5292`.

These exact numbers are asserted in `tests/test_seo_ranking.py`
(`test_loc_001_metrics_match_the_hand_computation`,
`test_loc_002_metrics_match_the_hand_computation`,
`test_week_over_week_deltas_match_the_hand_computation`,
`test_visibility_by_keyword_matches_the_hand_computation`).

## Determinism proof

```
simulate_dataset().to_json()   14 800 bytes
  sha256 ef8919052c6376bafaadd9cae38fbdee12b9792808855afa82ed957b7e927c0e
summarize_series(series).to_json()  13 276 bytes
  sha256 7b39f008c90bd8b49c38eddc19914cdbf8edc6bf85a5d5b73650a80484e3da10
summary(loc-001).to_json()      4 728 bytes
  sha256 bd9f458ba175f2c118f3d03bbfa0cf15f864513dcb385857aeab1f9156bb33bf
```

Two runs in one process, and `simulate_dataset(seed=91)`, differ as expected;
`tests/test_seo_ranking.py` pins the mixer itself (`_seed_int(90, "a", "b") ==
1980356466062855818`, first five `randbelow(10)` draws `[4, 7, 9, 2, 2]`) so the
stream is provably independent of `random`.

## Tests

```bash
cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_seo_ranking
```

57 tests: the full validation matrix (out-of-range / non-int / bool positions,
malformed weeks, blank identifiers, non-simulated sources including
`"local-rank-grid"`, duplicates), frozen dataclass, generator determinism and
input-order independence, the frozen mixer constants, hand-computed metrics for
both fixture locations, null-week handling in `coverage` / `average_position` /
top shares / `week_over_week`, empty-period behaviour, the required
`simulated` field (`TypeError` when omitted, `ValueError` when not `True`), a
report refusing a summary that lost the marker, deterministic serialization and
an explicit **offline guard** (sockets patched to raise, a source scan for
network/scraping/`random` imports, and a check that no public name mentions a
grid, scraping or fetching).

## Limits (by design)

* No live GBP connectivity, no OAuth, no Google API, no rank checking, no
  scraping, no paid service and **no Local Rank Grid in any form**. The module
  cannot obtain a position; it can only measure positions handed to it as
  simulated.
* No persistence, no schema change, no dashboard UI and no automated profile
  edit: issue #90 is measurement only.
* Positions are integers `1..20` or "outside the top 20". The framework does not
  model per-grid-point ranks, radius, device or personalization -- that is
  exactly the grid surface it refuses.
* No causal or outcome claim: the metrics describe a simulated series and say
  nothing about a real business.
