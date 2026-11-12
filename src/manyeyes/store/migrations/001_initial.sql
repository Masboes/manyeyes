CREATE TABLE jobs (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,              -- review | spar
    status        TEXT NOT NULL,              -- queued | running | done | failed | cancelled
    partial       INTEGER NOT NULL DEFAULT 0,
    requested_by  TEXT,                       -- GitHub login, or "local"
    source        TEXT NOT NULL DEFAULT 'api',-- api | mcp | ui | cli
    repo          TEXT,
    pr            INTEGER,
    head_ref      TEXT,
    base_ref      TEXT,
    head_sha      TEXT,
    base_sha      TEXT,
    prompt        TEXT,                       -- spar question, or extra review instructions
    roster        TEXT NOT NULL,
    options_json  TEXT,                       -- per-job options, e.g. {"no_synth": true}
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    error         TEXT,
    outcomes_json TEXT,
    synthesis_md  TEXT,
    result_md     TEXT
);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_created ON jobs(created_at);

CREATE TABLE reviewer_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    reviewer            TEXT NOT NULL,        -- codex#3
    kind                TEXT NOT NULL,        -- codex
    instance            INTEGER NOT NULL,
    model               TEXT NOT NULL,
    effort              TEXT NOT NULL,
    started_at          REAL NOT NULL,
    finished_at         REAL NOT NULL,
    duration_s          REAL NOT NULL,
    input_tokens        INTEGER,
    cached_input_tokens INTEGER,
    output_tokens       INTEGER,
    reasoning_tokens    INTEGER,
    outcome             TEXT NOT NULL,
    failure_reason      TEXT,
    failure_detail      TEXT,
    exit_code           INTEGER,
    review_md           TEXT
);
CREATE INDEX idx_runs_job ON reviewer_runs(job_id);
CREATE INDEX idx_runs_kind ON reviewer_runs(kind, model);

CREATE TABLE synth_runs (
    job_id           TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    model            TEXT NOT NULL,
    duration_s       REAL NOT NULL,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    reasoning_tokens INTEGER,
    outcome          TEXT NOT NULL,
    failure_detail   TEXT,
    created_at       REAL NOT NULL
);

CREATE TABLE job_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ts           REAL NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT
);
CREATE INDEX idx_events_job ON job_events(job_id, id);

CREATE TABLE users (
    github_login TEXT PRIMARY KEY,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL
);

CREATE TABLE api_tokens (
    id           TEXT PRIMARY KEY,
    github_login TEXT NOT NULL,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   REAL NOT NULL,
    last_used_at REAL,
    revoked_at   REAL
);
CREATE INDEX idx_tokens_login ON api_tokens(github_login);

CREATE TABLE audit_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    github_login TEXT,
    action       TEXT NOT NULL,
    detail_json  TEXT
);
CREATE INDEX idx_audit_login ON audit_events(github_login, id);
CREATE INDEX idx_synth_job ON synth_runs(job_id);

-- Review defaults edited in the web UI. They override file and environment.
CREATE TABLE settings_overrides (
    name       TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_by TEXT,
    updated_at REAL NOT NULL
);
