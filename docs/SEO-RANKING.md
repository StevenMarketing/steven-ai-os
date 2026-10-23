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
| `week_over_week` | for each pair of **consecutive ISO weeks present in the period**: `delta = average_position(later) - average_position(earlier)` (3 decimals), plus `direction` = `improved` / `worsened` / `flat`. **Negative delta means the average position number fell, i.e. the salon moved up.** A week whose average is `None` yields a `None` delta with `direction: "unknown"` and is listed, never dropped. Two weeks that are present but **not** consecutive ISO weeks (a missing week between them) yield `delta: null` with `direction: "gap"` -- a gap is never presented as a week-over-week change (issue #92). |
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
  year boundaries: `2020-W53 -> 2021-W01`). Since issue #92 the label is
  validated **strictly**: the whole string must be ASCII `YYYY-Www` (a trailing
  newline, a space or non-ASCII digits such as `٢٠٢٦-W40` / `２０２６-W40` are
  refused) and the pair must be a **real** ISO calendar week
  (`date.fromisocalendar`), so `2025-W53` (2025 has 52 ISO weeks) and
  `0000-W01` are refused while `2020-W53` and `2026-W53` are accepted.
* `source` must be exactly `"simulated"`. `"scraped"`, `"local-rank-grid"`,
  `"SIMULATED"`, `" simulated "` and `None` are all refused.
* `(location_id, keyword, week)` is the key. A second observation for the same
  cell is refused everywhere (`duplicate (location_id, keyword, week)`): by
  `RankSeries.add` **and**, since issue #92, by every public metric / summary
  entry point through the shared input normalization -- `coverage`,
  `average_position`, `best_position`, `worst_position`, `top_share`,
  `week_over_week`, `visibility_by_keyword`, `visibility_index`,
  `summarize_location` -- for a list, a tuple, a generator or a series alike.
  Duplicates are **never** silently averaged or deduplicated: duplicate rank
  `1` + rank `20` for one cell raises instead of reporting `average_position =
  10.5`. Two readings of one cell in one ISO week cannot both be true.

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
metric definitions. For this fixture it is 4 880 bytes, and `to_json()` is
byte-identical across runs. The fixture's weeks are consecutive, so none of its
rows is a gap: issue #92 leaves every number above untouched.)

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
summarize_series(series).to_json()  13 732 bytes
  sha256 3b3e89d1bf26ea869710a5bd4035f3a1fe110cd1af27850ae60cf580073d49d5
summary(loc-001).to_json()      4 880 bytes
  sha256 a3b294771b94348e9212111e98cf1ca21b721659a1189fc3954e44bdc938e7e4
```

(The dataset digest is unchanged by issue #92 -- the simulated observations are
exactly the same. The two summary digests moved because `week_over_week`'s
entry in `METRIC_DEFINITIONS`, which every summary and report embeds, gained the
gap clause; the fixture's own numbers and rows are identical.)

Two runs in one process, and `simulate_dataset(seed=91)`, differ as expected;
`tests/test_seo_ranking.py` pins the mixer itself (`_seed_int(90, "a", "b") ==
1980356466062855818`, first five `randbelow(10)` draws `[4, 7, 9, 2, 2]`) so the
stream is provably independent of `random`.

## Tests

```bash
cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_seo_ranking
```

77 tests: the full validation matrix (out-of-range / non-int / bool positions,
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

20 of those tests are the issue #92 regressions, in three groups: the
`week_over_week` gap rules (missing week -> `direction: "gap"` with a null
delta, real year boundaries still yield a delta, a 53-week year is not jumped
over, and the fixture has no gap rows); the strict ISO-week matrix (rejects
`2025-W53` / `2021-W53` (53 weeks in 52-week years), `0000-W01`, week `00` /
`54`, trailing newline / carriage return / spaces / junk, and Arabic-Indic,
full-width and superscript digits; accepts `2020-W53`, `2026-W53` and ordinary
weeks, through `parse_week`, `week_label`, `RankObservation` and
`iso_week_range` alike); and central duplicate refusal, asserted on a **list**,
a **tuple**, a **generator** and a `RankSeries` for every metric function plus
`summarize_location`. Reverting only `src/gbp/seo/ranking.py` to the pre-fix
version fails 28 of these sub-tests, so they fail for the reason they exist.

## Correction pass -- issue #92

Three correctness defects in this module were found and fixed (same scope, same
simulated-only gate, no other behaviour changed):

1. **A gap was reported as a weekly change.** `week_over_week` paired the sorted
   weeks present in the input with `zip(weeks, weeks[1:])`, so two weeks two
   apart -- `2026-W10` (avg `4.0`) and `2026-W12` (avg `6.0`) -- produced
   `delta: 2.0, direction: "worsened"`, i.e. a three-week movement was labelled a
   one-week change. The pair is now emitted only when the two weeks are actually
   consecutive on the ISO calendar (`next_week(earlier) == later`). The chosen
   semantics for a gap: **keep the row and mark it** -- the two averages are
   still shown, `delta` is `None` and `direction` is `"gap"` -- because this
   module's rule elsewhere is to label what it cannot compute (a null week is
   listed as `"unknown"`) rather than to drop it silently. A row can therefore
   now never carry a delta that spans more than one ISO week. Consecutiveness is
   calendar-driven, so `2020-W53 -> 2021-W01` is consecutive, and `2026-W52 ->
   2027-W01` is a **gap**: 2026 has 53 ISO weeks, so `2026-W53` sits between
   them (the framework follows the calendar, not the year number).
2. **ISO week validation was too lax.** `^(\d{4})-W(\d{2})$` accepted `2026-W40\n`
   (`$` matches before a trailing newline), non-ASCII digits (`\d` matches
   them), `2025-W53` (2025 has 52 ISO weeks) and `0000-W01`. `parse_week` now
   matches `\A([0-9]{4})-W([0-9]{2})\Z` on the whole ASCII string **and** checks the
   real calendar with `date.fromisocalendar`; `week_label` applies the same
   calendar check, so it cannot mint a label `parse_week` would refuse. The
   comment claiming "week 53 is accepted for any year" is gone.
3. **Duplicates were only refused inside `RankSeries`.** The metric functions and
   the summary builders each materialized their own list, so a repeated
   `(location_id, keyword, week)` cell slipped through: duplicate rank `1` +
   rank `20` for one cell reported `average_position = 10.5`. Uniqueness is now
   checked in the one shared normalization path (`_observations`), so **every**
   public entry point -- list, tuple, generator or series -- refuses a duplicate
   with the same clear error. There is still no silent deduplication.

Accepted as-is, deliberately not changed: `week_over_week` compares a period's
*sorted* weeks, `summarize_location` bounds its period before computing metrics,
and the module still reports every position as simulated.

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
