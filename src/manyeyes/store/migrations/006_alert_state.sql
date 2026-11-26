-- Quota alerts (alerts.py): a row means "this plan window has had its alert".
-- The row goes when the window drops well below the line or resets.
CREATE TABLE alert_state (
    provider   TEXT NOT NULL,
    login      TEXT NOT NULL,
    window     TEXT NOT NULL,
    alerted_at REAL NOT NULL,
    resets_at  REAL,
    PRIMARY KEY (provider, login, window)
);
