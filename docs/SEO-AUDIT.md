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

## What it checks

| Area | Codes | Notes |
|---|---|---|
| Completeness | `SEO-MISSING-FIELD` | required fields: title, primary category, city, state, postal code, phone, website, hours, description, services |
| Primary category | `SEO-PRIMARY-CATEGORY-OFF`, `SEO-PRIMARY-CATEGORY-SUBOPTIMAL` | flags categories outside the nail/beauty set, and suggests `Nail Salon` |
| Secondary categories | `SEO-SECONDARY-CATEGORIES-EMPTY`, `-DUPLICATE`, `-OVERLAP-PRIMARY`, `-TOO-MANY` | |
| Services | `SEO-SERVICES-DUPLICATE`, `SEO-SERVICES-TOO-FEW` | |
| Description | `SEO-DESCRIPTION-TOO-SHORT`, `-TOO-LONG`, `-PHONE`, `-URL`, `-ALL-CAPS`, `-KEYWORD-STUFFING` | flags stuffing rather than encouraging it |
| Hours | `SEO-HOURS-DAY-MISSING`, `-MALFORMED`, `-INVALID-RANGE`, `-ALL-CLOSED` | |
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
  "total_findings": 12,
  "duplicate_names": {
    "demo nails": ["accounts/1001/locations/loc-001", "accounts/1001/locations/loc-002"]
  },
  "locations": [
    {
      "location_name": "accounts/1001/locations/loc-002",
      "score": 38,
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

34 tests: completeness, categories/services, description, hours, duplicate
name/ZIP, reused-name non-scoring behaviour, empty and malformed fixtures,
deterministic JSON, prioritised actions, deep-copy fixtures, and an explicit
**no-network guard** (sockets patched to raise and a source scan for network
imports).

## Limits (by design)

* No live GBP connectivity, no OAuth, no Google API, no paid service.
* No competitor data and no ranking claims: recommendations describe profile
  completeness only.
* No automatic profile edits and no writes anywhere.
* This does not touch the GReply approval workflow or any protected path.
