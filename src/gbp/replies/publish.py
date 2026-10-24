"""Live reply publishing behind the human approval gate (Issue #126).

This is the FIRST Google WRITE in the whole system, so every safeguard below
exists on purpose. The module is deliberately paranoid:

* a publish starts **only** from the ``publish-intent`` state, which itself is
  reachable only from a state a human already approved
  (``approved`` / ``ready_to_publish``) -- see :mod:`gbp.workflow.states`;
* the approval must still bind the EXACT current draft text (a hash recorded at
  approval time); any edit after approval makes the hash mismatch and the
  publish is refused BEFORE any network call;
* the review must belong to a ``source='live'`` location owned by the caller's
  tenant (the tenant is derived server-side, never from the request body);
* the network call is made by an INJECTED publisher; when none is supplied the
  service fails closed with :class:`PublishDisabled` and writes nothing
  (the offline default is unchanged);
* a Google failure leaves the state at ``publish-intent`` and records a SAFE
  reason only -- the reply is never marked ``sent``;
* publishing is idempotent per draft: a second publish of an already-``sent``
  draft returns the recorded result and performs NO second PUT.

Offline and stdlib only: no socket is ever opened here, the publisher is
injected (the wiring hands in the ``AuthorizedHttp``-backed client).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from gbp.client.gbp_client import GbpClient
from gbp.oauth.tenancy import normalize_tenant
from gbp.oauth.vault import TokenVault
from gbp.sync.live_sync import AuthorizedHttp
from gbp.sync.pacing import DEFAULT_QPM, TokenBucket
from gbp.sync.persist import safe_reason
from gbp.workflow.service import PublishBlocked, text_hash
from gbp.workflow.states import assert_transition

__all__ = [
    "PublishDisabled",
    "PublishService",
    "LiveReplyPublisher",
    "PUBLISH_INTENT",
    "SENT",
]

#: The single state a live publish may start from.
PUBLISH_INTENT = "publish-intent"
#: The terminal state of a reply that really was posted to Google.
SENT = "sent"

#: Audit entity, matching the reply-draft vocabulary the dashboard already reads.
AUDIT_ENTITY = "reply_draft"


class PublishDisabled(RuntimeError):
    """Live publishing is not available for this call -- fail closed.

    Raised when the publish service is asked to publish without a live
    publisher (the offline default). It is never a partial success: the caller
    sees an honest refusal and NOTHING is written.
    """


class PublishService:
    """Enforce every publish gate, in order, then (and only then) PUT.

    ``publisher`` is a callable ``(review_name, comment) -> response dict``.
    It is the ONLY thing that can reach the network, and it is injected -- so a
    test can pass a recording fake and prove the gate matrix produces zero
    writes, and the wiring can pass the ``AuthorizedHttp``-backed client.
    """

    def __init__(self, repo: Any, service: Any, *,
                 clock: Optional[Callable[[], str]] = None) -> None:
        self.repo = repo
        #: The canonical approval service (draft/review lookups + recording).
        self.service = service
        self._clock = clock or service.clock

    # -- helpers ----------------------------------------------------------- #
    def _now(self) -> str:
        return self._clock()

    def _location_row(self, location_id: Any):
        return self.repo.conn.execute(
            "SELECT * FROM business_locations WHERE id = ?", (location_id,)
        ).fetchone()

    def _latest_approval(self, draft_id: int):
        # The LATEST row that actually binds a text hash: a later bookkeeping row
        # (e.g. the request_publish marker, which carries no hash) must never
        # shadow the approval it followed.
        return self.repo.conn.execute(
            "SELECT * FROM approval_records WHERE draft_id = ?"
            " AND text_hash IS NOT NULL ORDER BY id DESC LIMIT 1", (draft_id,)
        ).fetchone()

    @staticmethod
    def _field(row: Any, key: str, default: Any = None) -> Any:
        if row is None:
            return default
        try:
            return row[key]
        except (TypeError, KeyError, IndexError):
            return getattr(row, key, default)

    def _audit(self, draft_id: int, actor: Optional[str], action: str,
               detail: Dict[str, Any]) -> None:
        self.repo.insert_audit_log(
            action=action, actor=actor, entity=AUDIT_ENTITY,
            entity_id=str(draft_id),
            detail_json=json.dumps(detail, ensure_ascii=False, sort_keys=True),
        )
        self.repo.conn.commit()

    def _refuse(self, draft_id: int, reason: str, state: str,
                actor: Optional[str] = None) -> Dict[str, Any]:
        """A gate refused the publish. Zero network calls; state is untouched."""
        self._audit(draft_id, actor, "publish_refused",
                    {"layer": "reply", "reason": reason, "state": state})
        return {"ok": False, "draft_id": draft_id, "state": state,
                "reason": reason, "published": False}

    def _recorded(self, draft_id: int) -> Optional[Dict[str, Any]]:
        """The result recorded by a previous successful publish (or None)."""
        return self.service._idem_get(self._result_key(draft_id))

    @staticmethod
    def _result_key(draft_id: int) -> str:
        return f"publish-result:{int(draft_id)}"

    # -- gates ------------------------------------------------------------- #
    def approval_matches(self, draft_id: int, text: str) -> bool:
        """True only when the recorded approval binds ``text`` EXACTLY.

        Fails closed: no approval row, or one without a recorded hash (a row
        written before Issue #126), is a mismatch.
        """
        row = self._latest_approval(draft_id)
        if row is None:
            return False
        stored = self._field(row, "text_hash")
        if not stored:
            return False
        return str(stored) == text_hash(text)

    # -- the publish ------------------------------------------------------- #
    def publish(
        self,
        draft_id: int,
        actor: str,
        *,
        tenant: Any,
        publisher: Optional[Callable[[str, str], Any]] = None,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Publish ONE approved reply, or refuse honestly.

        Gate order (all BEFORE any network call):

        1. the draft is in ``publish-intent`` (a human approved AND requested
           publish);
        2. the recorded approval hash still matches the CURRENT draft text;
        3. the review belongs to a ``source='live'`` location of ``tenant``;
        4. idempotency: an already-``sent`` draft returns the recorded result
           with NO second PUT.

        On success the draft becomes ``sent``; on a Google failure the state
        STAYS ``publish-intent`` and a safe reason is recorded -- never marked
        sent.
        """
        if publisher is None:
            # No live publisher injected -> fail closed, offline unchanged.
            raise PublishDisabled(
                "live reply publishing is disabled; no publisher is configured")

        draft = self.service._get_draft(draft_id)          # NotFoundError -> 404
        state = str(self._field(draft, "status"))
        version = int(self._field(draft, "version") or 1)

        # -- gate 4 first: an already-sent revision is a true no-op --------- #
        if state == SENT:
            recorded = self._recorded(draft_id)
            result: Dict[str, Any] = {"ok": True, "draft_id": draft_id,
                                     "state": SENT, "published": True,
                                     "idempotent": True}
            if recorded:
                result.update({k: v for k, v in recorded.items()
                               if k not in ("ok", "idempotent")})
                result["idempotent"] = True
            return result

        # -- gate 1: the state must be publish-intent ---------------------- #
        if state != PUBLISH_INTENT:
            return self._refuse(draft_id, "state_not_publish_intent", state, actor)

        # -- gate 2: the approval must still bind the current text --------- #
        text = str(self._field(draft, "draft_text") or "")
        if not self.approval_matches(draft_id, text):
            return self._refuse(draft_id, "approval_text_mismatch", state, actor)

        # -- gate 3: a live location of the caller's tenant ---------------- #
        review = self.service._get_review(self._field(draft, "review_id"))
        location = self._location_row(self._field(review, "location_id"))
        if location is None:
            return self._refuse(draft_id, "review_location_missing", state, actor)
        source = str(self._field(location, "source") or "")
        owner = self._field(location, "tenant_id")
        if source != "live" or not owner or str(owner) != str(normalize_tenant(tenant)):
            return self._refuse(draft_id, "review_not_live_for_tenant", state, actor)

        # The full v4 resource name the Inc 3 sync stored (composed from the
        # account + location short names); the reply PUT appends ``/reply``.
        review_name = str(self._field(review, "review_name") or "")
        if not review_name:
            return self._refuse(draft_id, "review_name_missing", state, actor)

        # -- the ONE network call ------------------------------------------ #
        try:
            response = publisher(review_name, text)
        except Exception as exc:  # noqa: BLE001 - report honestly, leak nothing
            reason = safe_reason(exc)
            self._audit(draft_id, actor, "publish_failed",
                        {"layer": "reply", "review_name": review_name,
                         "reason": reason})
            return {"ok": False, "draft_id": draft_id, "state": PUBLISH_INTENT,
                    "reason": reason, "published": False}

        # -- success: publish-intent -> sent (the only edge into ``sent``) -- #
        assert_transition(state, SENT)
        new_version = self.service._conditional_update(
            draft_id, version, status=SENT, expected_version=expected_version)
        update_time = None
        if isinstance(response, dict):
            update_time = response.get("updateTime")
            reply = response.get("reviewReply")
            if not update_time and isinstance(reply, dict):
                update_time = reply.get("updateTime")
        if not update_time:
            update_time = self._now()
        # Persist Google's own reply metadata on the review row so the dashboard
        # shows the posted reply and a later re-sync (which may omit the reply)
        # never resurrects the review as unanswered.
        self.repo.conn.execute(
            "UPDATE reviews SET reply_text = ?, reply_update_time = ? WHERE id = ?",
            (text, update_time, self._field(review, "id")))
        self.service._record(
            draft_id, "publish", actor, "reply_published",
            detail={"layer": "reply", "review_name": review_name,
                    "status": SENT, "version": new_version,
                    "update_time": update_time})
        result = {"ok": True, "draft_id": draft_id, "state": SENT,
                  "published": True, "review_name": review_name,
                  "update_time": update_time, "version": new_version}
        self.repo.conn.commit()
        # Recorded so a second publish of this revision is a NO-OP.
        self.service._idem_put(self._result_key(draft_id), "publish",
                              AUDIT_ENTITY, draft_id, result)
        self.repo.conn.commit()
        return result


class LiveReplyPublisher:
    """The wired, ``AuthorizedHttp``-backed publisher (mirrors ``LiveReviewSync``).

    Built the same way the Inc 3 sync engines are: an ``AuthorizedHttp`` over an
    ``HttpClient`` whose token provider refreshes through the SAME guard-passing
    transport the code exchange uses, with the shared pacing bucket. It exposes
    a single ``publish(review_name, comment)`` that calls the existing
    :meth:`GbpClient.update_reply` -- the ONE Google write.
    """

    def __init__(
        self,
        vault: TokenVault,
        tenant: Any,
        *,
        transport: Any = None,
        http: Any = None,
        quota: Any = None,
        base: Optional[str] = None,
        qpm: int = DEFAULT_QPM,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        self.vault = vault
        self.tenant = normalize_tenant(tenant)
        self._transport = transport
        self.quota = quota if quota is not None else TokenBucket(qpm)
        if http is None:
            from gbp.client.http_client import HttpClient
            from gbp.client.urllib_transport import UrllibTransport
            http = HttpClient(transport if transport is not None else UrllibTransport())
        self._authorized = AuthorizedHttp(
            http, lambda: self.vault.get_valid(self.tenant, self._transport))
        kwargs: Dict[str, Any] = {"http": self._authorized, "quota": self.quota}
        if base:
            kwargs["base"] = base
        self.client = GbpClient(**kwargs)

    def publish(self, review_name: str, comment: str) -> Dict[str, Any]:
        """``PUT {base}/v4/{review_name}/reply`` with the approved comment."""
        return self.client.update_reply(review_name, comment)
