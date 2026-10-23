"""Loader helpers for the local JSON fixtures under ``fixtures/``.

Offline only: reads files from disk, makes no network calls, uses the standard
library exclusively. Every helper accepts an optional ``fixtures_dir`` override
so tests can point at a temporary directory.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List, Optional, Union

#: Default fixtures directory: <project root>/fixtures
FIXTURES_DIR: pathlib.Path = pathlib.Path(__file__).resolve().parents[2] / "fixtures"

PathLike = Union[str, pathlib.Path]


def _as_dir(fixtures_dir: Optional[PathLike] = None) -> pathlib.Path:
    return pathlib.Path(fixtures_dir) if fixtures_dir is not None else FIXTURES_DIR


def load_fixture(name: str, fixtures_dir: Optional[PathLike] = None) -> Any:
    """Load and parse a fixture by name.

    ``name`` may be given with or without the ``.json`` suffix, e.g.
    ``load_fixture("accounts")`` or ``load_fixture("accounts.json")``.
    """
    if not name.endswith(".json"):
        name = f"{name}.json"
    path = _as_dir(fixtures_dir) / name
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def list_accounts(fixtures_dir: Optional[PathLike] = None) -> List[Dict[str, Any]]:
    """Return the accounts from ``accounts.json``."""
    return list(load_fixture("accounts.json", fixtures_dir).get("accounts", []))


def list_locations(fixtures_dir: Optional[PathLike] = None) -> List[Dict[str, Any]]:
    """Return the locations from ``locations.json``."""
    return list(load_fixture("locations.json", fixtures_dir).get("locations", []))


def list_reviews_pages(fixtures_dir: Optional[PathLike] = None) -> List[Dict[str, Any]]:
    """Return review pages in traversal order (page 1, then page 2)."""
    return [
        load_fixture("reviews_page1.json", fixtures_dir),
        load_fixture("reviews_page2.json", fixtures_dir),
    ]


def load_location_groups(fixtures_dir: Optional[PathLike] = None) -> List[Dict[str, Any]]:
    """Return the extra business groups from ``location_groups.json``.

    Issue #112: the primary account (``accounts.json``) is the implicit
    "Ungrouped" bucket; this fixture adds one or more real ``LOCATION_GROUP``
    accounts (with their locations) so grouping is visible without live data.
    Returns an empty list when the fixture is absent, so the seeder stays
    tolerant of older checkouts.
    """
    try:
        data = load_fixture("location_groups.json", fixtures_dir)
    except FileNotFoundError:
        return []
    return list((data or {}).get("groups", []))


#: Review-page fixture files per location. The default two-page set is keyed by
#: ``loc-001``; the other locations each have a single-page fixture. Used by
#: :meth:`gbp.client.MockGBPClient.list_reviews_for` and the coverage tests.
LOCATION_REVIEW_PAGES: Dict[str, List[str]] = {
    "accounts/1001/locations/loc-001": ["reviews_page1.json", "reviews_page2.json"],
    "accounts/1001/locations/loc-002": ["reviews_loc002_page1.json"],
    "accounts/1001/locations/loc-005": ["reviews_loc005_vi_page1.json"],
}


def review_page_files(location: str) -> List[str]:
    """Return the fixture file names that hold ``location``'s review pages."""
    return list(LOCATION_REVIEW_PAGES.get(location, []))


def list_reviews_pages_for(
    location: str, fixtures_dir: Optional[PathLike] = None
) -> List[Dict[str, Any]]:
    """Return the raw review pages for ``location``."""
    return [load_fixture(f, fixtures_dir) for f in review_page_files(location)]


def list_reviews_empty(fixtures_dir: Optional[PathLike] = None) -> Dict[str, Any]:
    """Return the empty review page (``reviews: []``, no ``nextPageToken``)."""
    return dict(load_fixture("reviews_empty.json", fixtures_dir))


def list_sensitive_reviews(fixtures_dir: Optional[PathLike] = None) -> List[Dict[str, Any]]:
    """Return the sensitive-category review fixtures (G4-G9)."""
    return list(load_fixture("reviews_sensitive.json", fixtures_dir).get("reviews", []))


def load_brand_profiles(fixtures_dir: Optional[PathLike] = None) -> List[Dict[str, Any]]:
    """Return the brand profile fixtures keyed by ``location_id`` (G12-G14)."""
    return list(load_fixture("brand_profiles.json", fixtures_dir).get("profiles", []))


def load_error(name: str, fixtures_dir: Optional[PathLike] = None) -> Dict[str, Any]:
    """Load an error payload, e.g. ``load_error("error_429")``."""
    return dict(load_fixture(name, fixtures_dir))
