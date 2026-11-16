-- More than one reviewer login per provider. "default" is the login in
-- auth/<kind>; another login lives in auth/<kind>@<name>.
ALTER TABLE usage_snapshots ADD COLUMN login TEXT NOT NULL DEFAULT 'default';
ALTER TABLE reviewer_runs ADD COLUMN login TEXT;

-- Per login: switched off by an admin, or resting after a rate limit.
CREATE TABLE logins (
    kind          TEXT NOT NULL,
    name          TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    resting_until REAL,
    updated_at    REAL NOT NULL,
    updated_by    TEXT,
    PRIMARY KEY (kind, name)
);
