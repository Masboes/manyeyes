"""SQLite store shared by the CLI and the server.

One short-lived connection per call (safe across threads), WAL, and a process
lock around writes. Schema changes are numbered SQL files in `migrations/`,
tracked with PRAGMA user_version.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from ..engine.models import ReviewerRun, SwarmResult, SynthRun

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
TERMINAL = frozenset({DONE, FAILED, CANCELLED})

JOB_COLUMNS = (
    "id", "kind", "status", "partial", "requested_by", "source", "repo", "pr", "head_ref",
    "base_ref", "head_sha", "base_sha", "prompt", "roster", "options_json", "created_at",
    "started_at",
    "finished_at", "error", "outcomes_json", "synthesis_md", "result_md",
)


def new_job_id(kind: str) -> str:
    return f"{'r' if kind == 'review' else 's'}-{secrets.token_hex(4)}"


def _migrations() -> list[tuple[int, str]]:
    out = []
    for f in resources.files("manyeyes.store.migrations").iterdir():
        if f.name.endswith(".sql"):
            out.append((int(f.name.split("_", 1)[0]), f.read_text()))
    return sorted(out)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        import fcntl
        with self._lock, open(str(path) + ".lock", "w") as lockf:
            # Serializes migrations across processes (CLI and server at once).
            fcntl.flock(lockf, fcntl.LOCK_EX)
            conn = sqlite3.connect(str(path), timeout=30.0)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                for number, sql in _migrations():
                    if number > version:
                        # Script and version bump commit together.
                        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version={number};\nCOMMIT;")
            finally:
                conn.close()
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            # IMMEDIATE: take the write lock up front, so a read-then-write
            # (claim_next) waits on busy_timeout instead of failing.
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _write(self, sql: str, params: tuple | list = ()) -> int:
        with self._lock, self._conn() as c:
            return c.execute(sql, params).rowcount

    def _all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    # -- jobs ---------------------------------------------------------------

    def create_job(self, charge: tuple[str, str, int] | None = None,
                   **fields: Any) -> dict[str, Any]:
        """Insert a job. With `charge` = (login, mode, reviewer_runs), also record the
        usage in the ledger, in the same transaction."""
        fields.setdefault("id", new_job_id(fields["kind"]))
        fields.setdefault("status", QUEUED)
        fields.setdefault("created_at", time.time())
        cols = [c for c in JOB_COLUMNS if c in fields]
        with self._lock, self._conn() as c:
            c.execute(f"INSERT INTO jobs ({', '.join(cols)}) VALUES "
                      f"({', '.join('?' for _ in cols)})", [fields[k] for k in cols])
            if charge:
                login, mode, runs = charge
                c.execute("INSERT INTO usage_ledger (ts, github_login, job_id, mode, "
                          "reviewer_runs) VALUES (?,?,?,?,?)",
                          (fields["created_at"], login.lower(), fields["id"], mode, runs))
        return self.get_job(fields["id"])  # type: ignore[return-value]

    def update_job(self, job_id: str, **fields: Any) -> None:
        unknown = set(fields) - set(JOB_COLUMNS)
        if unknown:
            raise ValueError(f"unknown job columns: {sorted(unknown)}")
        if fields:
            self._write(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                        [*fields.values(), job_id])

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def list_jobs(self, limit: int = 50, kind: str | None = None,
                  status: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        if kind:
            where.append("kind=?")
            params.append(kind)
        if status:
            where.append("status=?")
            params.append(status)
        sql = "SELECT * FROM jobs" + (f" WHERE {' AND '.join(where)}" if where else "")
        sql += " ORDER BY created_at DESC LIMIT ?"
        return self._all(sql, [*params, max(1, min(limit, 500))])

    def claim_next(self, max_per_user: int = 0,
                   exempt: frozenset[str] = frozenset()) -> dict[str, Any] | None:
        """Claim the oldest queued job. With `max_per_user`, skip people who already
        have that many jobs running (except `exempt` logins, lowercase), so one
        person's batch cannot hold the queue."""
        with self._lock, self._conn() as c:
            if max_per_user > 0:
                # Jobs without an owner (none come from the API or MCP) are not capped.
                marks = ",".join("?" for _ in exempt) or "NULL"
                row = c.execute(
                    "SELECT q.id FROM jobs q WHERE q.status=? AND ("
                    f"q.requested_by IS NULL OR LOWER(q.requested_by) IN ({marks}) OR "
                    "(SELECT COUNT(*) FROM jobs r WHERE r.status=? AND r.source != 'cli' AND "
                    "LOWER(r.requested_by) = LOWER(q.requested_by)) < ?) "
                    "ORDER BY q.created_at, q.id LIMIT 1",
                    (QUEUED, *sorted(exempt), RUNNING, max_per_user)).fetchone()
            else:
                row = c.execute("SELECT id FROM jobs WHERE status=? ORDER BY created_at, id "
                                "LIMIT 1", (QUEUED,)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE jobs SET status=?, started_at=? WHERE id=?",
                      (RUNNING, time.time(), row["id"]))
            return dict(c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def reconcile_orphans(self) -> int:
        # Only server jobs: a CLI run on the same data dir is not ours to fail.
        return self._write("UPDATE jobs SET status=?, error=?, finished_at=? "
                           "WHERE status=? AND source != 'cli'",
                           (FAILED, "server restarted while this job was running",
                            time.time(), RUNNING))

    def delete_job(self, job_id: str, keep_telemetry: bool = True) -> bool:
        with self._lock, self._conn() as c:
            if keep_telemetry:
                c.execute("UPDATE reviewer_runs SET review_md=NULL, failure_detail=NULL "
                          "WHERE job_id=?", (job_id,))
            else:
                c.execute("DELETE FROM reviewer_runs WHERE job_id=?", (job_id,))
                c.execute("DELETE FROM synth_runs WHERE job_id=?", (job_id,))
            return c.execute("DELETE FROM jobs WHERE id=?", (job_id,)).rowcount > 0

    def jobs_older_than(self, cutoff: float) -> list[str]:
        return [r["id"] for r in self._all(
            "SELECT id FROM jobs WHERE created_at < ? AND status IN (?,?,?)",
            (cutoff, DONE, FAILED, CANCELLED))]

    # -- runs and events ----------------------------------------------------

    def record_run(self, job_id: str | None, run: ReviewerRun) -> None:
        u = run.usage
        self._write(
            "INSERT INTO reviewer_runs (job_id, reviewer, kind, instance, model, effort, "
            "started_at, finished_at, duration_s, input_tokens, cached_input_tokens, "
            "output_tokens, reasoning_tokens, outcome, failure_reason, failure_detail, "
            "exit_code, review_md, model_resolved) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, run.spec.name, run.spec.kind, run.spec.instance, run.spec.model,
             run.spec.effort, run.started_at, run.finished_at, run.duration_s, u.input_tokens,
             u.cached_input_tokens, u.output_tokens, u.reasoning_tokens, str(run.outcome),
             run.failure_reason or None, run.failure_detail or None, run.exit_code,
             run.review if run.ok else None, run.model_resolved or None))

    def record_synth(self, job_id: str | None, s: SynthRun) -> None:
        self._write(
            "INSERT INTO synth_runs (job_id, model, duration_s, input_tokens, output_tokens, "
            "reasoning_tokens, outcome, failure_detail, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, s.model, s.duration_s, s.usage.input_tokens, s.usage.output_tokens,
             s.usage.reasoning_tokens, s.outcome, s.failure_detail or None, time.time()))

    def finish_job(self, job_id: str, result: SwarmResult, result_md: str) -> None:
        s = result.synth
        self.update_job(
            job_id, status=result.status, partial=int(result.partial),
            finished_at=time.time(), outcomes_json=json.dumps(result.outcome_counts),
            synthesis_md=s.text if s and s.outcome == "success" else None,
            result_md=result_md,
            error=result.failure_summary() or None)

    def runs_for(self, job_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM reviewer_runs WHERE job_id=? ORDER BY kind, instance",
                         (job_id,))

    def synth_for(self, job_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM synth_runs WHERE job_id=? ORDER BY created_at DESC",
                         (job_id,))

    def add_event(self, job_id: str, type_: str, payload: dict[str, Any] | None = None) -> None:
        self._write("INSERT INTO job_events (job_id, ts, type, payload_json) VALUES (?,?,?,?)",
                    (job_id, time.time(), type_, json.dumps(payload or {}, default=str)))

    def events_for(self, job_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id",
                         (job_id, after_id))

    # -- stats ----------------------------------------------------------------

    def run_rows(self, since: float = 0.0) -> list[dict[str, Any]]:
        return self._all(
            "SELECT kind, COALESCE(model_resolved, model) AS model, effort, outcome, duration_s, "
            "input_tokens, output_tokens, "
            "reasoning_tokens, failure_reason FROM reviewer_runs WHERE started_at >= ?",
            (since,))

    def job_rows(self, since: float = 0.0) -> list[dict[str, Any]]:
        return self._all("SELECT kind, status, partial, created_at, started_at, finished_at "
                         "FROM jobs WHERE created_at >= ?", (since,))

    # -- settings overrides (web UI) ---------------------------------------

    def get_overrides(self) -> dict[str, Any]:
        return {r["name"]: json.loads(r["value_json"])
                for r in self._all("SELECT name, value_json FROM settings_overrides")}

    def set_override(self, name: str, value: Any, by: str | None) -> None:
        self._write("INSERT INTO settings_overrides (name, value_json, updated_by, updated_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded."
                    "value_json, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (name, json.dumps(value), by, time.time()))

    def clear_override(self, name: str) -> None:
        self._write("DELETE FROM settings_overrides WHERE name=?", (name,))

    # -- users, tokens, audit (server) ---------------------------------------

    def touch_user(self, login: str) -> None:
        now = time.time()
        self._write("INSERT INTO users (github_login, first_seen, last_seen) VALUES (?,?,?) "
                    "ON CONFLICT(github_login) DO UPDATE SET last_seen=excluded.last_seen",
                    (login, now, now))

    def audit(self, login: str | None, action: str, detail: dict[str, Any] | None = None) -> None:
        self._write("INSERT INTO audit_events (ts, github_login, action, detail_json) "
                    "VALUES (?,?,?,?)", (time.time(), login, action, json.dumps(detail or {})))

    def ledger_since(self, login: str, since: float) -> list[dict[str, Any]]:
        """Usage entries of `login` (any casing) since `since`, oldest first."""
        return self._all("SELECT ts, mode, reviewer_runs FROM usage_ledger "
                         "WHERE github_login=? AND ts>=? ORDER BY ts", (login.lower(), since))

    def prune_ledger(self, before: float) -> int:
        return self._write("DELETE FROM usage_ledger WHERE ts<?", (before,))

    def add_usage(self, windows: list[dict[str, Any]], ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        with self._lock, self._conn() as c:
            c.executemany("INSERT INTO usage_snapshots (ts, provider, window, used_percent, "
                          "resets_at, plan, error) VALUES (?,?,?,?,?,?,?)",
                          [(ts, w["provider"], w["window"], w.get("used_percent"),
                            w.get("resets_at"), w.get("plan"), w.get("error")) for w in windows])

    def usage_since(self, since: float) -> list[dict[str, Any]]:
        return self._all("SELECT ts, provider, window, used_percent, resets_at, plan, error "
                         "FROM usage_snapshots WHERE ts>=? ORDER BY ts", (since,))

    def prune_usage(self, before: float) -> int:
        return self._write("DELETE FROM usage_snapshots WHERE ts<?", (before,))

    def user_rows(self, since: float) -> list[dict[str, Any]]:
        """Per person: jobs, reviewer runs and tokens since `since`."""
        return self._all(
            "SELECT LOWER(j.requested_by) AS login, COUNT(DISTINCT j.id) AS jobs, "
            "COUNT(r.id) AS reviewer_runs, COALESCE(SUM(r.input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(r.output_tokens), 0) AS output_tokens "
            "FROM jobs j LEFT JOIN reviewer_runs r ON r.job_id = j.id "
            "WHERE j.created_at >= ? AND j.source != 'cli' "
            "GROUP BY LOWER(j.requested_by) ORDER BY reviewer_runs DESC", (since,))

    def audit_all(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,))

    def audit_for(self, login: str, limit: int = 100) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM audit_events WHERE LOWER(github_login)=LOWER(?) "
                         "ORDER BY id DESC "
                         "LIMIT ?", (login, limit))

    def add_token(self, token_id: str, login: str, name: str, token_hash: str) -> None:
        self._write("INSERT INTO api_tokens (id, github_login, name, token_hash, created_at) "
                    "VALUES (?,?,?,?,?)", (token_id, login, name, token_hash, time.time()))

    def token_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM api_tokens WHERE token_hash=? AND revoked_at IS NULL",
                         (token_hash,))

    def touch_token(self, token_id: str) -> None:
        self._write("UPDATE api_tokens SET last_used_at=? WHERE id=?", (time.time(), token_id))

    def tokens_for(self, login: str) -> list[dict[str, Any]]:
        return self._all("SELECT id, name, created_at, last_used_at, revoked_at FROM api_tokens "
                         "WHERE LOWER(github_login)=LOWER(?) ORDER BY created_at DESC", (login,))

    def revoke_token(self, token_id: str, login: str) -> bool:
        return self._write("UPDATE api_tokens SET revoked_at=? WHERE id=? AND LOWER(github_login)=LOWER(?) "
                           "AND revoked_at IS NULL", (time.time(), token_id, login)) > 0

    def forget_user(self, login: str) -> None:
        """Erase a person (login in any casing). Anonymous telemetry stays."""
        who = (login.lower(),)
        mine = "SELECT id FROM jobs WHERE LOWER(requested_by)=?"
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM api_tokens WHERE LOWER(github_login)=?", who)
            c.execute("DELETE FROM audit_events WHERE LOWER(github_login)=?", who)
            c.execute("DELETE FROM users WHERE LOWER(github_login)=?", who)
            c.execute("DELETE FROM usage_ledger WHERE github_login=?", who)
            c.execute("UPDATE reviewer_runs SET review_md=NULL, failure_detail=NULL, "
                      f"job_id=NULL WHERE job_id IN ({mine})", who)
            c.execute(f"UPDATE synth_runs SET job_id=NULL WHERE job_id IN ({mine})", who)
            c.execute("DELETE FROM jobs WHERE LOWER(requested_by)=?", who)
            c.execute("UPDATE settings_overrides SET updated_by='(forgotten)' "
                      "WHERE LOWER(updated_by)=?", who)
