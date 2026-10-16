"""Idempotent review synchronisation (offline, stdlib only).

:class:`ReviewSync` walks every reviews page of a location through a
:class:`~gbp.client.gbp_client.GbpClient`, normalises each review and stores it
through the :class:`~gbp.repo.sqlite_repo.Repository`.

Guarantees
----------
* Pagination via ``nextPageToken``; an empty page ends cleanly.
* Idempotent upsert keyed on ``review_name``: a snapshot whose ``updateTime`` is
  *older* than the stored one never overwrites it, and ``last_seen_at`` is
  refreshed either way.
* Exactly one ``sync_runs`` row per run key (``<location>@<YYYYMMDD>``), which
  is itself idempotent so re-running the same day collapses into one row.
* A single malformed review is counted in ``errors`` and never aborts the run.
* Incremental syncs use a PERSISTED per-location watermark (so an unchanged
  review is skipped and only a NEWER ``updateTime`` is re-upserted) and resume
  from a stored pagination checkpoint when one exists.
* *Guarantee*: running an incremental sync twice in a row with no data change
  writes zero new/duplicate rows and the second run reports ``upserted == 0``.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from .state import SyncState

__all__ = ["ReviewSync"]

#: Upper bound on pages walked per location (protects against a token loop).
PAGE_LIMIT = 1000


class ReviewSync:
    """Synchronise GBP reviews into the local SQLite repository."""

    def __init__(
        self,
        repo: Any,
        client: Any,
        clock: Optional[Callable[[], Any]] = None,
        state: Optional[SyncState] = None,
    ) -> None:
        self.repo = repo
        self.client = client
        self._clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self.state = state if state is not None else SyncState(repo)

    # -- helpers ----------------------------------------------------------- #
    def _now(self) -> str:
        return self._clock().strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _account_of(location: str) -> str:
        parts = str(location).split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else str(location)

    @staticmethod
    def _location_of_review(name: Any) -> Optional[str]:
        """Extract ``accounts/A/locations/L`` from a review resource name."""
        if not name:
            return None
        parts = str(name).split("/")
        if len(parts) >= 4:
            return "/".join(parts[:4])
        return None

    def _ensure_location(self, location: str):
        account_id = self.repo.upsert_account(self._account_of(location))
        location_id = self.repo.upsert_location(account_id, location)
        return account_id, location_id

    def _iter_reviews(
        self,
        location: str,
        start_token: Optional[str] = None,
        on_page: Optional[Callable[[Optional[str]], None]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield every raw review dict across all pages for ``location``.

        ``start_token`` resumes pagination from a stored checkpoint instead of
        page 1. ``on_page`` is called after each page with the token to resume
        from next (``None`` on the last page), so callers can persist progress.
        """
        page_token: Optional[str] = start_token
        for _ in range(PAGE_LIMIT):
            page = self.client.list_reviews(location, page_token=page_token)
            if page is None:
                break
            for review in page.get("reviews") or []:
                yield review
            page_token = page.get("nextPageToken")
            if on_page is not None:
                on_page(page_token)
            if not page_token:
                break

    @staticmethod
    def _normalise(review: Any) -> Dict[str, Any]:
        """Map a raw API review to ``upsert_review`` kwargs.

        Raises ``ValueError``/``TypeError`` for a malformed review; callers
        count that as an error instead of aborting.
        """
        if not isinstance(review, dict):
            raise ValueError("review payload is not a JSON object")
        name = review.get("name")
        if not name:
            raise ValueError("review payload has no name")
        reviewer = review.get("reviewer")
        reply = review.get("reviewReply")
        rating = review.get("rating")
        return {
            "review_name": str(name),
            "review_id": str(name).rsplit("/", 1)[-1],
            "rating": int(rating) if rating is not None else None,
            "comment": review.get("comment"),
            "reviewer_name": reviewer.get("displayName") if isinstance(reviewer, dict) else None,
            "create_time": review.get("createTime"),
            "update_time": review.get("updateTime"),
            "reply_text": reply.get("comment") if isinstance(reply, dict) else None,
            "reply_update_time": reply.get("updateTime") if isinstance(reply, dict) else None,
            "status": review.get("status") or "new",
            "raw_json": json.dumps(review, ensure_ascii=False, sort_keys=True, default=str),
        }

    def _touch(self, review_id: int) -> None:
        """Refresh ``last_seen_at`` without changing the stored snapshot."""
        self.repo.conn.execute(
            "UPDATE reviews SET last_seen_at = datetime('now') WHERE id = ?", (review_id,)
        )
        self.repo.conn.commit()

    def _upsert(self, location_id: int, data: Dict[str, Any]) -> bool:
        """Upsert one normalised review. Returns True when a write happened.

        When the stored snapshot is strictly newer (by ``updateTime``) the row
        is left untouched and only ``last_seen_at`` is refreshed.
        """
        existing = self.repo.conn.execute(
            "SELECT id, update_time FROM reviews WHERE review_name = ?",
            (data["review_name"],),
        ).fetchone()
        if existing is not None:
            old = existing["update_time"] or ""
            new = data.get("update_time") or ""
            if old and new and new < old:
                self._touch(int(existing["id"]))
                return False
        self.repo.upsert_review(location_id=location_id, **data)
        return True

    def _record_run(
        self,
        location: str,
        run_key: str,
        started_at: str,
        fetched: int,
        upserted: int,
        errors: int,
        status: str,
    ) -> None:
        """Record one run in BOTH ledgers.

        ``sync_runs`` keeps the historical one-row-per-``run_key`` summary
        (unchanged, for backward compatibility). The same attempt is ALSO
        appended to ``sync_run_log`` -- the canonical ledger the Dashboard
        reads -- so a sync driven by the queue is finally visible in the UI
        (Group 8B / T27 unification). Nothing is deleted or rewritten.
        """
        self.repo.conn.execute(
            """
            INSERT INTO sync_runs
                (run_key, started_at, ended_at, status, fetched, upserted, errors)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_key) DO UPDATE SET
                ended_at = excluded.ended_at,
                status   = excluded.status,
                fetched  = excluded.fetched,
                upserted = excluded.upserted,
                errors   = excluded.errors
            """,
            (run_key, started_at, self._now(), status, fetched, upserted, errors),
        )
        self.repo.conn.commit()
        # Mirror into the append-only ledger, keyed on the location RESOURCE
        # NAME (the only identity) so the Dashboard can show real history. The
        # ledger's vocabulary is ok/partial/failed, so the older "error" marker
        # is mapped to "failed" (the run really did fail) -- that also keeps the
        # consecutive-failure streak meaningful for alerting.
        self.state.record_run(
            run_key, str(location), "failed" if status == "error" else status,
            fetched=fetched, upserted=upserted, errors=errors,
            started_at=started_at, ended_at=self._now(),
        )

    @staticmethod
    def _default_run_key(location: str, started_at: str) -> str:
        day = started_at.split("T", 1)[0].replace("-", "")
        return f"{location}@{day}"

    def _collect_best(
        self, location_id: int, raw_reviews: Iterable[Any], watermark: Optional[str]
    ) -> Dict[str, Any]:
        """Normalise + de-duplicate raw reviews (newer snapshot wins).

        Returns a counts dict with ``fetched``, ``errors``, ``skipped`` and the
        winning ``best`` mapping (name -> normalised review).
        """
        fetched = errors = skipped = 0
        best: Dict[str, Dict[str, Any]] = {}
        for raw in raw_reviews:
            fetched += 1
            try:
                data = self._normalise(raw)
            except Exception:
                errors += 1
                continue
            if watermark is not None and str(data.get("update_time") or "") <= watermark:
                skipped += 1
                continue
            current = best.get(data["review_name"])
            if current is not None and str(data.get("update_time") or "") < str(
                current.get("update_time") or ""
            ):
                continue  # an older duplicate: keep the newer snapshot
            best[data["review_name"]] = data
        return {"fetched": fetched, "errors": errors, "skipped": skipped, "best": best}

    def _write_best(self, location_id: int, best: Dict[str, Dict[str, Any]]):
        """Upsert every normalised review; returns ``(upserted, errors, newest)``."""
        upserted = errors = 0
        newest: Optional[str] = None
        for data in best.values():
            try:
                if self._upsert(location_id, data):
                    upserted += 1
            except Exception:
                errors += 1
            stamp = data.get("update_time")
            if stamp and (newest is None or str(stamp) > newest):
                newest = str(stamp)
        return upserted, errors, newest

    def _store_reviews(
        self,
        location: str,
        raw_reviews: Iterable[Any],
        run_key: Optional[str] = None,
        watermark: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store already-fetched reviews for ``location`` (used by batch mode)."""
        _account_id, location_id = self._ensure_location(location)
        started_at = self._now()
        key = run_key or self._default_run_key(location, started_at)
        collected = self._collect_best(location_id, raw_reviews, watermark)
        upserted, write_errors, _newest = self._write_best(location_id, collected["best"])
        errors = collected["errors"] + write_errors
        status = "ok" if errors == 0 else "partial"
        return {
            "location": location,
            "run_key": key,
            "fetched": collected["fetched"],
            "upserted": upserted,
            "errors": errors,
            "skipped": collected["skipped"],
            "status": status,
            "started_at": started_at,
            "ended_at": self._now(),
        }

    # -- public API -------------------------------------------------------- #
    def sync_location(self, location: str, run_key: Optional[str] = None) -> Dict[str, Any]:
        """Full sync of one location; walks every page and upserts each review.

        Every terminal outcome (ok / partial / failed) is recorded EXACTLY ONCE
        in both ledgers -- including a failure that happens before the walk even
        starts -- so the Dashboard's consecutive-failure streak stays truthful.
        """
        started_at = self._now()
        key = run_key or self._default_run_key(location, started_at)
        try:
            _account_id, location_id = self._ensure_location(location)
        except Exception:
            self._record_run(location, key, started_at, 0, 0, 1, "failed")
            raise

        fetched = upserted = errors = 0
        status = "ok"
        #: name -> newest snapshot seen in this walk (older duplicates dropped).
        # (No cross-page buffering: each review is written as its page arrives,
        # so the pagination checkpoint always describes data that is actually
        # stored -- see the resume note below.)
        # Resume from a stored page checkpoint when one exists and persist
        # progress after every page, so a crash mid-walk does not lose the
        # position (this is what makes "resume after failure" real rather than
        # aspirational). A failed walk deliberately LEAVES the checkpoint in
        # place; only a completed walk clears it.
        start_token = self.state.get_checkpoint(location)

        def _persist_progress(next_token: Optional[str]) -> None:
            # Advances ONLY after the page it describes has been written;
            # checkpointing earlier resumed PAST unstored reviews (a real
            # data-loss bug).
            if next_token:
                self.state.set_checkpoint(location, next_token)
            else:
                self.state.clear_checkpoint(location)

        try:
            for raw in self._iter_reviews(location, start_token=start_token,
                                          on_page=_persist_progress):
                fetched += 1
                try:
                    data = self._normalise(raw)
                except Exception:
                    errors += 1
                    continue
                try:
                    # Streamed upsert: an older duplicate is rejected by
                    # _upsert()'s monotonic update_time guard, so no in-memory
                    # cross-page buffer is needed and memory stays bounded for a
                    # location with thousands of reviews.
                    if self._upsert(location_id, data):
                        upserted += 1
                except Exception:
                    errors += 1
        except Exception:
            self._record_run(location, key, started_at, fetched, upserted,
                             errors + 1, "error")
            raise

        status = "ok" if errors == 0 else "partial"
        self._record_run(location, key, started_at, fetched, upserted, errors, status)
        return {
            "location": location,
            "run_key": key,
            "fetched": fetched,
            "upserted": upserted,
            "errors": errors,
            "status": status,
            "resumed_from": start_token,
            "started_at": started_at,
            "ended_at": self._now(),
        }

    def incremental_sync(
        self, location: str, since_update_time: Optional[str] = None
    ) -> Dict[str, Any]:
        """Like :meth:`sync_location` but skips reviews at or below the watermark.

        Uses the PERSISTED watermark when ``since_update_time`` is not given and
        persists the newest ``updateTime`` seen afterwards. Resumes from a stored
        page checkpoint when one exists, and clears it once the walk completes.

        Returns a counts dict (``fetched``, ``upserted``, ``errors``,
        ``skipped``) and does not write a ``sync_runs`` row.
        """
        _account_id, location_id = self._ensure_location(location)
        fetched = upserted = errors = skipped = 0
        best: Dict[str, Dict[str, Any]] = {}

        watermark = since_update_time
        if watermark is None:
            watermark = self.state.get_watermark(location)
        watermark = str(watermark) if watermark is not None else None

        start_token = self.state.get_checkpoint(location)

        def _persist_progress(next_token: Optional[str]) -> None:
            if next_token:
                self.state.set_checkpoint(location, next_token)
            else:
                self.state.clear_checkpoint(location)

        for raw in self._iter_reviews(location, start_token=start_token, on_page=_persist_progress):
            fetched += 1
            try:
                data = self._normalise(raw)
            except Exception:
                errors += 1
                continue
            if watermark is not None and str(data.get("update_time") or "") <= watermark:
                skipped += 1
                continue
            current = best.get(data["review_name"])
            if current is not None and str(data.get("update_time") or "") < str(
                current.get("update_time") or ""
            ):
                continue
            best[data["review_name"]] = data

        newest = watermark
        for data in best.values():
            try:
                if self._upsert(location_id, data):
                    upserted += 1
            except Exception:
                errors += 1
            stamp = data.get("update_time")
            if stamp and (newest is None or str(stamp) > newest):
                newest = str(stamp)

        if newest is not None:
            self.state.set_watermark(location, newest)
        self.state.clear_checkpoint(location)

        return {
            "location": location,
            "fetched": fetched,
            "upserted": upserted,
            "errors": errors,
            "skipped": skipped,
            "resumed_from": start_token,
            "watermark": newest,
        }

    def sync_all(
        self,
        locations: Iterable[str],
        batch: bool = False,
        run_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Sync many locations; a single failure never aborts the whole run.

        With ``batch=True`` the locations of one account are fetched through
        :meth:`GbpClient.batch_get_reviews` and the returned reviews are routed
        back to their location (idempotent upsert keyed on ``review_name``).

        Returns ``{location: result_dict}``; every location (ok, partial or
        failed) is also recorded in the :class:`SyncState` run ledger.
        """
        locs: List[str] = list(locations)
        results: Dict[str, Any] = {}

        if batch:
            by_account: Dict[str, List[str]] = {}
            for loc in locs:
                by_account.setdefault(self._account_of(loc), []).append(loc)
            for account, account_locs in by_account.items():
                started_at = self._now()
                key = run_key or self._default_run_key(account, started_at)
                try:
                    response = self.client.batch_get_reviews(account, account_locs)
                except Exception:
                    for loc in account_locs:
                        self.state.record_run(key, loc, "failed", 0, 0, 1, started_at, self._now())
                        results[loc] = {
                            "location": loc,
                            "run_key": key,
                            "fetched": 0,
                            "upserted": 0,
                            "errors": 1,
                            "skipped": 0,
                            "status": "failed",
                        }
                    continue

                grouped: Dict[Optional[str], List[Any]] = {}
                for raw in (response or {}).get("reviews") or []:
                    loc_name = self._location_of_review(
                        raw.get("name") if isinstance(raw, dict) else None
                    )
                    grouped.setdefault(loc_name, []).append(raw)

                for loc in account_locs:
                    result = self._store_reviews(loc, grouped.get(loc, []), run_key=key)
                    results[loc] = result
                    self.state.record_run(
                        result["run_key"], loc, result["status"], result["fetched"],
                        result["upserted"], result["errors"], started_at, result["ended_at"],
                    )
            return results

        for loc in locs:
            try:
                result = self.sync_location(loc)
            except Exception:
                # sync_location already recorded this failure in BOTH ledgers;
                # recording again here would double-count the failure streak.
                results[loc] = {
                    "location": loc,
                    "run_key": run_key or self._default_run_key(loc, self._now()),
                    "fetched": 0,
                    "upserted": 0,
                    "errors": 1,
                    "skipped": 0,
                    "status": "failed",
                }
                continue
            results[loc] = result
        return results
