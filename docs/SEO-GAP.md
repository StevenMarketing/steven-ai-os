# Offline competitor gap analysis (issue #88)

Deterministic, offline **competitor-gap** analysis for the GMap SEO stream. Given
one target salon profile and a set of competitor profiles in the *same* fixture
schema as the #39/#86 audit engine, it reports where the target profile's
**coverage** lags the profiles it is actually compared with -- services,
secondary categories, key-field presence, photo counts (issue #107), and the
distribution of the heuristic completeness score across the comparison set.

Implementation: `src/gbp/seo/gap.py` · Tests: `tests/test_seo_gap.py`.

> **Fixtures only.** This module makes **no** Google API call, opens **no**
> socket, scrapes **nothing**, uses **no** credential and edits **no** live
> profile. There is no live competitor data source: the "competitors" are
> synthetic fixture profiles. It never ranks competitors, never reports a
> competitor position, and never claims a ranking or visibility improvement.
> All data used by the tests is synthetic.

## Design in one line

It is a **set-arithmetic layer over the existing audit engine**: every
completeness score comes from `audit_location`, and no scoring rule is
re-implemented here (verified by
`test_module_reuses_the_audit_engine_instead_of_re_scoring`). What this module
adds is the comparison: union across the direct set, per-item competitor count
and share, a deterministic gap list, benchmarks and evidence-quoting
recommendations.

## Definitions (the contract this module commits to)

| Term | Rule |
|---|---|
| **Direct set** | competitors whose *normalized* primary category equals the target's normalized primary category; everyone else is `excluded` **with a reason** |
| **Self** | a competitor carrying the target's own resource id (`location_name`) is excluded into `skipped`; two *blank* ids are not an identity, so blank ids never trigger the self rule |
| **Malformed entry** | a non-mapping competitor is `skipped` with a reason; a mapping whose `services` / `secondary_categories` is not a list is still a direct competitor but is recorded in `notes` and contributes no coverage for that field |
| **Coverage table** | union of the direct set **with the target's own items**, so an item only the target lists is visible with a competitor count of `0` and never becomes a gap |
| **Share** | `competitor_count / len(direct set)` as an integer percentage (Python `round`, half to even); the raw count is always reported next to it |
| **Gap list** | the items the target does not list, sorted by competitor share descending, then name ascending |
| **Benchmarks** | target score vs the direct-set `median` / `min` / `max`, always labelled `heuristic completeness` |
| **Photo benchmark** | when the target and at least one direct competitor carry a photo total, target photo count vs the photo **median** of the direct competitors that carry one (`photo_benchmark`, issue #107); a competitor without a photo total is simply excluded from the median, and the report never claims a ranking/visibility outcome |
| **Field presence** | share of direct competitors carrying each of: website, phone, full-week hours, description >= 250 chars, next to the target's own presence |
| **Small-N guard** | fewer than `SMALL_N_MINIMUM` (3) direct competitors -> `insufficient-competitor-data` with **no** percentages, **no** coverage tables, **no** benchmarks, **no** field-presence rates and **no** recommendations -- only the resolved set and the reason |
| **Recommendations** | generated only from the tables above, each quoting its counts, always conditional ("if the salon offers X, consider ..."); the target's offering is never asserted and no ranking/visibility outcome is claimed |

`full_week_hours` means a usable `HH:MM` open/close pair for all seven days (the
same shape the audit engine scores), and `description_over_250` reuses the
engine's `_DESCRIPTION_MIN`.

## Usage

```python
from gbp.seo.gap import analyze_gap, load_gap_fixture

fixture = load_gap_fixture()                  # embedded synthetic fixtures
report = analyze_gap(fixture["target"], fixture["competitors"])
print(report.to_json())                       # canonical, byte-stable JSON

# or a fixture file on disk (still offline): {"target": {...},
#   "competitors": [...]}
fixture = load_gap_fixture("/path/to/gap.json")
```

Report shape (trimmed from the embedded sample below):

```
status / insufficient_data / reason      -> "ok" or the small-N state
target                                   -> id, name, primary category, score + label
direct_competitor_count, direct_set      -> the resolved comparison set
excluded, skipped, notes                 -> everything left out, with reasons
services, secondary_categories           -> {total_direct, target_count, items, gaps}
benchmarks                               -> target vs median/min/max (labelled)
photo_benchmark                          -> target photo count vs competitor median (issue #107)
field_presence                           -> per-field share + target_present
recommendations                          -> kind, subject, evidence, conditional text
```

## Sample report (embedded fixture, trimmed)

The embedded fixtures (`load_gap_fixture()`) hold a complete target, three direct
competitors, one competitor in another category, one entry that reuses the
target's resource id, and one non-mapping entry:

```json
{
  "status": "ok",
  "insufficient_data": false,
  "target": {"location_name": "accounts/1001/locations/loc-001",
              "display_name": "Demo Nails Garden Grove",
              "primary_category": "Nail Salon", "score": 100,
              "score_label": "heuristic completeness score (not a ranking score)"},
  "direct_competitor_count": 3,
  "direct_set": [
    {"display_name": "Bloom Nails", "score": 77},
    {"display_name": "Luxe Nails", "score": 78},
    {"display_name": "Sunny Nails", "score": 97}
  ],
  "excluded": [
    {"location_name": "accounts/2002/locations/cmp-104",
     "primary_category": "Barber Shop",
     "reason": "primary category 'Barber Shop' differs from the target primary category 'Nail Salon'"}
  ],
  "skipped": [
    {"index": "5", "reason": "not a mapping (NoneType)"},
    {"index": "4", "location_name": "accounts/1001/locations/loc-001",
     "reason": "same resource id as the target profile (self)"}
  ],
  "services": {
    "total_direct": 3,
    "gaps": [
      {"name": "Dip Powder", "competitor_count": 3, "competitor_share": 100, "target_lists": false},
      {"name": "Acrylic Nails", "competitor_count": 1, "competitor_share": 33, "target_lists": false},
      {"name": "Nail Art", "competitor_count": 1, "competitor_share": 33, "target_lists": false},
      {"name": "Shellac", "competitor_count": 1, "competitor_share": 33, "target_lists": false},
      {"name": "Spa Pedicure", "competitor_count": 1, "competitor_share": 33, "target_lists": false}
    ]
  },
  "benchmarks": {"label": "heuristic completeness", "target": 100, "median": 78,
                  "min": 77, "max": 97, "direct_competitors": 3,
                  "target_below_median": false},
  "photo_benchmark": {"label": "photo count (fixture)", "target": 12, "median": 25,
                      "min": 20, "max": 30, "direct_competitors_with_photos": 3,
                      "target_below_median": true},
  "field_presence": [
    {"field": "website", "present_count": 2, "competitor_share": 67, "target_present": true},
    {"field": "phone", "present_count": 3, "competitor_share": 100, "target_present": true},
    {"field": "full_week_hours", "present_count": 2, "competitor_share": 67, "target_present": true},
    {"field": "description_over_250", "present_count": 2, "competitor_share": 67, "target_present": true}
  ],
  "recommendations": [
    {"kind": "service", "subject": "Dip Powder",
     "evidence": "3 of 3 direct competitors list 'Dip Powder' (100%)",
     "text": "If the salon offers Dip Powder, consider adding it to the services list: 3 of 3 direct competitors list it (100%)."},
    {"kind": "service", "subject": "Acrylic Nails",
     "evidence": "1 of 3 direct competitors list 'Acrylic Nails' (33%)",
     "text": "If the salon offers Acrylic Nails, consider adding it to the services list: 1 of 3 direct competitors list it (33%)."},
    {"kind": "secondary_category", "subject": "Nail Art",
     "evidence": "2 of 3 direct competitors carry 'Nail Art' as a secondary category (67%)",
     "text": "If the salon offers Nail Art, consider adding it as a secondary category: 2 of 3 direct competitors carry it (67%)."},
    {"kind": "photos", "subject": "photos",
     "evidence": "target 12 photos vs direct-set median 25 across 3 direct competitors with photo counts",
     "text": "If the salon can add photos, consider doing so: the target has 12 photos while the direct-set median is 25 across 3 direct competitors with photo counts. Photo count only -- this is not a position or outcome claim."}
  ]
}
```

(Abridged: the real report lists every service and category union item, the full
`notes` list, and one recommendation per gap. `to_json()` output is 9 570 bytes
for this fixture and byte-identical across runs.)

## Small-N guard output (2 direct competitors)

```json
{
  "status": "insufficient-competitor-data",
  "insufficient_data": true,
  "reason": "only 2 direct competitor(s) share the target primary category 'Nail Salon'; at least 3 are required before competitor shares are reported",
  "direct_competitor_count": 2,
  "services": null,
  "secondary_categories": null,
  "benchmarks": null,
  "photo_benchmark": null,
  "field_presence": [],
  "recommendations": [],
  "excluded": [], "skipped": [], "notes": []
}
```

The resolved `direct_set` (ids, names, scores) is still returned, so a caller can
show *why* the set was too small; nothing statistical is emitted.

## Tests

```bash
cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_seo_gap
```

57 tests: coverage both ways (a common service the target lacks; a target-only
item that is never a gap), share rounding next to the raw count, gap-list
ordering, category coverage, benchmark median/min/max (odd and even sized sets),
the photo-count benchmark (issue #107: target vs competitor median, competitors
without a total excluded, no benchmark when the target has none), field-presence
rates vs the target, self-exclusion, blank-id handling, primary category
mismatch, malformed entries and malformed list fields, target without a primary
category, the small-N guard and its 3-competitor boundary (with an explicit "no
percentages in the JSON" assertion), score reuse from the audit engine, no input
mutation, byte-identical output for identical input, direct-set permutation
invariance, fixture loading/deep copy, wording safety (conditional only,
evidence counts, no ranking/visibility claim) and an explicit **no-network
guard** (sockets patched to raise, plus a source scan for network imports).

## Limits (by design)

* No live GBP connectivity, no OAuth, no Google API, no scraping, no paid
  service, no Local Rank Grid. Competitor data is synthetic fixtures only.
* No competitor ranking and no ranking or visibility claim: every number is a
  count over the fixture set, and completeness is always labelled heuristic.
* No automatic profile edits and no writes anywhere.
* Dashboard surfacing of the gap report is out of scope for issue #88.
* Reserved only for `pathlib`-based fixture loading: the module never opens a
  socket, so the audit's no-network contract carries over unchanged.
