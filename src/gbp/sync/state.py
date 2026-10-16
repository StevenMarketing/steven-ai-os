"""Per-location sync state: watermark, page checkpoint and run ledger.

The sync layer needs three pieces of durable state that survive between runs:

* a **watermark** -- the newest ``updateTime`` already ingested for a location,
  so an incremental run can skip unchanged reviews;
* a **checkpoint** -- the ``nextPageToken`` a previous run stopped at, so a
  resumed run continues where it left off instead of re-walking page 1;
* a **run ledger** -- every run attempt (ok / partial / failed), used to raise
  an alert once a location fails several times in a row.

All of it is stored in the same SQLite database through the existing
:class:`~gbp.repo.sqlite_repo.Repository`, using two small tables added to
``sql/schema.sql`` (``sync_state`` and ``sync_run_log``). The schema change is
backward compatible: both are ``CREATE TABLE IF NOT EXISTS``.

Standard library only, no network.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["SyncState"]

#: Number of consecutive failures that should trigger an alert by default.
DEFAULT_ALERT_THRESHOLD = 3


class SyncState:
    """Watermark + checkpoint + run-ledger store for review synchronisation."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self.conn = repo.conn

    # ------------------------------------------------------------------ #
    # watermark
    # ------------------------------------------------------------------ #
    def get_watermark(self, location_id: str) -> Optional[str]:
        """Return the stored watermark for ``location_id`` (or ``None``)."""
        row = self.conn.execute(
            "SELECT watermark FROM sync_state WHERE location_id = ?",
            (str(location_id),),
        ).fetchone()
        if row is None:
            return None
        return row["watermark"]

    def set_watermark(self, location_id: str, update_time: Optional[str]) -> None:
        """Persist ``update_time`` as the watermark for ``location_id``."""
        self._upsert(location_id, watermark=update_time)
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # checkpoint
    # ------------------------------------------------------------------ #
    def get_checkpoint(self, location_id: str) -> Optional[str]:
        """Return the stored page token for ``location_id`` (or ``None``)."""
        row = self.conn.execute(
            "SELECT checkpoint FROM sync_state WHERE location_id = ?",
            (str(location_id),),
        ).fetchone()
        if row is None:
            return None
        return row["checkpoint"]

    def set_checkpoint(self, location_id: str, page_token: Optional[str]) -> None:
        """Persist the in-flight pagination ``page_token`` for ``location_id``."""
        self._upsert(location_id, checkpoint=page_token)
        self.conn.commit()

    def clear_checkpoint(self, location_id: str) -> None:
        """Forget any stored checkpoint for ``location_id``."""
        self._upsert(location_id, checkpoint=None)
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # run ledger
    # ------------------------------------------------------------------ #
    def record_run(
        self,
        run_key: str,
        location_id: str,
        status: str,
        fetched: int = 0,
        upserted: int = 0,
        errors: int = 0,
        started_at: Optional[str] = None,
        ended_at: Optional[str] = None,
    ) -> int:
        """Append one run attempt to ``sync_run_log``; returns the row id."""
        cur = self.conn.execute(
            """
            INSERT INTO sync_run_log
                (run_key, location_id, status, fetched, upserted, errors,
                 started_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_key,
                str(location_id),
                status,
                int(fetched or 0),
                int(upserted or 0),
                int(errors or 0),
                started_at,
                ended_at,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def history(
        self, location_id: str, *, legacy_id: Any = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Unified sync history for one location, newest first (Group 8B / T27).

        ``sync_run_log`` is the canonical append-only ledger (every attempt, and
        what the Dashboard reads). ``sync_runs`` is the older per-``run_key``
        summary. Any ``sync_runs`` row never mirrored into the log is still
        returned -- so unifying the two sources cannot lose old history -- and a
        mirrored row is never reported twice.

        ``location_id`` is the location RESOURCE NAME (the only identity).
        ``legacy_id`` additionally matches rows written by older code that stored
        the numeric row id instead; nothing is rewritten or deleted.
        """
        loc = str(location_id)
        legacy = loc if legacy_id is None else str(legacy_id)
        limit = max(1, min(int(limit), 200))

        seen: set = set()
        rows: List[Dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT id, run_key, status, fetched, upserted, errors,"
            " started_at, ended_at FROM sync_run_log"
            " WHERE location_id = ? OR location_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (loc, legacy, limit),
        ).fetchall():
            seen.add(str(row["run_key"] or f"log:{row['id']}"))
            rows.append({
                "source": "sync_run_log",
                "run_key": row["run_key"],
                "status": row["status"],
                "fetched": int(row["fetched"] or 0),
                "upserted": int(row["upserted"] or 0),
                "errors": int(row["errors"] or 0),
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
            })

        for row in self.conn.execute(
            "SELECT run_key, status, fetched, upserted, errors,"
            " started_at, ended_at FROM sync_runs"
            " WHERE location_id = ? OR run_key LIKE ? || '%'"
            " ORDER BY id DESC LIMIT ?",
            (loc, loc, limit),
        ).fetchall():
            key = str(row["run_key"] or "")
            if key and key in seen:
                continue          # already reported from the canonical ledger
            rows.append({
                "source": "sync_runs",
                "run_key": row["run_key"],
                "status": row["status"],
                "fetched": int(row["fetched"] or 0),
                "upserted": int(row["upserted"] or 0),
                "errors": int(row["errors"] or 0),
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
            })

        rows.sort(
            key=lambda r: str(r.get("ended_at") or r.get("started_at") or ""),
            reverse=True,
        )
        return rows[:limit]

    def consecutive_failures(self, location_id: str) -> int:
        """Count trailing consecutive ``failed`` runs for ``location_id``.
        """
        rows = self.conn.execute(
            "SELECT status FROM sync_run_log WHERE location_id = ? ORDER BY id DESC",
            (str(location_id),),
        ).fetchall()
        count = 0
        for row in rows:
            if str(row["status"]) != "failed":
                break
            count += 1
        return count

    def should_alert(self, location_id: str, threshold: int = DEFAULT_ALERT_THRESHOLD) -> bool:
        """True when the location has failed at least ``threshold`` times in a row."""
        return self.consecutive_failures(location_id) >= int(threshold)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _upsert(
        self,
        location_id: str,
        watermark: Any = ...,
        checkpoint: Any = ...,
    ) -> None:
        """Insert/merge one ``sync_state`` row, touching only supplied columns."""
        row = self.conn.execute(
            "SELECT watermark, checkpoint FROM sync_state WHERE location_id = ?",
            (str(location_id),),
        ).fetchone()
        old_watermark = row["watermark"] if row is not None else None
        old_checkpoint = row["checkpoint"] if row is not None else None
        if watermark is ...:
            watermark = old_watermark
        if checkpoint is ...:
            checkpoint = old_checkpoint
        self.conn.execute(
            """
            INSERT INTO sync_state(location_id, watermark, checkpoint, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(location_id) DO UPDATE SET
                watermark  = excluded.watermark,
                checkpoint = excluded.checkpoint,
                updated_at = excluded.updated_at
            """,
            (str(location_id), watermark, checkpoint),
        )
