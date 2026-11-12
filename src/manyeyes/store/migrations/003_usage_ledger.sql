-- What each person was charged at admission, for per-person limits. Append-only:
-- deleting a job must not give the quota back.
CREATE TABLE usage_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    github_login  TEXT NOT NULL,
    job_id        TEXT,
    mode          TEXT NOT NULL,
    reviewer_runs INTEGER NOT NULL
);
CREATE INDEX idx_ledger_login ON usage_ledger(github_login, ts);
