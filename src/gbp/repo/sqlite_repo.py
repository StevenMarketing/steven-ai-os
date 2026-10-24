"""Minimal SQLite repository layer for the GBP integration.

Standard library only. The repository is intentionally thin: it owns the
connection and the handful of upsert/query operations the foundation needs.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional

__all__ = ["Repository"]


class Repository:
    """Thin SQLite repository.

    ``path`` accepts anything ``sqlite3.connect`` accepts, including the
    special ``":memory:"`` value used by the tests.
    """

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Enforce foreign keys on every connection.
        self.conn.execute("PRAGMA foreign_keys = ON")

    # ------------------------------------------------------------------ #
    # schema
    # ------------------------------------------------------------------ #
    def init_db(self, schema_path: Optional[str] = None) -> None:
        """Create all tables.

        If ``schema_path`` is given, executes that ``.sql`` file; otherwise
        loads the bundled ``sql/schema.sql`` at the repository root.
        """
        if schema_path is None:
            here = os.path.dirname(os.path.abspath(__file__))
            # src/gbp/repo -> repo root is three levels up
            root = os.path.abspath(os.path.join(here, "..", "..", ".."))
            schema_path = os.path.join(root, "sql", "schema.sql")
        with open(schema_path, "r", encoding="utf-8") as fh:
            sql = fh.read()
        self.conn.executescript(sql)
        # Additive migration: databases created before the ZIP-code change have
        # no business_locations.postal_code. Never destructive -- the column is
        # added in place and existing rows keep their data (NULL = unknown).
        cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(business_locations)")}
        if "postal_code" not in cols:
            self.conn.execute(
                "ALTER TABLE business_locations ADD COLUMN postal_code TEXT")
        # Additive migration (Group 8B / T27): the older ``sync_runs`` summary
        # predates the location column, so it could not be joined to a location.
        # The column is added in place (nothing is dropped) and existing rows are
        # back-filled on a best-effort basis from the run_key prefix. Rows that
        # cannot be resolved keep NULL and stay visible through
        # SyncState.history(), so no history is lost either way.
        run_cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(sync_runs)")}
        if run_cols and "location_id" not in run_cols:
            self.conn.execute(
                "ALTER TABLE sync_runs ADD COLUMN location_id TEXT")
        if run_cols:
            self.conn.execute(
                "UPDATE sync_runs SET location_id = ("
                " SELECT l.location_name FROM business_locations l"
                " WHERE sync_runs.run_key LIKE l.location_name || '%' LIMIT 1)"
                " WHERE location_id IS NULL")
        # Additive migration (Issue #112): google_accounts predates the
        # business-group projection, so it carried only the account resource
        # name. The display name + type are added in place (nothing is dropped);
        # existing rows keep NULL and are reported as "Ungrouped" by the
        # projection rather than guessing a group.
        self._ensure_account_columns()
        self._ensure_location_columns()
        # Issue #126: the first Google WRITE needs two new reply states
        # (publish-intent, sent) and an approval text hash. ``sql/schema.sql``
        # is a protected path, so both arrive here, additively, exactly like
        # the #117 live columns above.
        self._ensure_columns("approval_records", ("text_hash",))
        self._ensure_reply_draft_statuses()
        self.conn.commit()

    def _ensure_reply_draft_statuses(self) -> None:
        """Widen ``reply_drafts.status`` to allow ``publish-intent`` / ``sent``.

        A ``CHECK`` constraint cannot be altered in place, so the table is
        REBUILT only when its current definition does not already permit the two
        new states: every row (and its ids) is copied to a new table with the
        widened CHECK, the old table is dropped and the new one renamed, and the
        index is recreated. Foreign keys are disabled for the swap so the
        ``approval_records`` children are NOT cascade-deleted. Idempotent and
        non-destructive: a database already permitting the states is left alone.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table'"
            " AND name = 'reply_drafts'").fetchone()
        if row is None or not row[0]:
            return
        current = str(row[0])
        if "publish-intent" in current and "'sent'" in current:
            return
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.executescript(
                """
                CREATE TABLE reply_drafts_migrate (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_id   INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
                    draft_text  TEXT,
                    status      TEXT NOT NULL DEFAULT 'draft'
                                  CHECK (status IN ('draft','needs_review','approved',
                                                    'rejected','ready_to_publish',
                                                    'published','failed',
                                                    'publish-intent','sent')),
                    version     INTEGER NOT NULL DEFAULT 1,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO reply_drafts_migrate
                    (id, review_id, draft_text, status, version, created_at, updated_at)
                    SELECT id, review_id, draft_text, status, version,
                           COALESCE(created_at, datetime('now')),
                           COALESCE(updated_at, datetime('now'))
                    FROM reply_drafts;
                DROP TABLE reply_drafts;
                ALTER TABLE reply_drafts_migrate RENAME TO reply_drafts;
                CREATE INDEX IF NOT EXISTS idx_reply_drafts_review_id
                    ON reply_drafts(review_id);
                """
            )
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.commit()

    def _ensure_account_columns(self) -> None:
        """Add the optional ``google_accounts`` columns once (additive).

        ``account_name`` / ``account_type`` come from Issue #112 (business-group
        projection); ``tenant_id`` from Issue #117 (live-sync ownership).
        Idempotent and non-destructive: a database created before those issues
        gains the nullable columns in place; an unknown/absent table is left
        untouched.
        """
        self._ensure_columns("google_accounts",
                             ("account_name", "account_type", "tenant_id"))

    def _ensure_location_columns(self) -> None:
        """Add the live-sync provenance columns to ``business_locations`` once.

        ``tenant_id`` / ``source`` / ``sync_status`` / ``last_sync_at`` (Issue
        #117) record which tenant a persisted live location belongs to and how
        its last sync went. Additive and non-destructive: existing fixture rows
        keep NULL columns, which the readers treat as "demo seed, no live data".
        """
        self._ensure_columns("business_locations",
                             ("tenant_id", "source", "sync_status",
                              "last_sync_at"))
        # The index is created here (not in schema.sql) so a database created
        # before Issue #117 gains its columns first and then the index.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_business_locations_tenant_id"
            " ON business_locations(tenant_id)")

    def _ensure_columns(self, table: str, wanted: Any = ()) -> None:
        """Add each missing nullable TEXT column to ``table`` (idempotent)."""
        cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(%s)" % table)}
        if not cols:
            return  # unknown/absent table: never invent one here
        for column in wanted:
            if column in cols:
                continue
            self.conn.execute(
                "ALTER TABLE %s ADD COLUMN %s TEXT" % (table, column))
            cols.add(column)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # accounts / locations
    # ------------------------------------------------------------------ #
    def upsert_account(self, name: str, account_name: Optional[str] = None,
                       account_type: Optional[str] = None,
                       tenant_id: Optional[str] = None) -> int:
        """Insert account ``name`` or return the existing row's id.

        ``account_name`` (the display name) and ``account_type`` are optional and
        additive: a call that omits them never erases a previously stored value
        (COALESCE), so the older call sites are unaffected. They feed the
        business-group projection (Issue #112). ``tenant_id`` (Issue #117)
        records which tenant wrote the row and is never overwritten by a
        fixture/demo call that omits it.
        """
        # Older databases predate these columns; make sure they exist so a
        # repository built without ``init_db`` still accepts the metadata.
        self._ensure_account_columns()
        self.conn.execute(
            "INSERT INTO google_accounts(name, account_name, account_type, tenant_id)"
            " VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET"
            " account_name = COALESCE(excluded.account_name,"
            "                          google_accounts.account_name),"
            " account_type = COALESCE(excluded.account_type,"
            "                          google_accounts.account_type),"
            " tenant_id = COALESCE(excluded.tenant_id,"
            "                       google_accounts.tenant_id)",
            (name, account_name, account_type, tenant_id),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM google_accounts WHERE name = ?", (name,)
        ).fetchone()
        return int(row["id"])

    def account_tenant(self, name: str) -> Optional[str]:
        """The tenant that owns account ``name`` (``None`` = fixture/unknown)."""
        cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(google_accounts)")}
        if "tenant_id" not in cols:
            return None
        row = self.conn.execute(
            "SELECT tenant_id FROM google_accounts WHERE name = ?",
            (name,)).fetchone()
        return None if row is None else row["tenant_id"]

    def account_metadata(self) -> Dict[int, Dict[str, Any]]:
        """Return ``{account row id: {"name", "account_name", "account_type"}}``.

        Tolerant of a database created before Issue #112 (the two metadata
        columns are simply absent and reported as ``None``), so the projection
        can never crash on an old DB file.
        """
        cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(google_accounts)")}
        if not cols:
            return {}
        selected = ["id", "name"]
        selected.append("account_name" if "account_name" in cols
                        else "NULL AS account_name")
        selected.append("account_type" if "account_type" in cols
                        else "NULL AS account_type")
        out: Dict[int, Dict[str, Any]] = {}
        for row in self.conn.execute(
                "SELECT " + ", ".join(selected) + " FROM google_accounts").fetchall():
            data = dict(row)
            out[int(data["id"])] = data
        return out

    def upsert_location(
        self,
        account_id: int,
        location_name: str,
        title: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        language: Optional[str] = None,
        postal_code: Optional[str] = None,
        tenant_id: Optional[str] = None,
        source: Optional[str] = None,
        sync_status: Optional[str] = None,
        last_sync_at: Optional[str] = None,
    ) -> int:
        """Insert or update a business location keyed on ``location_name``.

        ``postal_code`` is stored as TEXT so leading zeros survive. It is only
        ever overwritten by a new non-empty value (COALESCE), so a later upsert
        that omits the ZIP never erases a known one.

        ``tenant_id`` / ``source`` / ``sync_status`` / ``last_sync_at`` (Issue
        #117) tag the row as live-synced data for one tenant. Like the ZIP they
        only ever move forward: a call that omits them (the fixture seeder)
        leaves the stored value alone, so re-seeding the demo data can never
        strip the live provenance off a synced row.
        """
        self._ensure_location_columns()
        self.conn.execute(
            """
            INSERT INTO business_locations
                (account_id, location_name, title, city, state, language,
                 postal_code, tenant_id, source, sync_status, last_sync_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(location_name) DO UPDATE SET
                account_id  = excluded.account_id,
                title       = excluded.title,
                city        = excluded.city,
                state       = excluded.state,
                language    = excluded.language,
                postal_code = COALESCE(excluded.postal_code,
                                       business_locations.postal_code),
                tenant_id   = COALESCE(excluded.tenant_id,
                                       business_locations.tenant_id),
                source      = COALESCE(excluded.source,
                                       business_locations.source),
                sync_status = COALESCE(excluded.sync_status,
                                       business_locations.sync_status),
                last_sync_at = COALESCE(excluded.last_sync_at,
                                        business_locations.last_sync_at)
            """,
            (account_id, location_name, title, city, state, language,
             (str(postal_code).strip() or None) if postal_code is not None else None,
             tenant_id, source, sync_status, last_sync_at),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM business_locations WHERE location_name = ?",
            (location_name,),
        ).fetchone()
        return int(row["id"])

    # -- live-sync provenance (Issue #117) -------------------------------- #
    def location_tenant(self, location_name: str) -> Optional[str]:
        """The tenant a location row belongs to (``None`` = fixture/unknown)."""
        cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(business_locations)")}
        if "tenant_id" not in cols:
            return None
        row = self.conn.execute(
            "SELECT tenant_id FROM business_locations WHERE location_name = ?",
            (location_name,)).fetchone()
        return None if row is None else row["tenant_id"]

    def live_location_names(self, tenant: str) -> List[str]:
        """Every location resource name persisted for ``tenant`` as live data."""
        cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(business_locations)")}
        if not {"tenant_id", "source"} <= cols:
            return []
        return [str(r["location_name"]) for r in self.conn.execute(
            "SELECT location_name FROM business_locations"
            " WHERE tenant_id = ? AND source = 'live'"
            " ORDER BY location_name", (tenant,)).fetchall()]

    def live_locations(self, tenant: str) -> List[Dict[str, Any]]:
        """Every live location row for ``tenant`` (Issue #123).

        Returns ``{location_row_id, location_name, business_name, account_name}``
        per row: the pre-resolved row id the review sync must attach reviews to,
        the stored (short) resource name, its human title and the owning
        ``google_accounts.name`` used to compose the full v4 resource name.
        Empty when the database predates the live-sync columns.
        """
        cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(business_locations)")}
        if not {"tenant_id", "source"} <= cols:
            return []
        rows = self.conn.execute(
            "SELECT l.id AS location_row_id, l.location_name, l.title,"
            " COALESCE(a.name, '') AS account_name"
            " FROM business_locations l"
            " LEFT JOIN google_accounts a ON a.id = l.account_id"
            " WHERE l.tenant_id = ? AND l.source = 'live'"
            " ORDER BY l.location_name", (tenant,)).fetchall()
        return [{
            "location_row_id": int(r["location_row_id"]),
            "location_name": str(r["location_name"]),
            "business_name": r["title"],
            "account_name": str(r["account_name"] or ""),
        } for r in rows]


    def mark_locations_absent(self, tenant: str, keep: Any,
                             *, status: str = "removed",
                             at: Optional[str] = None) -> int:
        """Flag ``tenant``'s live locations that this sync did NOT return.

        A location removed from Google is marked (``sync_status = 'removed'``)
        rather than deleted or silently left looking fresh, so the dashboard can
        tell "gone from Google" apart from "synced ok". Only rows already tagged
        as this tenant's live data are touched -- the query is scoped by
        ``tenant_id`` and ``source = 'live'``, so another tenant's rows (and the
        demo seed) can never be modified.
        """
        self._ensure_location_columns()
        names = [str(n) for n in (keep or ())]
        sql = ("UPDATE business_locations SET sync_status = ?, last_sync_at = ?"
               " WHERE tenant_id = ? AND source = 'live'")
        params: List[Any] = [str(status), at, tenant]
        if names:
            sql += " AND location_name NOT IN (%s)" % ",".join(
                "?" for _ in names)
            params.extend(names)
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0)

    # ------------------------------------------------------------------ #
    # reviews
    # ------------------------------------------------------------------ #
    def upsert_review(
        self,
        location_id: int,
        review_name: str,
        review_id: Optional[str] = None,
        rating: Optional[int] = None,
        comment: Optional[str] = None,
        reviewer_name: Optional[str] = None,
        create_time: Optional[str] = None,
        update_time: Optional[str] = None,
        reply_text: Optional[str] = None,
        reply_update_time: Optional[str] = None,
        status: Optional[str] = None,
        raw_json: Optional[str] = None,
    ) -> int:
        """Idempotent upsert of a review keyed on the unique ``review_name``.

        On conflict, mutable fields are refreshed and ``last_seen_at`` is
        bumped to now; ``first_seen_at`` is left untouched. ``reply_text`` is
        only overwritten when a non-NULL value is supplied, so an incoming
        payload without a reply never wipes an existing one.
        """
        self.conn.execute(
            """
            INSERT INTO reviews (
                location_id, review_name, review_id, rating, comment,
                reviewer_name, create_time, update_time, reply_text,
                reply_update_time, status, raw_json,
                first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    datetime('now'), datetime('now'))
            ON CONFLICT(review_name) DO UPDATE SET
                location_id       = excluded.location_id,
                review_id         = excluded.review_id,
                rating            = excluded.rating,
                comment           = excluded.comment,
                reviewer_name     = excluded.reviewer_name,
                create_time       = excluded.create_time,
                update_time       = excluded.update_time,
                reply_text        = COALESCE(excluded.reply_text, reviews.reply_text),
                reply_update_time = COALESCE(excluded.reply_update_time,
                                             reviews.reply_update_time),
                status            = excluded.status,
                raw_json          = excluded.raw_json,
                last_seen_at      = datetime('now')
            """,
            (
                location_id, review_name, review_id, rating, comment,
                reviewer_name, create_time, update_time, reply_text,
                reply_update_time, status, raw_json,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM reviews WHERE review_name = ?", (review_name,)
        ).fetchone()
        return int(row["id"])

    def get_reviews(self, status: Optional[str] = None) -> List[sqlite3.Row]:
        """Return reviews, optionally filtered by ``status``."""
        if status is None:
            cur = self.conn.execute("SELECT * FROM reviews ORDER BY id")
        else:
            cur = self.conn.execute(
                "SELECT * FROM reviews WHERE status = ? ORDER BY id", (status,)
            )
        return cur.fetchall()

    def count_unanswered(self) -> int:
        """Count reviews that have no reply yet (``reply_text IS NULL``)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE reply_text IS NULL"
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ #
    # lookups used by the reply engine
    # ------------------------------------------------------------------ #
    def get_location(self, location_id: int) -> Optional[sqlite3.Row]:
        """Return one ``business_locations`` row by id, or ``None``."""
        return self.conn.execute(
            "SELECT * FROM business_locations WHERE id = ?", (location_id,)
        ).fetchone()

    def get_review(self, review_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()

    def get_recent_replies(self, location_id: int, days: int = 90) -> List[dict]:
        """Return previous replies for ``location_id`` (same shop only).

        Only rows that already carry a non-empty ``reply_text`` are returned, so
        the duplicate checker compares against real published/queued replies of
        the SAME location. ``days`` windows on ``reply_update_time`` when that
        timestamp is parseable; unparseable timestamps are kept.
        """
        cur = self.conn.execute(
            """
            SELECT id, location_id, reply_text, reply_update_time
            FROM reviews
            WHERE location_id = ?
              AND reply_text IS NOT NULL
              AND TRIM(reply_text) <> ''
            ORDER BY id DESC
            """,
            (location_id,),
        )
        rows = cur.fetchall()
        if days is None:
            return [dict(r) for r in rows]
        import datetime as _dt

        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=int(days))
        out: List[dict] = []
        for row in rows:
            stamp = row["reply_update_time"]
            if stamp:
                text = str(stamp).replace("Z", "+00:00")
                try:
                    when = _dt.datetime.fromisoformat(text)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=_dt.timezone.utc)
                    if when < cutoff:
                        continue
                except ValueError:
                    pass  # keep rows we cannot parse
            out.append(dict(row))
        return out

    # ------------------------------------------------------------------ #
    # reply drafts
    # ------------------------------------------------------------------ #
    def insert_reply_draft(
        self,
        review_id: int,
        draft_text: Optional[str] = None,
        status: str = "draft",
        idempotent: bool = True,
    ) -> int:
        """Insert a draft row and return its id.

        Idempotent by default: a review has AT MOST ONE reply draft, so if a
        draft already exists for ``review_id`` its id is returned and NO new
        row is created. This keeps re-runs of the demo seeder (and any repeated
        writer) from growing ``reply_drafts`` without bound, and makes the
        "reviews vs reply drafts" units line up (a review can have 0..1 draft).
        Pass ``idempotent=False`` only when a caller genuinely needs a second
        row for the same review.

        ``published`` is rejected here as well as by the schema CHECK: this
        module never publishes. (The reply workflow, not this module, is what
        moves a draft on to ``publish-intent``/``sent`` -- Issue #126.)
        """
        if status not in ("draft", "needs_review", "approved", "rejected",
                          "ready_to_publish", "publish-intent", "sent"):
            raise ValueError(f"invalid draft status: {status!r}")
        if idempotent:
            existing = self.conn.execute(
                "SELECT id FROM reply_drafts WHERE review_id = ? ORDER BY id LIMIT 1",
                (review_id,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
        cur = self.conn.execute(
            "INSERT INTO reply_drafts(review_id, draft_text, status) VALUES (?, ?, ?)",
            (review_id, draft_text, status),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_reply_drafts(
        self, review_id: Optional[int] = None
    ) -> List[sqlite3.Row]:
        """Return draft rows, optionally filtered by ``review_id``."""
        if review_id is None:
            return self.conn.execute(
                "SELECT * FROM reply_drafts ORDER BY id"
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM reply_drafts WHERE review_id = ? ORDER BY id",
            (review_id,),
        ).fetchall()

    # ------------------------------------------------------------------ #
    # audit log
    # ------------------------------------------------------------------ #
    def insert_audit_log(
        self,
        action: str,
        actor: Optional[str] = None,
        entity: Optional[str] = None,
        entity_id: Optional[str] = None,
        detail_json: Optional[str] = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO audit_logs(actor, action, entity, entity_id, detail_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor, action, entity, entity_id, detail_json),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_audit_logs(self, entity: Optional[str] = None) -> List[sqlite3.Row]:
        if entity is None:
            return self.conn.execute(
                "SELECT * FROM audit_logs ORDER BY id"
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM audit_logs WHERE entity = ? ORDER BY id", (entity,)
        ).fetchall()