# Offline GMap SEO audit (issue #39)

Deterministic, offline audit of **synthetic** Google Business Profile fixtures for
multi-location nail salons. It is the profile-side companion to GReply (review
replies): it looks at the listing fields a shop controls and turns them into
explainable, prioritized actions.

> **Fixtures only.** This module makes **no** Google API call, opens **no**
> socket, uses **no** credential and edits **no** live profile. It never ranks
> competitors and never claims a ranking improvement. All data used by the
> tests is synthetic.

Implementation: `src/gbp/seo/audit.py` · Tests: `tests/test_seo_audit.py`.

Companion module (issue #88): `src/gbp/seo/gap.py` -- offline competitor-gap
analysis that *reuses* this engine's `audit_location` scores instead of
re-implementing them. See `docs/SEO-GAP.md`. The audit engine itself still uses
no competitor data and makes no ranking claim.

## What it checks

| Area | Codes | Notes |
|---|---|---|
| Completeness | `SEO-MISSING-FIELD` | required fields: title, primary category, city, state, postal code, phone, website, hours, description, services |
| Primary category | `SEO-PRIMARY-CATEGORY-OFF`, `SEO-PRIMARY-CATEGORY-SUBOPTIMAL` | flags categories outside the nail/beauty set, and suggests `Nail Salon` |
| Secondary categories | `SEO-SECONDARY-CATEGORIES-EMPTY`, `-DUPLICATE`, `-OVERLAP-PRIMARY`, `-TOO-MANY` | |
| Services | `SEO-SERVICES-DUPLICATE`, `SEO-SERVICES-TOO-FEW` | |
| Description | `SEO-DESCRIPTION-TOO-SHORT`, `-TOO-LONG`, `-PHONE`, `-URL`, `-ALL-CAPS`, `-KEYWORD-STUFFING` | flags stuffing rather than encouraging it |
| Description <-> services coherence | `SEO-SERVICES-NOT-IN-DESCRIPTION`, `SEO-DESCRIPTION-SERVICE-NOT-LISTED` | deterministic cross-reference (`low`); quotes the matched terms and never asserts what the business offers |
| Hours | `SEO-HOURS-DAY-MISSING`, `-MALFORMED`, `-INVALID-RANGE`, `-ALL-CLOSED` | a close of `00:00` is read as `24:00` (see *Midnight close* below) |
| Photos | `SEO-PHOTOS-NONE`, `SEO-PHOTOS-TOO-FEW`, `SEO-PHOTOS-NO-EXTERIOR`, `-NO-INTERIOR`, `-NO-WORK` | optional `photos` field; not a required field (see *Photos & review health* below) |
| Review health | `SEO-REVIEWS-LOW-RATING`, `SEO-REVIEWS-TOO-FEW`, `SEO-REVIEWS-UNANSWERED` | optional review fields; each check is silent when its field is absent |
| Attributes | `SEO-ATTRIBUTES-EMPTY` | optional `attributes` list; absent/empty -> flagged (see *Tier 2* below) |
| Booking link | `SEO-BOOKING-MISSING` | optional `booking_url`; judged only when the profile has a website |
| Special/holiday hours | `SEO-SPECIAL-HOURS-UNSET` | optional `special_hours` mapping; validated with the regular-hours range rules |
| Local mention | `SEO-DESCRIPTION-NO-LOCAL` | the description names neither the profile city nor state |
| Identity (cross-location) | `SEO-IDENTITY-REUSED-NAME`, `SEO-IDENTITY-DUPLICATE-NAME-ZIP` | mirrors the dashboard convention: name + ZIP |
| Malformed input | `SEO-PROFILE-MALFORMED` | a wrong-typed field is reported, never invented |

Every finding carries `code`, `severity` (`high`/`medium`/`low`), `field`,
`message`, `evidence` and a `recommendation`. Actions are sorted by severity, then
code, then field, so the output is stable. Each location gets a deterministic
0-100 completeness `score` (100 − 15·high − 7·medium − 3·low, floored at 0).

Two findings are informational and do **not** affect the score:
`SEO-IDENTITY-REUSED-NAME` (same name across locations with distinct ZIPs — normal
for a chain) and `SEO-HOURS-OVERNIGHT` (a span that crosses midnight, issue #105).
Both are still reported and still appear in the actions list, but their penalty is
excluded from the score. The probable-duplicate
`SEO-IDENTITY-DUPLICATE-NAME-ZIP` keeps its full `high` penalty. The exclusion is
declared once in `_NON_SCORING_CODES` in `src/gbp/seo/audit.py`.

## Rule basis (issue #86)

Every numeric threshold in `src/gbp/seo/audit.py` carries an in-code comment
stating its basis. Inventory:

| Threshold | Value | Basis |
|---|---|---|
| Description minimum | `_DESCRIPTION_MIN = 250` | Google shows roughly the first 250 characters of the description before truncating in search, so 250 is the practical minimum for a useful, non-truncated description. |
| Description maximum | `_DESCRIPTION_MAX = 750` | Documented GBP business-description field limit (hard cap; text beyond it is truncated). |
| Keyword-stuffing repeat / density | `_STUFFING_MIN_REPEAT = 6`, `_STUFFING_DENSITY = 0.06` | Product heuristic: Google forbids keyword stuffing but publishes no number; a word is flagged only at >= 6 repeats **and** >= 6% of content words. |
| Secondary categories "too many" | `> 5` unique | Google allows one primary plus up to 9 additional categories but recommends the fewest that describe the business; 3–5 focused secondaries is the target. **Owner decision (Steven, 2026-10-22): rule kept as-is (`low`, scoring); this pass only records its basis.** |
| Services "too few" | `< 3` unique | Practical minimum for the services list to describe the shop; Google recommends listing every service sold. |
| All-caps guard | `>= 20` letters, `> 50%` upper | Requires enough letters to judge, and treats a strict majority upper-case as "mostly upper case". |
| Severity weights | `15 / 7 / 3` | Product scoring convention only — Google publishes no profile score; the weights rank findings, they are not a ranking factor. |
| Phone / URL / time shapes | NANP 3-3-4, `http(s)://`/`www.`, `HH:MM` | Google's content guidelines keep contact details and links out of the description; hours are 24-hour `HH:MM`. |
| Photo working floor | `_PHOTOS_TOO_FEW_MIN = 10` | Product heuristic: a visual service needs a gallery thick enough to show its range; Google publishes no photo minimum. Below 10 the listing gets `SEO-PHOTOS-TOO-FEW` (`low`). |
| Rating floor | `_RATING_MIN = 4.0` | Product heuristic: 4.0 is the conventional "customers are broadly happy" line; below it the ratings themselves are surfaced as `SEO-REVIEWS-LOW-RATING` (`medium`). |
| Review-count floor | `_REVIEW_COUNT_MIN = 20` | Product heuristic: a minimum review base for social proof; below it the listing gets `SEO-REVIEWS-TOO-FEW` (`low`). |
| Unanswered-review split | `_UNANSWERED_MEDIUM_MIN = 10` | Product heuristic: 1–9 unanswered reviews is a `low` backlog; 10 or more is a `medium` signal that replies are neglected. |

The description/services coherence check is offline and deterministic: it folds
plurals ("manicures" ~ "Manicure") and matches against a small built-in
`NAIL_SERVICE_VOCABULARY`. It reports only the cross-reference result and quotes
the matched terms; it never asserts what the business offers and invents no
service or category.

## Photos & review health (issue #107)

Two optional check groups, both offline and deterministic like the rest of the
engine. Every finding here scores normally (only the informational codes in
`_NON_SCORING_CODES` are excluded).

**Photos** — the profile gains an optional `photos` field: either a
non-negative integer total, or a mapping `{total, exterior, interior, work}` of
integer counts. `photos` is deliberately **not** in `REQUIRED_FIELDS`, so a
listing without the key is not flagged as a *missing field* — it gets the
dedicated photo findings instead.

| Condition | Finding | Severity |
|---|---|---|
| `photos` absent or `total == 0` | `SEO-PHOTOS-NONE` | `medium` |
| `0 < total < 10` | `SEO-PHOTOS-TOO-FEW` | `low` |
| mapping with `exterior == 0` | `SEO-PHOTOS-NO-EXTERIOR` | `low` |
| mapping with `interior == 0` | `SEO-PHOTOS-NO-INTERIOR` | `low` |
| mapping with `work == 0` (`work` = service/result photos) | `SEO-PHOTOS-NO-WORK` | `low` |

A mapping without an explicit `total` derives it from the category counts, and a
category the fixture omits is never judged. Malformed values (a string, a list,
a negative or non-numeric count) produce `SEO-PROFILE-MALFORMED` and never
crash the audit.

**Review health** — three optional fields (`rating`, `review_count`,
`unanswered_reviews`) that arrive from a separate pipeline, so **absence is not
a profile defect**: a field that is absent (or explicitly `null`) keeps its check
silent.

| Condition | Finding | Severity |
|---|---|---|
| `rating` present and `< 4.0` | `SEO-REVIEWS-LOW-RATING` | `medium` |
| `review_count` present and `< 20` | `SEO-REVIEWS-TOO-FEW` | `low` |
| `unanswered_reviews` present and `1–9` | `SEO-REVIEWS-UNANSWERED` | `low` |
| `unanswered_reviews` present and `>= 10` | `SEO-REVIEWS-UNANSWERED` | `medium` |

A present-but-malformed review value (non-numeric rating, a non-integer or
negative count) produces `SEO-PROFILE-MALFORMED`. The engine also exposes a
public `photo_total(profile)` helper (the tolerant parse used above) so the gap
analysis benchmarks photo counts from the same rule and the two can never
disagree.

## Tier 2 (issue #108)

Four additive, purely offline checks. Unlike the review data, these fields are
part of the profile the salon controls, so their absence is a (low-severity)
gap. Every finding here scores normally unless noted; only the informational
codes in `_NON_SCORING_CODES` are excluded.

**Attributes** — the profile gains an optional `attributes` list (e.g. walk-ins
welcome, women-owned, parking). Setting no attributes loses the customers who
filter for them.

**Booking link** — an optional `booking_url`. Only judged when the profile also
has a website: a shop with a site but no booking link is flagged, and a
present-but-malformed value (not an `http(s)`/`www` URL) is the same finding
with the bad value quoted. When the website itself is missing, the completeness
check owns it and this check stays silent.

**Special/holiday hours** — an optional `special_hours` mapping of
`date -> {open, close}` (a `"closed"` value is accepted and ignored). When the
field is present it is validated with the **same range rules as regular hours**:
a zero-length span is `SEO-HOURS-INVALID-RANGE` and a midnight-crossing span is
the informational `SEO-HOURS-OVERNIGHT` (issue #105, never lowers the score).
The findings carry `field="special_hours"` so they stay distinguishable from the
regular-hours ones; the codes are reused rather than duplicated.

**Local mention** — a case-insensitive, offline signal: when the description is
present but names neither the profile **city** nor **state**, it is flagged. The
recommendation only asks to mention the city *naturally* — never to repeat or
stuff place names. The check is skipped when the description is missing (the
completeness check owns that) or when the profile carries no city/state.

| Condition | Finding | Severity |
|---|---|---|
| `attributes` absent or empty | `SEO-ATTRIBUTES-EMPTY` | `low` |
| `booking_url` absent while a website is present | `SEO-BOOKING-MISSING` | `low` |
| `booking_url` present but not a URL | `SEO-BOOKING-MISSING` | `low` |
| `special_hours` absent or empty | `SEO-SPECIAL-HOURS-UNSET` | `low` |
| special-hours entry with `open == close` | `SEO-HOURS-INVALID-RANGE` (`field=special_hours`) | `medium` |
| special-hours entry crossing midnight | `SEO-HOURS-OVERNIGHT` (`field=special_hours`) | `low` (non-scoring) |
| description names neither city nor state | `SEO-DESCRIPTION-NO-LOCAL` | `low` |
| wrong-typed `attributes` / `special_hours` | `SEO-PROFILE-MALFORMED` | `medium` |

Malformed attribute or special-hours data is reported (`SEO-PROFILE-MALFORMED`)
and never crashes the audit.

## Midnight close (issue #98)

`00:00` on the **close** side is midnight at the *end* of the day, so
`_check_hours` reads it as `24:00` (1440 minutes) instead of `00:00` (0 minutes) —
but only when the open time is a real time after `00:00`. Before this, a shop
open `09:00-00:00` looked inverted (`540 >= 0`) and every such day added a
`medium` `SEO-HOURS-INVALID-RANGE`, so a perfect profile with one midnight-closing
day scored **93 instead of 100**.

| Fixture | Result |
|---|---|
| `10:00-00:00` (one day) | no finding, score 100 |
| `09:00-00:00` (all seven days) | no findings, score 100 |
| `19:00-09:00` | `SEO-HOURS-OVERNIGHT` (low severity note, non-scoring) |
| `09:00-09:00` (open == close) | `SEO-HOURS-INVALID-RANGE` (still) |
| `00:00-00:00` | `SEO-HOURS-INVALID-RANGE` (still: open is not *after* midnight) |
| `22:00-02:00` | `SEO-HOURS-OVERNIGHT` (low severity note, non-scoring) |
| malformed close (`00:0`, `25:99`) | `SEO-HOURS-MALFORMED`, unchanged |

**Overnight windows (owner decision, 2026-10-23, issue #105).** A span whose
open and close both parse and whose close is earlier on the clock than its open
(e.g. `22:00-02:00`, `19:00-09:00`) crosses midnight and is a valid way for a
shop to stay open past midnight. It is therefore **supported** as a low-severity
non-scoring note, `SEO-HOURS-OVERNIGHT` (`low`): it is reported for a human to
verify but never lowers the score. Only a zero-length span where `start == end`
(e.g. `09:00-09:00`, `00:00-00:00`) stays `SEO-HOURS-INVALID-RANGE`. `_minutes`
itself is unchanged, so the rule lives in one place — `_check_hours`.

**Known divergence.** `src/gbp/seo/gap.py` has its own `_has_full_week_hours`
helper that still treats a `00:00` close as *not* a full week of hours, so a
midnight-closing shop is counted as missing `full_week_hours` in the competitor
presence table (`docs/SEO-GAP.md`). That helper is outside this fix's scope
(`_check_hours` only) and was left untouched; aligning it is a separate change.

## Multi-location identity

Two salons may share a name. Following `tests/test_zip_code.py` and
`docs/DOMAIN-RULES.md` §2.3, the audit keeps them apart by **ZIP** plus the Google
resource id:

* same name, **distinct** ZIPs → kept separate, reported as `SEO-IDENTITY-REUSED-NAME`
  (informational: disambiguate, never merge; does not lower the score);
* same name **and** ZIP → `SEO-IDENTITY-DUPLICATE-NAME-ZIP` (`high`): probable
  duplicate listing, a human must confirm.

## Usage

```python
from gbp.seo import audit_profiles, load_profiles

report = audit_profiles(load_profiles())      # embedded synthetic fixtures
print(report.to_json())                        # canonical, byte-stable JSON

# or a fixture file on disk (still offline)
report = audit_profiles(load_profiles("/path/to/profiles.json"))
```

Example (synthetic demo account, trimmed):

```json
{
  "fixture_only": true,
  "source": "synthetic-fixture",
  "disclaimer": "Offline audit of synthetic Google Business Profile fixtures. ...",
  "location_count": 2,
  "total_findings": 18,
  "duplicate_names": {
    "demo nails": ["accounts/1001/locations/loc-001", "accounts/1001/locations/loc-002"]
  },
  "locations": [
    {
      "location_name": "accounts/1001/locations/loc-002",
      "score": 23,
      "highest_severity": "medium",
      "findings": [
        {
          "code": "SEO-PHOTOS-NONE",
          "severity": "medium",
          "field": "photos",
          "message": "The listing has no photos.",
          "evidence": "photos=<absent>",
          "recommendation": "Add real photos of the shop -- exterior, interior and your work ..."
        },
        {
          "code": "SEO-DESCRIPTION-TOO-SHORT",
          "severity": "medium",
          "field": "description",
          "message": "Description is shorter than the recommended minimum.",
          "evidence": "length=12 (min 250)",
          "recommendation": "Expand the description to roughly 250-750 characters ..."
        }
      ]
    }
  ]
}
```

## Tests

```bash
cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_seo_audit
```

102 tests: completeness, categories/services, description, hours (including the
issue #98 midnight-close rule), photos and review health (issue #107), Tier 2
attributes/booking-link/special-hours/local-mention (issue #108), duplicate
name/ZIP, reused-name non-scoring behaviour, description <-> services coherence,
empty and malformed fixtures, deterministic JSON, prioritised actions, deep-copy
fixtures, and an explicit **no-network guard** (sockets patched to raise and a
source scan for network imports).

## Dashboard preview (issue #42)

A read-only, offline, mobile-friendly preview of the audit lives inside the
existing web dashboard: the **SEO Audit** panel (`area=seo`, a nav-sub entry
next to *Business Dashboard* — the primary nav keeps its 8 tabs).

* `src/gbp/seo/preview.py` is a thin **view model** on top of this engine: it
  calls `audit_profiles` and re-shapes the report into per-salon cards. No audit
  logic is re-implemented there.
* `GET /api/seo/audit?dataset=<name>` serves it. Datasets are deterministic
  fixtures: `default`, `incomplete` (missing fields), `hostile` (markup in text,
  to prove escaping), `empty` and `error` (a deliberate 503 that exercises the
  UI error state). An unknown dataset is a `400`.
* Per salon the preview shows the **completeness score** — labelled exactly
  `completeness score (heuristic)`, never a ranking score — the missing required
  fields, and the prioritized findings with evidence and a suggested action,
  with **categories kept visually separate from services**. A visible banner
  states there is no live Google connectivity.
* Multi-location identity is the shared one: the business selector lists every
  salon as `name · ZIP · resource id`, so two same-name shops stay distinct.
* The renderer writes every dynamic value with `textContent` (no HTML sink), so
  fixture markup is displayed literally and can never execute.

Tests: `tests/test_seo_dashboard.py`.

## Limits (by design)

* No live GBP connectivity, no OAuth, no Google API, no paid service.
* No competitor data and no ranking claims: recommendations describe profile
  completeness only.
* No automatic profile edits and no writes anywhere.
* This does not touch the GReply approval workflow or any protected path.
