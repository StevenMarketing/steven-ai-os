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
| Identity (cross-location) | `SEO-IDENTITY-REUSED-NAME`, `SEO-IDENTITY-DUPLICATE-NAME-ZIP` | mirrors the dashboard convention: name + ZIP |
| Malformed input | `SEO-PROFILE-MALFORMED` | a wrong-typed field is reported, never invented |

Every finding carries `code`, `severity` (`high`/`medium`/`low`), `field`,
`message`, `evidence` and a `recommendation`. Actions are sorted by severity, then
code, then field, so the output is stable. Each location gets a deterministic
0-100 completeness `score` (100 − 15·high − 7·medium − 3·low, floored at 0).

One finding is informational and does **not** affect the score:
`SEO-IDENTITY-REUSED-NAME` (same name across locations with distinct ZIPs — normal
for a chain) is still reported and still appears in the actions list, but its
penalty is excluded from the score. The probable-duplicate
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

The description/services coherence check is offline and deterministic: it folds
plurals ("manicures" ~ "Manicure") and matches against a small built-in
`NAIL_SERVICE_VOCABULARY`. It reports only the cross-reference result and quotes
the matched terms; it never asserts what the business offers and invents no
service or category.

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
| `19:00-09:00` | `SEO-HOURS-INVALID-RANGE` (still) |
| `09:00-09:00` (open == close) | `SEO-HOURS-INVALID-RANGE` (still) |
| `00:00-00:00` | `SEO-HOURS-INVALID-RANGE` (still: open is not *after* midnight) |
| `22:00-02:00` | `SEO-HOURS-INVALID-RANGE` (still) |
| malformed close (`00:0`, `25:99`) | `SEO-HOURS-MALFORMED`, unchanged |

**Out of scope (owner decision).** Only an exact `00:00` close is special-cased.
General overnight windows (e.g. `22:00-02:00`) are deliberately **not** supported
and keep their invalid-range finding; that is a separate product decision.
`_minutes` itself is unchanged, so the rule lives in one place — `_check_hours`.

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
  "total_findings": 13,
  "duplicate_names": {
    "demo nails": ["accounts/1001/locations/loc-001", "accounts/1001/locations/loc-002"]
  },
  "locations": [
    {
      "location_name": "accounts/1001/locations/loc-002",
      "score": 35,
      "highest_severity": "medium",
      "findings": [
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

50 tests: completeness, categories/services, description, hours (including the
issue #98 midnight-close rule), duplicate name/ZIP, reused-name non-scoring
behaviour, description <-> services coherence, empty and malformed fixtures,
deterministic JSON, prioritised actions, deep-copy fixtures, and an explicit
**no-network guard** (sockets patched to raise and a source scan for network
imports).

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
