"""End-to-end offline chain: fixture sync -> draft -> edit -> approve -> refused publish.

The chain reuses the existing offline pieces instead of reimplementing them:

* :class:`gbp.sync.fixture_sync.FixtureSync` -- the sync / dedup / idempotent
  upsert step (no network, guard client never touched).
* :class:`gbp.workflow.service.ApprovalService` -- the draft / edit / approve /
  reject / publish-guard state machine (human approval stays mandatory).

Guarantees
----------
* **Offline by construction.** The module imports no HTTP client. A guard
  client (a transport that raises on any request) can be handed in; a test
  proves the run never touches it.
* **Idempotent across the seam.** Re-running the identical chain writes no new
  review rows, no second draft and no duplicate audit entries. Mutations go
  through the approval service's ``idempotency_key`` so a replay is a true
  no-op (its cached result is returned and nothing is written).
* **No second draft on resync.** A review has at most one draft; a resync that
  updates a review in place keeps the same review id, so the draft is not
  duplicated.
* **Multi-location isolation.** Replies stay bound to the review's own
  location; a review from one location is never drafted or approved against
  another.
* **Approval gate preserved.** An approval names the approver and the exact
  revision; a stale (superseded) approval is refused; an edit after approval
  invalidates it and the reply drops back to a draft state.
* **Publish is refused end to end.** There is no code path that publishes or
  sends anything: :meth:`ReviewReplyChain.publish_request` always refuses with
  a clear error and performs no network call.
* **Append-only, redaction-safe audit.** One entry per transition across both
  layers (``review_sync`` and ``reply_draft`` agents) with ids, state names,
  actor, revision and text hashes -- and only synthetic fixture data.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from ..sync.fixture_sync import FixtureError, FixtureSync, normalize_review
from ..workflow.service import (
    ApprovalService,
    ConflictError,
    PublishBlocked,
)
from ..workflow.states import may_publish

__all__ = ["ChainError", "OfflineGuard", "ReviewReplyChain", "run_chain"]

#: audit entity names -- one per layer of the chain.
SYNC_ENTITY = "review_sync"
REPLY_ENTITY = "reply_draft"

#: a short, stable hash of a draft text (used as the revision / binding token).
def _text_hash(text: Optional[str]) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


class ChainError(Exception):
    """The chain input is unusable (e.g. a malformed fixture dataset)."""


class OfflineGuard:
    """A client whose transport raises on *any* request.

    Hand it to the chain as ``client=``. If any code path ever tried to reach
    the network the transport would raise; a test also asserts ``calls == 0``
    after a full run.
    """

    def __init__(self) -> None:
        self.calls = 0

    def transport(self):  # noqa: D401 - mirrors the real client surface
        outer = self

        class _RaisingTransport:
            def request(self, *args: Any, **kwargs: Any) -> Any:
                outer.calls += 1
                raise AssertionError("network access attempted in the offline chain")

        return _RaisingTransport()


class ReviewReplyChain:
    """Drive one synced review through draft -> edit -> approve -> refused publish."""

    def __init__(
        self,
        repo: Any,
        *,
        brand_loader: Optional[Any] = None,
        client: Optional[Any] = None,
        clock: Optional[Any] = None,
    ) -> None:
        self.repo = repo
        self.client = client
        self.sync = FixtureSync(repo, client=client)
        self.service = ApprovalService(repo, clock=clock, brand_loader=brand_loader)
        #: live Google access is disabled in this offline chain -- publish can
        #: never be reached, whatever the caller does.
        self.live_enabled = False

    # ------------------------------------------------------------------ #
    # audit (append-only, both layers)
    # ------------------------------------------------------------------ #
    def _audit(self, action: str, entity: str, entity_id: Any,
               actor: Optional[str], detail: Dict[str, Any]) -> None:
        self.repo.insert_audit_log(
            action=action,
            actor=actor,
            entity=entity,
            entity_id=str(entity_id),
            detail_json=json.dumps(detail, ensure_ascii=False, sort_keys=True),
        )
        self.repo.conn.commit()

    def audit_entries(self) -> List[Dict[str, Any]]:
        """The full append-only trail (sync layer + reply layer), oldest first."""
        rows = self.repo.conn.execute(
            "SELECT * FROM audit_logs WHERE entity IN (?, ?) ORDER BY id",
            (SYNC_ENTITY, REPLY_ENTITY),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # steps
    # ------------------------------------------------------------------ #
    @staticmethod
    def validate_dataset(dataset: Any) -> Dict[str, Any]:
        """Refuse a malformed dataset *before* anything is written."""
        if not isinstance(dataset, dict):
            raise ChainError("dataset must be a JSON object")
        locations = dataset.get("locations")
        if not isinstance(locations, list):
            raise ChainError("dataset must have a 'locations' list")
        return dataset

    @staticmethod
    def load_dataset(path: str) -> Dict[str, Any]:
        """Read a synthetic dataset from a JSON file (offline, no network)."""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                dataset = json.load(handle)
        except (OSError, ValueError) as exc:  # ValueError covers json.JSONDecodeError
            raise ChainError(f"dataset could not be read: {exc}") from exc
        return ReviewReplyChain.validate_dataset(dataset)

    def _sync_step(self, dataset: Dict[str, Any]) -> Dict[str, Any]:
        try:
            summary = self.sync.sync_dataset(dataset)
        except FixtureError as exc:
            raise ChainError(str(exc)) from exc

        # one audit entry per location that actually changed -- a replay that
        # changes nothing writes none, so the trail stays idempotent.
        for location, counts in summary["locations"].items():
            if counts.get("created", 0) == 0 and counts.get("updated", 0) == 0:
                continue
            self._audit(
                "sync_applied", SYNC_ENTITY, location, actor="offline-sync",
                detail={
                    "layer": "sync",
                    "location": location,
                    "created": counts.get("created", 0),
                    "updated": counts.get("updated", 0),
                    "duplicates": counts.get("duplicates", 0),
                    "errors": counts.get("errors", 0),
                    "stored": counts.get("stored", 0),
                },
            )
        return summary

    @staticmethod
    def _default_review_name(dataset: Dict[str, Any]) -> str:
        """The first valid review in dataset order -- deterministic."""
        for entry in dataset.get("locations", []):
            if not isinstance(entry, dict):
                continue
            location = entry.get("location")
            if not isinstance(location, str) or not location.strip():
                continue
            for raw in entry.get("reviews", []) or []:
                try:
                    record = normalize_review(raw, location)
                except FixtureError:
                    continue
                return record["review_name"]
        raise ChainError("dataset carries no usable review to draft")

    def _review_row(self, review_name: str):
        row = self.repo.conn.execute(
            "SELECT * FROM reviews WHERE review_name = ?", (review_name,)
        ).fetchone()
        if row is None:
            raise ChainError(f"review not synced: {review_name}")
        return row

    def _draft_row(self, review_id: int):
        return self.repo.conn.execute(
            "SELECT * FROM reply_drafts WHERE review_id = ? ORDER BY id LIMIT 1",
            (review_id,),
        ).fetchone()

    def _draft_step(self, review_id: int, review_name: str, text: str,
                    actor: str) -> Dict[str, Any]:
        existing = self._draft_row(review_id)
        if existing is not None:
            return {"created": False, "draft_id": int(existing["id"]),
                    "status": existing["status"], "version": int(existing["version"])}
        draft_id = self.repo.insert_reply_draft(review_id, draft_text=text, status="draft")
        self._audit(
            "draft_created", REPLY_ENTITY, draft_id, actor=actor,
            detail={"layer": "reply", "review_name": review_name, "status": "draft",
                    "revision": 1, "text_hash": _text_hash(text)},
        )
        return {"created": True, "draft_id": int(draft_id), "status": "draft", "version": 1}

    # ------------------------------------------------------------------ #
    # the whole chain
    # ------------------------------------------------------------------ #
    def run(
        self,
        dataset: Any = None,
        *,
        path: Optional[str] = None,
        review_name: Optional[str] = None,
        draft_text: str = "Thank you for the feedback, we appreciate it!",
        edited_text: Optional[str] = "Thank you so much for visiting us -- we hope to see you again!",
        actor: str = "offline-reviewer",
        approver: Optional[str] = "owner",
    ) -> Dict[str, Any]:
        """Run the full offline chain and return a machine-readable summary.

        Steps: load fixture dataset -> sync (dedup + idempotent upsert) ->
        select a synced review -> create a draft -> edit -> approve -> request
        publish (always refused). No network call is made at any step.
        """
        approver = approver or actor
        if path is not None:
            dataset = self.load_dataset(path)
        else:
            dataset = self.validate_dataset(dataset)
            if not dataset.get("locations"):
                raise ChainError("dataset has no locations")

        summary: Dict[str, Any] = {"steps": []}

        sync_summary = self._sync_step(dataset)
        summary["sync"] = sync_summary
        summary["steps"].append({"step": "sync", "totals": sync_summary["totals"]})

        name = review_name or self._default_review_name(dataset)
        review = self._review_row(name)
        review_id = int(review["id"])
        location = str(review["review_name"]).split("/reviews/")[0]
        summary["review"] = {"review_name": name, "location": location,
                             "review_id": review_id}
        summary["steps"].append({"step": "select_review", "location": location})

        drafted = self._draft_step(review_id, name, draft_text, actor)
        draft_id = drafted["draft_id"]
        summary["steps"].append({"step": "create_draft", **drafted})

        # -- edit (idempotent: same text -> cache hit, nothing written) ----- #
        edit_result = None
        if edited_text is not None:
            current = self._draft_row(review_id)
            key = f"chain:{name}:edit:{_text_hash(edited_text)}"
            edit_result = self.service.edit_draft(
                draft_id, edited_text, actor,
                expected_version=int(current["version"]), idempotency_key=key,
            )
        summary["steps"].append({"step": "edit", "result": edit_result})

        # -- approve, bound to the exact revision and text ------------------ #
        current = self._draft_row(review_id)
        approve_key = f"chain:{name}:approve:{_text_hash(current['draft_text'])}"
        approval = self.service.approve(
            draft_id, approver,
            expected_version=int(current["version"]), idempotency_key=approve_key,
        )
        summary["steps"].append({"step": "approve", "result": approval})

        publish = self.publish_request(draft_id)
        summary["steps"].append({"step": "publish", "result": publish})
        summary["published"] = False
        summary["ok"] = publish["published"] is False
        return summary

    # ------------------------------------------------------------------ #
    # gates
    # ------------------------------------------------------------------ #
    def approve_exact(self, draft_id: int, approver: str,
                      expected_version: int) -> Dict[str, Any]:
        """Approve a *specific* revision; a stale revision is refused."""
        return self.service.approve(draft_id, approver,
                                    expected_version=int(expected_version))

    def edit_after_approval(self, draft_id: int, text: str, actor: str) -> Dict[str, Any]:
        """Edit a draft that is already approved: the approval is invalidated.

        The state machine has no ``approved -> draft`` edge, so the chain
        invalidates the approval explicitly (reject) and then applies the edit,
        which lands the reply in the ``needs_review`` draft state. The result is
        never publishable again until a fresh human approval.
        """
        draft = self.service.repo.conn.execute(
            "SELECT * FROM reply_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if draft is None:
            raise ChainError(f"draft not found: {draft_id!r}")
        if may_publish(draft["status"]):
            self.service.reject(draft_id, actor, note="invalidated by edit after approval")
            self._audit(
                "approval_invalidated", REPLY_ENTITY, draft_id, actor=actor,
                detail={"layer": "reply", "reason": "edit_after_approval",
                        "from_state": draft["status"]},
            )
        result = self.service.edit_draft(draft_id, text, actor)
        return result

    def request_publish(self, draft_id: int) -> Dict[str, Any]:
        """Alias kept for readability -- always refused while live is disabled."""
        return self.publish_request(draft_id)

    def publish_request(self, draft_id: int) -> Dict[str, Any]:
        """Refuse to publish. Never performs a network call.

        While :attr:`live_enabled` is False (always, in this offline chain) the
        request is refused with a clear error. Even if it were enabled, the
        service's :meth:`publish_guard` is a dry-run that raises rather than
        publishing anything.
        """
        if not self.live_enabled:
            return {"published": False, "refused": True,
                    "reason": "live_access_disabled", "draft_id": draft_id}
        try:
            return {"published": False, "refused": False,
                    "draft_id": draft_id, **self.service.publish_guard(draft_id)}
        except PublishBlocked as exc:
            return {"published": False, "refused": True,
                    "reason": str(exc), "draft_id": draft_id}

    # ------------------------------------------------------------------ #
    # redaction
    # ------------------------------------------------------------------ #
    def redaction_check(self) -> List[str]:
        """Return the audit entries that carry suspicious (credential-shaped) keys.

        An empty list means the trail is clean. ``detail`` is scanned as text;
        the chain only ever stores ids, states, actor, revision and hashes.
        """
        bad: List[str] = []
        markers = ("token", "secret", "password", "api_key", "apikey",
                   "credential", "authorization", "bearer")
        for row in self.audit_entries():
            blob = json.dumps(row, ensure_ascii=False).lower()
            if any(m in blob for m in markers):
                bad.append(str(row.get("id")))
        return bad


def run_chain(repo: Any, dataset: Any = None, *, path: Optional[str] = None,
              brand_loader: Optional[Any] = None, client: Optional[Any] = None,
              clock: Optional[Any] = None, **kwargs: Any) -> Dict[str, Any]:
    """Convenience: build a :class:`ReviewReplyChain` and run it once.

    The whole offline chain runs from fixture JSON to a refused publish and
    returns a machine-readable summary of every step. No network is touched.
    """
    chain = ReviewReplyChain(repo, brand_loader=brand_loader, client=client, clock=clock)
    summary = chain.run(dataset, path=path, **kwargs)
    summary["audit"] = chain.audit_entries()
    return summary
