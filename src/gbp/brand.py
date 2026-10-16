"""Brand profile loading and per-location isolation.

The brand profile is the *single source of truth* for one shop. Lookups are
strictly keyed by ``location_id``: a review for shop B must never see shop A's
name, city or keywords (docs/DOMAIN-RULES.md §2.3 and §6).

When a profile is missing or partial we return a NEUTRAL profile with
``complete=False`` and empty fields, never another shop's data.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

__all__ = [
    "BrandProfile",
    "BrandLoader",
    "load_brand_profiles",
    "get_for",
    "NEUTRAL_LANGUAGE",
]

NEUTRAL_LANGUAGE = "en"
_ALLOWED_LANGUAGES = ("en", "vi")


def _as_list(value: Any) -> List[str]:
    """Coerce a JSON field into a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    out: List[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _norm(location_id: Any) -> str:
    """Normalise a location key so int/str ids match consistently."""
    return str(location_id).strip() if location_id is not None else ""


@dataclass
class BrandProfile:
    """Verified brand data for exactly one location."""

    location_id: Optional[Union[int, str]] = None
    business_name: str = ""
    city: str = ""
    state: str = ""
    category: str = ""
    services: List[str] = field(default_factory=list)
    language: str = NEUTRAL_LANGUAGE
    tone: str = ""
    seo_keywords: List[str] = field(default_factory=list)
    do_not_mention: List[str] = field(default_factory=list)
    rules: Dict[str, Any] = field(default_factory=dict)
    auto_publish_allowed: bool = False
    #: ``False`` whenever the row was missing or partial (neutral fallback).
    complete: bool = False

    # ------------------------------------------------------------------ #
    # completeness flags (what MAY be mentioned downstream)
    # ------------------------------------------------------------------ #
    #: Each flag is True only when the field is usable *and* the profile is
    #: complete. A missing/partial profile therefore exposes every flag as
    #: False, so the prompt + validator can forbid inventing the detail.
    @property
    def has_city(self) -> bool:
        """True only when a verified city may be mentioned."""
        return bool(self.complete and (self.city or "").strip())

    @property
    def has_state(self) -> bool:
        """True only when a verified state/region may be mentioned."""
        return bool(self.complete and (self.state or "").strip())

    @property
    def has_services(self) -> bool:
        """True only when the verified service list may be mentioned."""
        return bool(self.complete and list(self.services or []))

    @property
    def has_seo_keywords(self) -> bool:
        """True only when curated local-SEO keywords may be used."""
        return bool(self.complete and list(self.seo_keywords or []))

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def merge_for_prompt(self) -> Dict[str, Any]:
        """Return a ``{}``-safe mapping for prompt building.

        Every documented key is always present, so callers can use ``.get``
        freely and never key-error on a partial profile. Empty data stays
        empty (the generator then forbids mentioning it).
        Complete profiles expose their verified data; incomplete (missing or
        partial) profiles mask the identity fields so the prompt can never
        mention a name/city/service we cannot verify.
        """
        if not self.complete:
            return {
                "location_id": self.location_id,
                "business_name": "",
                "city": "",
                "state": "",
                "category": "",
                "services": [],
                "language": self.language or NEUTRAL_LANGUAGE,
                "tone": self.tone or "",
                "seo_keywords": [],
                "do_not_mention": list(self.do_not_mention),
                "rules": dict(self.rules),
                "auto_publish_allowed": bool(self.auto_publish_allowed),
                "complete": False,
                "has_city": False,
                "has_state": False,
                "has_services": False,
                "has_seo_keywords": False,
            }
        return {
            "location_id": self.location_id,
            "business_name": self.business_name or "",
            "city": self.city or "",
            "state": self.state or "",
            "category": self.category or "",
            "services": list(self.services),
            "language": self.language or NEUTRAL_LANGUAGE,
            "tone": self.tone or "",
            "seo_keywords": list(self.seo_keywords),
            "do_not_mention": list(self.do_not_mention),
            "rules": dict(self.rules),
            "auto_publish_allowed": bool(self.auto_publish_allowed),
            "complete": bool(self.complete),
            "has_city": self.has_city,
            "has_state": self.has_state,
            "has_services": self.has_services,
            "has_seo_keywords": self.has_seo_keywords,
        }

    def is_verified_service(self, service: str) -> bool:
        """True when ``service`` is in this shop's verified service list."""
        needle = (service or "").strip().lower()
        if not needle:
            return False
        return any(needle == s.strip().lower() for s in self.services)

    @classmethod
    def neutral(cls, location_id: Any = None) -> "BrandProfile":
        """A safe, empty profile (never another shop's data)."""
        return cls(location_id=location_id, complete=False)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "BrandProfile":
        """Build a profile from one JSON object.

        ``complete`` is True only when the mandatory/identifying fields are
        usable (business_name + city + language). Partial rows stay neutral so
        the generator refuses to invent missing details.
        """
        raw = raw or {}
        location_id = raw.get("location_id")
        business_name = str(raw.get("business_name") or "").strip()
        city = str(raw.get("city") or "").strip()
        state = str(raw.get("state") or "").strip()
        category = str(raw.get("category") or "").strip()
        services = _as_list(raw.get("services_json", raw.get("services")))
        seo_keywords = _as_list(raw.get("seo_keywords_json", raw.get("seo_keywords")))

        language = str(raw.get("language") or "").strip().lower()
        if language not in _ALLOWED_LANGUAGES:
            language = NEUTRAL_LANGUAGE

        rules_raw = raw.get("rules_json", raw.get("rules"))
        rules: Dict[str, Any] = dict(rules_raw) if isinstance(rules_raw, dict) else {}

        do_not_mention = _as_list(raw.get("do_not_mention"))
        if not do_not_mention:
            do_not_mention = _as_list(rules.get("do_not_mention"))

        auto_publish = raw.get("auto_publish_allowed", 0)
        auto_publish_bool = bool(int(auto_publish)) if isinstance(auto_publish, (int, float, str)) and str(auto_publish).strip() != "" else False

        complete = bool(business_name) and bool(city)

        return cls(
            location_id=location_id,
            business_name=business_name,
            city=city,
            state=state,
            category=category,
            services=services,
            language=language,
            tone=str(raw.get("tone") or "").strip(),
            seo_keywords=seo_keywords,
            do_not_mention=do_not_mention,
            rules=rules,
            auto_publish_allowed=auto_publish_bool,
            complete=complete,
        )


class BrandLoader:
    """Holds ``location_id -> BrandProfile`` and serves isolated lookups."""

    def __init__(self, profiles: Optional[Dict[Any, BrandProfile]] = None):
        self._profiles: Dict[str, BrandProfile] = {}
        for key, profile in (profiles or {}).items():
            self._profiles[_norm(key)] = profile

    @classmethod
    def from_file(cls, path: Union[str, pathlib.Path]) -> "BrandLoader":
        return cls(load_brand_profiles(path))

    def __len__(self) -> int:
        return len(self._profiles)

    def keys(self) -> List[str]:
        return list(self._profiles.keys())

    def get_for(self, location_id: Any) -> BrandProfile:
        """Return the profile for ``location_id`` or a NEUTRAL one.

        The returned profile is always safe to use: if the location is unknown
        (or the stored row was partial) ``complete`` is False and every field is
        empty, so no other shop's data can leak into the prompt.
        """
        profile = self._profiles.get(_norm(location_id))
        if profile is None or not profile.complete:
            if profile is not None and not profile.complete:
                return profile
            return BrandProfile.neutral(location_id)
        return profile


def load_brand_profiles(path: Union[str, pathlib.Path]) -> Dict[str, BrandProfile]:
    """Load brand profiles from a JSON file.

    Accepts either ``{"profiles": [...]}`` or a bare list. Returns a mapping
    keyed by the normalised ``location_id`` string (int ids become ``"1"``).
    """
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    if isinstance(data, dict):
        rows = data.get("profiles", [])
        if not rows and all(isinstance(v, dict) for v in data.values()):
            # Bare mapping {location_id: {...}}
            rows = [dict(v, location_id=v.get("location_id", k)) for k, v in data.items()]
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    out: Dict[str, BrandProfile] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        profile = BrandProfile.from_dict(row)
        if profile.location_id is None:
            continue
        out[_norm(profile.location_id)] = profile
    return out


def get_for(profiles: Dict[Any, BrandProfile], location_id: Any) -> BrandProfile:
    """Module-level convenience wrapper around :class:`BrandLoader.get_for`."""
    return BrandLoader(profiles).get_for(location_id)
