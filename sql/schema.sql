-- GBP integration — SQLite schema
-- All timestamps are ISO-8601 TEXT unless noted. Foreign keys are enforced by
-- the application (PRAGMA foreign_keys = ON).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS google_accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS business_locations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
    location_name TEXT NOT NULL UNIQUE,
    title         TEXT,
    city          TEXT,
    state         TEXT,
    postal_code   TEXT,
    language      TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_business_locations_account_id
    ON business_locations(account_id);

CREATE TABLE IF NOT EXISTS brand_profiles (
    location_id            INTEGER NOT NULL UNIQUE
                             REFERENCES business_locations(id) ON DELETE CASCADE,
    business_name          TEXT,
    city                   TEXT,
    services_json          TEXT,
    language               TEXT,
    tone                   TEXT,
    seo_keywords_json      TEXT,
    rules_json             TEXT,
    auto_publish_allowed   INTEGER NOT NULL DEFAULT 0,
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id       INTEGER NOT NULL REFERENCES business_locations(id) ON DELETE CASCADE,
    review_name       TEXT NOT NULL UNIQUE,
    review_id         TEXT,
    rating            INTEGER,
    comment           TEXT,
    reviewer_name     TEXT,
    create_time       TEXT,
    update_time       TEXT,
    reply_text        TEXT,
    reply_update_time TEXT,
    status            TEXT,
    raw_json          TEXT,
    first_seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reviews_location_id ON reviews(location_id);
CREATE INDEX IF NOT EXISTS idx_reviews_status      ON reviews(status);
CREATE INDEX IF NOT EXISTS idx_reviews_update_time ON reviews(update_time);

CREATE TABLE IF NOT EXISTS reply_drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id   INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    draft_text  TEXT,
    -- status: draft | needs_review | approved | rejected |
    --         ready_to_publish | published | failed
    -- (the first four are what the reply engine writes; the rest belong to the
    --  approval workflow's state machine -- see src/gbp/workflow/states.py)
    status      TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','needs_review','approved','rejected',
                                    'ready_to_publish','published','failed')),
    -- optimistic-concurrency counter; bumped on every mutation
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reply_drafts_review_id ON reply_drafts(review_id);

-- Idempotency guard for approval-workflow mutations: the first request with a
-- given key stores its result; replays return the stored result and write
-- nothing (see src/gbp/workflow/service.py).
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    action      TEXT,
    entity      TEXT,
    entity_id   TEXT,
    result_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS approval_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL REFERENCES reply_drafts(id) ON DELETE CASCADE,
    action      TEXT,
    actor       TEXT,
    decided_at  TEXT NOT NULL DEFAULT (datetime('now')),
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_approval_records_draft_id ON approval_records(draft_id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key     TEXT NOT NULL UNIQUE,
    started_at  TEXT,
    ended_at    TEXT,
    status      TEXT,
    fetched     INTEGER DEFAULT 0,
    upserted    INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0
);

-- Per-location incremental-sync cursor: the last ``updateTime`` watermark and
-- an optional in-flight pagination checkpoint. Added by Group 5A in a
-- backward-compatible way (CREATE ... IF NOT EXISTS, so existing DBs are fine).
CREATE TABLE IF NOT EXISTS sync_state (
    location_id TEXT PRIMARY KEY,
    watermark   TEXT,
    checkpoint  TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only run ledger used for consecutive-failure alerting. Unlike
-- ``sync_runs`` (one row per run_key), this keeps every attempt so the sync
-- layer can count how many times in a row a location has failed.
CREATE TABLE IF NOT EXISTS sync_run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key     TEXT,
    location_id TEXT,
    status      TEXT,
    fetched     INTEGER DEFAULT 0,
    upserted    INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    started_at  TEXT,
    ended_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_run_log_location
    ON sync_run_log(location_id, id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    actor       TEXT,
    action      TEXT,
    entity      TEXT,
    entity_id   TEXT,
    detail_json TEXT
);

CREATE TABLE IF NOT EXISTS api_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL DEFAULT (datetime('now')),
    endpoint      TEXT,
    status_code   INTEGER,
    retry_count   INTEGER DEFAULT 0,
    latency_ms    INTEGER,
    calls         INTEGER DEFAULT 0,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    est_cost_usd  REAL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Group 6B -- persistent authentication/authorization state.
-- Added backward compatibly (CREATE ... IF NOT EXISTS, so existing DBs are
-- fine). Counters and sessions live in SQLite so a restart or a second worker
-- process observes the same sessions and the same rate-limit windows.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    password_hash   TEXT NOT NULL,                 -- pbkdf2_sha256$iters$salt$hash
    salt            TEXT,
    roles_json      TEXT NOT NULL DEFAULT '[]',
    locations_json  TEXT NOT NULL DEFAULT '[]',
    active          INTEGER NOT NULL DEFAULT 1,
    mfa_required    INTEGER NOT NULL DEFAULT 0,    -- readiness metadata ONLY
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    jti         TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    issued_at   INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    revoked_at  INTEGER,
    ip          TEXT,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_username ON sessions(username);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket        TEXT NOT NULL,
    window_start  INTEGER NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, window_start)
);

-- Login-failure ledger (Group 8B / T25). Two scopes:
--   scope='user_ip'  key='<username>|<ip>'  -> throttles ONE source, so an
--                    attacker on one address cannot lock everyone else out
--   scope='user'     key='<username>'       -> only ever set when failures
--                    arrive from several distinct addresses (see AuthService)
CREATE TABLE IF NOT EXISTS login_guard (
    scope           TEXT NOT NULL,
    key             TEXT NOT NULL,
    failures        INTEGER NOT NULL DEFAULT 0,
    window_start    INTEGER,
    last_failure_at INTEGER,
    locked_until    INTEGER,
    PRIMARY KEY (scope, key)
);