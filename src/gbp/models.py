"""Plain dataclasses mirroring the SQLite schema.

These are lightweight, dependency-free value objects. Every field that maps to
a table column uses the same name so rows can be built from ``sqlite3.Row``
with a simple dict comprehension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Account:
    id: Optional[int] = None
    name: str = ""
    created_at: Optional[str] = None


@dataclass
class Location:
    id: Optional[int] = None
    account_id: Optional[int] = None
    location_name: str = ""
    title: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    language: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class Review:
    id: Optional[int] = None
    location_id: Optional[int] = None
    review_name: str = ""
    review_id: Optional[str] = None
    rating: Optional[int] = None
    comment: Optional[str] = None
    reviewer_name: Optional[str] = None
    create_time: Optional[str] = None
    update_time: Optional[str] = None
    reply_text: Optional[str] = None
    reply_update_time: Optional[str] = None
    status: Optional[str] = None
    raw_json: Optional[str] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None


@dataclass
class BrandProfile:
    location_id: Optional[int] = None
    business_name: Optional[str] = None
    city: Optional[str] = None
    services_json: Optional[str] = None
    language: Optional[str] = None
    tone: Optional[str] = None
    seo_keywords_json: Optional[str] = None
    rules_json: Optional[str] = None
    auto_publish_allowed: int = 0
    updated_at: Optional[str] = None


@dataclass
class ReplyDraft:
    id: Optional[int] = None
    review_id: Optional[int] = None
    draft_text: Optional[str] = None
    status: str = "draft"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None