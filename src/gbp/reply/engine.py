"""Persistence layer for drafts: writes reply_drafts + audit_logs rows.

The engine NEVER writes ``published`` -- the schema's CHECK constraint does not
even allow that status; drafts land as ``draft`` or ``needs_review``.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Sequence

from ..brand import BrandLoader
from .duplicate import DuplicateChecker
from .generator import ReplyGenerator

__all__ = ["ReviewReplyEngine", "DRAFT", "NEEDS_REVIEW"]

DRAFT = "draft"
NEEDS_REVIEW = "needs_review"
#: never written by the engine (kept for the assertion in tests/docs).
PUBLISHED = "published"


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return getattr(row, key, default)


class ReviewReplyEngine:
    """Turns a review row into a persisted draft (never published)."""

    def __init__(
        self,
        repo: Any,
        generator: ReplyGenerator,
        dup_checker: DuplicateChecker,
        brand_loader: BrandLoader,
        actor: str = "system",
    ):
        self.repo = repo
        self.generator = generator
        self.dup_checker = dup_checker
        self.brand_loader = brand_loader
        self.actor = actor

    # ------------------------------------------------------------------ #
    def _resolve_brand_key(self, location_id: Any, location_name: Optional[str]) -> Any:
        """Pick the brand key for this review, staying inside one location."""
        key = location_id
        profile = self.brand_loader.get_for(location_id)
        if getattr(profile, "complete", False):
            return key
        # Brand fixtures are keyed by the GBP location resource name, so fall
        # back to business_locations.location_name for this review's location.
        if not location_name and location_id is not None and hasattr(self.repo, "get_location"):
            row = self.repo.get_location(location_id)
            if row is not None:
                location_name = _get(row, "location_name")
        if location_name:
            alias = self.brand_loader.get_for(location_name)
            if getattr(alias, "complete", False):
                return location_name
        return key

    def process(self, review_row: Any) -> dict:
        """Generate and persist one draft. Returns a result dict."""
        review_id = _get(review_row, "id")
        location_id = _get(review_row, "location_id")
        location_name = _get(review_row, "location_name")
        existing_reply = str(_get(review_row, "reply_text") or "").strip()

        if existing_reply:
            detail = {"review_id": review_id, "reason": "already_has_reply"}
            self.repo.insert_audit_log(
                actor=self.actor,
                action="skipped_existing_reply",
                entity="review",
                entity_id=str(review_id),
                detail_json=json.dumps(detail),
            )
            return {
                "status": "skipped",
                "reason": "already_has_reply",
                "review_id": review_id,
                "draft_id": None,
                "draft_text": existing_reply,
                "meta": None,
            }

        brand_key = self._resolve_brand_key(location_id, location_name)
        recent = self.repo.get_recent_replies(location_id) if location_id is not None else []

        draft_text, meta = self.generator.generate(
            review_row, location_id, recent, brand_key=brand_key
        )

        failed = meta.get("status") == "failed"
        status = (
            DRAFT
            if (not failed and meta.get("status") == "ok" and not meta.get("requires_manual_approval"))
            else NEEDS_REVIEW
        )
        if status == PUBLISHED:  # pragma: no cover - defensive guard
            status = NEEDS_REVIEW

        draft_id = self.repo.insert_reply_draft(
            review_id=review_id,
            draft_text=draft_text or None,
            status=status,
        )

        detail = {
            "review_id": review_id,
            "location_id": location_id,
            "brand_key": meta.get("brand_key"),
            "brand_complete": meta.get("brand_complete"),
            "generation_status": meta.get("status"),
            "error": meta.get("error"),
            "attempts": meta.get("attempts"),
            "stars": meta.get("stars"),
            "is_sensitive": meta.get("is_sensitive"),
            "requires_manual_approval": meta.get("requires_manual_approval"),
            "validation_ok": (meta.get("validation") or {}).get("ok"),
            "validation_failed": (meta.get("validation") or {}).get("failed"),
            "duplicate": (meta.get("duplicate") or {}).get("status"),
            "draft_status": status,
        }
        self.repo.insert_audit_log(
            actor=self.actor,
            action="generation_failed" if failed else "draft_created",
            entity="reply_draft",
            entity_id=str(draft_id),
            detail_json=json.dumps(detail),
        )

        return {
            "status": status,
            "reason": meta.get("error") or meta.get("validation_reason") or "ok",
            "review_id": review_id,
            "draft_id": draft_id,
            "draft_text": draft_text,
            "meta": meta,
        }
