-- Provider quota readings (Codex and Claude plan windows), taken by the server.
CREATE TABLE usage_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    provider      TEXT NOT NULL,
    window        TEXT NOT NULL,
    used_percent  REAL,
    resets_at     REAL,
    plan          TEXT,
    error         TEXT
);
CREATE INDEX idx_usage_ts ON usage_snapshots(ts);
