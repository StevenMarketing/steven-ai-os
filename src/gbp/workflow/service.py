"""Approval workflow service: queue, edit, approve, reject, bulk-approve, guard.

Standard library only. Offline. This module NEVER publishes a reply -- there is
no publish call anywhere; ``publish_guard`` only *refuses*.

Every mutation writes exactly ONE ``approval_records`` row and ONE ``audit_logs``
row. Mutations accept an optional ``idempotency_key``; a replay returns the
original result and writes nothing. ``approve`` / ``edit_draft`` also accept an
optional ``expected_version`` for optimistic concurrency: a stale version raises
:class:`ConflictError` and no write happens, so of two concurrent approvers
exactly one wins.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any, Callable, Dict, List, Optional

from ..brand import BrandLoader, BrandProfile
from ..reply.classify import classify
from .states import assert_transition, may_publish

__all__ = [
    "ApprovalService",
    "WorkflowError",
    "NotFoundError",
    "ConflictError",
    "PublishBlocked",
    "text_hash",
]


#: The state a human-approved draft moves to when publish is REQUESTED, and the
#: state a live publish starts from (Issue #126). Reaching ``sent`` from anywhere
#: else is impossible -- see :mod:`gbp.workflow.states`.
PUBLISH_INTENT = "publish-intent"


def text_hash(text: Optional[str]) -> str:
    """A short, stable SHA-256 hash of a draft text (the approval binding).

    Recorded on the ``approval_records`` row at approval time and re-checked
    against the CURRENT text before a live publish, so an edit after approval
    can never be published.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]



# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class WorkflowError(Exception):
    """Base class for approval-workflow errors."""


class NotFoundError(WorkflowError):
    """The requested draft / review / location does not exist (HTTP 404)."""


class ConflictError(WorkflowError):
    """``expected_version`` did not match the current row (HTTP 409)."""


class PublishBlocked(WorkflowError):
    """The draft must not be published (HTTP 423)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _default_clock() -> str:
    """UTC ISO-8601 timestamp with microsecond precision."""
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(
        timespec="microseconds"
    )


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return getattr(row, key, default)


def _jsonable(row: Any) -> Dict[str, Any]:
    return dict(row) if row is not None else {}


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #
class ApprovalService:
    """Human-in-the-loop approval over ``reply_drafts`` rows."""

    def __init__(
        self,
        repo: Any,
        clock: Optional[Callable[[], str]] = None,
        brand_loader: Optional[BrandLoader] = None,
    ):
        self.repo = repo
        self.clock = clock or _default_clock
        self.brand_loader = brand_loader

    # ------------------------------------------------------------------ #
    # internal lookups
    # ------------------------------------------------------------------ #
    def _get_draft(self, draft_id: Any) -> Any:
        row = self.repo.conn.execute(
            "SELECT * FROM reply_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"draft not found: {draft_id!r}")
        return row

    def _get_review(self, review_id: Any) -> Any:
        row = self.repo.conn.execute(
            "SELECT * FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"review not found: {review_id!r}")
        return row

    def _location_name(self, location_id: Any) -> Optional[str]:
        row = self.repo.conn.execute(
            "SELECT location_name FROM business_locations WHERE id = ?",
            (location_id,),
        ).fetchone()
        return _get(row, "location_name")

    def _brand_for(self, review: Any) -> BrandProfile:
        """Brand profile for the review's location (never another shop's)."""
        location_id = _get(review, "location_id")
        location_name = self._location_name(location_id)
        if self.brand_loader is not None:
            profile = self.brand_loader.get_for(location_id)
            if not getattr(profile, "complete", False) and location_name:
                profile = self.brand_loader.get_for(location_name)
            return profile
        # Fall back to a brand_profiles row stored in the DB for this location.
        row = self.repo.conn.execute(
            "SELECT * FROM brand_profiles WHERE location_id = ?", (location_id,)
        ).fetchone()
        if row is None:
            return BrandProfile.neutral(location_id)
        raw = dict(row)
        for key in ("services_json", "seo_keywords_json", "rules_json"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    raw[key] = json.loads(value)
                except (ValueError, TypeError):
                    raw[key] = None
        raw.setdefault("location_id", location_id)
        return BrandProfile.from_dict(raw)

    def _review_has_reply(self, review: Any) -> bool:
        return bool(str(_get(review, "reply_text") or "").strip())

    @staticmethod
    def _auto_publish_eligible(review: Any, brand: BrandProfile):
        """Return ``(eligible, reason)`` for auto-publishing the review's class."""
        c = classify(review)
        if c.stars in (1, 2):
            return False, "low_stars_1_2_requires_manual_approval"
        if c.is_sensitive:
            return False, "sensitive_review_requires_manual_approval"
        if not getattr(brand, "complete", False):
            return False, "missing_brand_profile"
        if not getattr(brand, "auto_publish_allowed", False):
            return False, "no_brand_auto_publish_eligibility"
        return True, ""

    # ------------------------------------------------------------------ #
    # idempotency
    # ------------------------------------------------------------------ #
    def _idem_get(self, key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        row = self.repo.conn.execute(
            "SELECT result_json FROM idempotency_keys WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["result_json"])
        except (ValueError, TypeError):
            return None

    def _idem_put(
        self, key: Optional[str], action: str, entity: str, entity_id: Any, result: dict
    ) -> None:
        if not key:
            return
        self.repo.conn.execute(
            "INSERT OR IGNORE INTO idempotency_keys"
            "(key, action, entity, entity_id, result_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (key, action, entity, str(entity_id), json.dumps(result), self.clock()),
        )

    # ------------------------------------------------------------------ #
    # writes (approval_records + audit_logs)
    # ------------------------------------------------------------------ #
    def _record(
        self,
        draft_id: int,
        action: str,
        actor: str,
        audit_action: str,
        note: Optional[str] = None,
        detail: Optional[dict] = None,
        text_hash: Optional[str] = None,
    ) -> None:
        now = self.clock()
        self.repo.conn.execute(
            "INSERT INTO approval_records(draft_id, action, actor, decided_at,"
            " note, text_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (draft_id, action, actor, now, note, text_hash),
        )
        self.repo.insert_audit_log(
            action=audit_action,
            actor=actor,
            entity="reply_draft",
            entity_id=str(draft_id),
            detail_json=json.dumps(detail or {}, ensure_ascii=False),
        )

    @staticmethod
    def _check_version(draft: Any, expected_version: Optional[int]) -> None:
        """Raise :class:`ConflictError` when ``expected_version`` is stale."""
        if expected_version is None:
            return
        if int(expected_version) != int(draft["version"]):
            raise ConflictError(
                f"stale version for draft {draft['id']}: expected "
                f"{expected_version}, current {draft['version']}"
            )

    def _conditional_update(
        self,
        draft_id: int,
        current_version: int,
        *,
        text: Optional[str] = None,
        status: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> int:
        """UPDATE guarded by ``version``; returns the new version.

        Raises :class:`ConflictError` when the row changed under us.
        """
        if expected_version is not None and int(expected_version) != int(current_version):
            raise ConflictError(
                f"stale version for draft {draft_id}: expected "
                f"{expected_version}, current {current_version}"
            )
        now = self.clock()
        sets = ["updated_at = ?", "version = version + 1"]
        params: List[Any] = [now]
        if text is not None:
            sets.append("draft_text = ?")
            params.append(text)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        params.extend([draft_id, int(current_version)])
        cur = self.repo.conn.execute(
            f"UPDATE reply_drafts SET {', '.join(sets)}"
            " WHERE id = ? AND version = ?",
            params,
        )
        if cur.rowcount != 1:
            raise ConflictError(
                f"concurrent update lost for draft {draft_id} (version {current_version})"
            )
        self.repo.conn.commit()
        return int(current_version) + 1

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def list_queue(
        self,
        location_id: Any = None,
        stars: Optional[int] = None,
        status: Optional[str] = None,
        language: Optional[str] = None,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return draft rows (joined to review + location), filtered."""
        sql = [
            "SELECT d.id AS draft_id, d.review_id AS review_id, d.draft_text,"
            " d.status, d.version, d.created_at, d.updated_at,"
            " r.location_id, r.rating, r.comment, r.reviewer_name, r.review_name,"
            " r.reply_text, r.update_time,"
            " l.location_name, l.title, l.city, l.language"
            " FROM reply_drafts d"
            " JOIN reviews r ON d.review_id = r.id"
            " LEFT JOIN business_locations l ON r.location_id = l.id"
        ]
        where: List[str] = []
        params: List[Any] = []
        if location_id is not None:
            where.append("(r.location_id = ? OR l.location_name = ?)")
            params.extend([location_id, location_id])
        if stars is not None:
            where.append("r.rating = ?")
            params.append(int(stars))
        if status is not None:
            where.append("d.status = ?")
            params.append(status)
        if language is not None:
            where.append("l.language = ?")
            params.append(language)
        if since is not None:
            where.append("d.updated_at >= ?")
            params.append(since)
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY d.id")
        rows = self.repo.conn.execute(" ".join(sql), params).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["has_reply"] = bool(str(item.get("reply_text") or "").strip())
            classification = classify(row)
            item["is_sensitive"] = classification.is_sensitive
            # Human-readable reason(s) this draft needs attention (low stars,
            # rating-only, sensitive signals) -- surfaced in the web UI.
            item["reason"] = classification.reason
            out.append(item)
        return out

    def dashboard_counts(self) -> Dict[str, int]:
        """Count drafts per status (all workflow states present, 0 default)."""
        counts = {
            "draft": 0,
            "needs_review": 0,
            "approved": 0,
            "rejected": 0,
            "ready_to_publish": 0,
            "published": 0,
            "failed": 0,
        }
        for row in self.repo.conn.execute(
            "SELECT status, COUNT(*) AS n FROM reply_drafts GROUP BY status"
        ).fetchall():
            counts[str(row["status"])] = int(row["n"])
        counts["total"] = sum(
            v for k, v in counts.items() if k != "total"
        )
        return counts

    def list_locations(self) -> List[Dict[str, Any]]:
        """Locations with brand-completeness flags."""
        rows = self.repo.conn.execute(
            "SELECT * FROM business_locations ORDER BY id"
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            brand = self._brand_for({"location_id": row["id"], "rating": None,
                                     "comment": None})
            out.append(
                {
                    "id": row["id"],
                    "account_id": row["account_id"],
                    "location_name": row["location_name"],
                    "title": row["title"],
                    "city": row["city"],
                    "state": row["state"],
                    "language": row["language"],
                    "brand_complete": bool(getattr(brand, "complete", False)),
                    "auto_publish_allowed": bool(
                        getattr(brand, "auto_publish_allowed", False)
                    ),
                    "has_city": bool(getattr(brand, "has_city", False)),
                    "has_services": bool(getattr(brand, "has_services", False)),
                    "has_seo_keywords": bool(
                        getattr(brand, "has_seo_keywords", False)
                    ),
                }
            )
        return out

    def get_audit(
        self,
        entity: Optional[str] = None,
        entity_id: Optional[str] = None,
        actor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        sql = ["SELECT * FROM audit_logs"]
        where: List[str] = []
        params: List[Any] = []
        if entity is not None:
            where.append("entity = ?")
            params.append(entity)
        if entity_id is not None:
            where.append("entity_id = ?")
            params.append(str(entity_id))
        if actor is not None:
            where.append("actor = ?")
            params.append(actor)
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY id")
        if limit is not None:
            sql.append("LIMIT ?")
            params.append(int(limit))
        rows = self.repo.conn.execute(" ".join(sql), params).fetchall()
        # Audit V2: expose the stable field set the UI renders -- actor, action,
        # entity, entity_id, ts and detail. ``detail`` aliases ``detail_json``
        # (additive + backward compatible: rows missing a column just yield
        # None). Audit records are NEVER modified or deleted here.
        out: List[Dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            row.setdefault("ts", None)
            row["detail"] = row.get("detail_json")
            out.append(row)
        return out

    def get_approval_history(self, draft_id: int) -> List[Dict[str, Any]]:
        """Approval records for one draft, oldest first (the edit history)."""
        rows = self.repo.conn.execute(
            "SELECT * FROM approval_records WHERE draft_id = ? ORDER BY id",
            (draft_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # mutations
    # ------------------------------------------------------------------ #
    def edit_draft(
        self,
        draft_id: int,
        text: str,
        actor: str,
        *,
        expected_version: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Replace a draft's text. Status stays, except ``rejected -> needs_review``."""
        cached = self._idem_get(idempotency_key)
        if cached is not None:
            return cached

        draft = self._get_draft(draft_id)
        self._check_version(draft, expected_version)
        new_status = "needs_review" if draft["status"] == "rejected" else draft["status"]
        version = self._conditional_update(
            draft_id,
            draft["version"],
            text=text,
            status=new_status,
            expected_version=expected_version,
        )
        self._record(
            draft_id,
            "edit",
            actor,
            "draft_edited",
            detail={"status": new_status, "version": version},
        )
        self.repo.conn.commit()
        result = {
            "draft_id": draft_id,
            "status": new_status,
            "version": version,
            "draft_text": text,
            "actor": actor,
        }
        self._idem_put(idempotency_key, "edit", "reply_draft", draft_id, result)
        self.repo.conn.commit()
        return result

    def approve(
        self,
        draft_id: int,
        actor: str,
        *,
        expected_version: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Approve a draft; ``ready_to_publish`` when the brand allows it."""
        cached = self._idem_get(idempotency_key)
        if cached is not None:
            return cached

        draft = self._get_draft(draft_id)
        self._check_version(draft, expected_version)
        review = self._get_review(draft["review_id"])
        brand = self._brand_for(review)
        eligible, _reason = self._auto_publish_eligible(review, brand)

        if not self._review_has_reply(review) and eligible:
            target = "ready_to_publish"
        else:
            target = "approved"

        assert_transition(draft["status"], target)
        version = self._conditional_update(
            draft_id,
            draft["version"],
            status=target,
            expected_version=expected_version,
        )
        self._record(
            draft_id,
            "approve",
            actor,
            "draft_approved",
            detail={"status": target, "version": version, "auto_publish_eligible": eligible},
            text_hash=text_hash(draft["draft_text"]),
        )
        self.repo.conn.commit()
        result = {
            "draft_id": draft_id,
            "status": target,
            "version": version,
            "auto_publish_eligible": eligible,
            "actor": actor,
        }
        self._idem_put(idempotency_key, "approve", "reply_draft", draft_id, result)
        self.repo.conn.commit()
        return result

    def reject(
        self,
        draft_id: int,
        actor: str,
        note: str = "",
        *,
        expected_version: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reject a draft."""
        cached = self._idem_get(idempotency_key)
        if cached is not None:
            return cached

        draft = self._get_draft(draft_id)
        self._check_version(draft, expected_version)
        assert_transition(draft["status"], "rejected")
        version = self._conditional_update(
            draft_id,
            draft["version"],
            status="rejected",
            expected_version=expected_version,
        )
        self._record(
            draft_id, "reject", actor, "draft_rejected", note=note or None,
            detail={"status": "rejected", "version": version, "note": note},
        )
        self.repo.conn.commit()
        result = {
            "draft_id": draft_id,
            "status": "rejected",
            "version": version,
            "note": note,
            "actor": actor,
        }
        self._idem_put(idempotency_key, "reject", "reply_draft", draft_id, result)
        self.repo.conn.commit()
        return result

    def bulk_approve(self, ids: List[int], actor: str) -> Dict[str, Any]:
        """Approve many drafts, skipping the ones that require manual review.

        Returns ``{"approved": [...], "skipped": [{"id", "reason"}, ...]}``.
        Skips (never approves): 1-2 star reviews, sensitive reviews, reviews with
        no brand profile, and reviews with no brand auto-publish eligibility.
        """
        approved: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for draft_id in ids:
            try:
                draft = self._get_draft(draft_id)
            except NotFoundError:
                skipped.append({"id": draft_id, "reason": "draft_not_found"})
                continue
            review = self._get_review(draft["review_id"])
            brand = self._brand_for(review)
            eligible, reason = self._auto_publish_eligible(review, brand)
            if not eligible:
                skipped.append({"id": draft_id, "reason": reason})
                continue
            try:
                result = self.approve(draft_id, actor)
            except (WorkflowError, Exception) as exc:  # noqa: BLE001
                skipped.append({"id": draft_id, "reason": f"approve_failed:{exc}"})
                continue
            approved.append(result)
        return {"approved": approved, "skipped": skipped}

    def publish_guard(self, draft_id: int) -> Dict[str, Any]:
        """Dry-run: raise :class:`PublishBlocked` when the draft must not publish.

        This performs NO writes and never publishes anything.
        """
        draft = self._get_draft(draft_id)
        state = draft["status"]
        if not may_publish(state):
            raise PublishBlocked(f"state_not_publishable:{state}")
        review = self._get_review(draft["review_id"])
        if self._review_has_reply(review):
            raise PublishBlocked("review_already_has_reply")
        brand = self._brand_for(review)
        if not getattr(brand, "auto_publish_allowed", False):
            raise PublishBlocked("brand_auto_publish_disabled")
        c = classify(review)
        if c.stars in (1, 2):
            raise PublishBlocked("low_stars_1_2")
        if c.is_sensitive:
            raise PublishBlocked("sensitive_review")
        return {
            "draft_id": draft_id,
            "publishable": True,
            "status": state,
            "brand_auto_publish_allowed": True,
        }

    def request_publish(
        self,
        draft_id: int,
        actor: str,
        *,
        expected_version: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a human's publish REQUEST: ``approved`` -> ``publish-intent``.

        This is the only edge into ``publish-intent`` and therefore the only way
        a reply can ever become ``sent`` (Issue #126). It performs NO network
        call -- the live PUT is done by
        :class:`gbp.replies.publish.PublishService` afterwards, behind its own
        gates.

        Idempotent: re-requesting an already ``publish-intent`` draft returns the
        current state and writes nothing; a ``sent`` draft returns the recorded
        result. A draft that is NOT human-approved (``approved`` /
        ``ready_to_publish``) is refused with :class:`PublishBlocked`.
        """
        cached = self._idem_get(idempotency_key)
        if cached is not None:
            return cached

        draft = self._get_draft(draft_id)
        state = str(draft["status"])
        if state == "sent":
            # Already published: a re-request is a no-op, never a second write.
            return {"draft_id": draft_id, "status": "sent", "published": True,
                    "idempotent": True}
        if state == PUBLISH_INTENT:
            return {"draft_id": draft_id, "status": PUBLISH_INTENT,
                    "version": int(draft["version"]), "idempotent": True}
        if not may_publish(state):
            raise PublishBlocked(f"state_not_publishable:{state}")

        self._check_version(draft, expected_version)
        assert_transition(state, PUBLISH_INTENT)
        version = self._conditional_update(
            draft_id, draft["version"], status=PUBLISH_INTENT,
            expected_version=expected_version)
        self._record(
            draft_id, "request_publish", actor, "publish_requested",
            detail={"status": PUBLISH_INTENT, "version": version,
                    "from": state})
        self.repo.conn.commit()
        result = {"draft_id": draft_id, "status": PUBLISH_INTENT,
                  "version": version, "actor": actor}
        self._idem_put(idempotency_key, "request_publish", "reply_draft",
                       draft_id, result)
        self.repo.conn.commit()
        return result
