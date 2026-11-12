"""REST API for the web UI and scripts. Everything except /healthz needs a caller."""
from __future__ import annotations

import json
import secrets
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from .. import modes
from ..config import EDITABLE, KEY_BY_NAME, ConfigError, coerce, validate
from ..engine.roster import RosterError, build_specs
from ..sources import SourceError, parse_context
from ..store import stats
from . import limits
from .auth import Caller, hash_token, new_api_token
from .mcp import synthesis_view
from .queue import effective_settings

if TYPE_CHECKING:
    from ..config import Settings
    from ..store.db import Store
    from .auth import Auth
    from .queue import Worker


class ReviewIn(BaseModel):
    repo: str = Field(min_length=3)
    pr: int | None = None
    branch: str | None = None
    base: str | None = None
    roster: str | None = None
    focus: str | None = None
    no_synth: bool = False
    mode: str = Field(default="full", pattern="^(quick|full|deep)$")
    context_repos: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("repo", "branch", "base", "roster", "focus", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return (v.strip() or None) if isinstance(v, str) else v


class SparIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=50_000)
    roster: str | None = None
    no_synth: bool = False
    mode: str = Field(default="full", pattern="^(quick|full|deep)$")


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def job_view(job: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in job.items() if k not in ("result_md", "synthesis_md",
                                                     "outcomes_json", "options_json")}
    out["outcomes"] = json.loads(job["outcomes_json"]) if job.get("outcomes_json") else {}
    out["options"] = json.loads(job["options_json"]) if job.get("options_json") else {}
    out["partial"] = bool(job.get("partial"))
    return out


def create_router(settings: Settings, store: Store, worker: Worker, auth: Auth) -> APIRouter:
    def caller(request: Request) -> Caller:
        c = auth.require(request)
        store.touch_user(c.login)
        return c

    api = APIRouter(prefix="/api", dependencies=[Depends(caller)])

    def current() -> Settings:
        return effective_settings(settings, store)

    def check_roster(roster: str | None, mode: str = "full") -> str:
        s = current()
        r = roster or modes.roster_for(mode, s)
        try:
            build_specs(r, s)
        except RosterError as e:
            raise HTTPException(422, str(e)) from e
        return r

    def own_job(job_id: str, c: Caller) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"no such job: {job_id}")
        if ((job.get("requested_by") or "").lower() != (c.login or "").lower()
                and not limits.is_admin(current(), c.login)):
            raise HTTPException(403, "this is not your job")
        return job

    def require_manager(c: Caller) -> None:
        if not limits.can_manage(current(), c.login):
            raise HTTPException(403, "only admins can do this (see auth.admins)")

    def admit(c: Caller, roster: str, mode: str, fields: dict[str, Any]) -> dict[str, Any]:
        try:
            return limits.admit(current(), store, c.login, roster, mode, fields)
        except limits.NotSignedIn as e:
            raise HTTPException(401, str(e)) from e
        except limits.LimitHit as e:
            headers = {"Retry-After": str(e.retry_after_s)} if e.retry_after_s else None
            raise HTTPException(429, e.message, headers=headers) from e

    @api.get("/version")
    def version() -> dict[str, Any]:
        """The running version and whether a newer release is out."""
        return worker.update_info.to_dict()

    @api.get("/me")
    def me(c: Caller = Depends(caller)) -> dict[str, Any]:
        s = current()
        return {"login": c.login, "via": c.via, "admin": limits.is_admin(s, c.login),
                "can_manage": limits.can_manage(s, c.login)}

    # -- jobs ---------------------------------------------------------------

    @api.post("/jobs/review", status_code=202)
    def submit_review(body: ReviewIn, c: Caller = Depends(caller)) -> dict[str, Any]:
        if body.pr is None and not body.branch:
            raise HTTPException(422, "give a PR number or a head branch")
        try:
            ctx = [f"{r}@{ref}" if ref else r for r, ref in parse_context(body.context_repos)]
        except SourceError as e:
            raise HTTPException(422, str(e)) from e
        roster = check_roster(body.roster, body.mode)
        job = admit(c, roster, body.mode, dict(
            kind="review", requested_by=c.login, source="api",
            repo=body.repo, pr=body.pr, head_ref=body.branch,
            base_ref=body.base, roster=roster, prompt=body.focus,
            options_json=json.dumps({"no_synth": body.no_synth, "mode": body.mode,
                                     "context": ctx})))
        worker.notify()
        return job_view(job)

    @api.post("/jobs/spar", status_code=202)
    def submit_spar(body: SparIn, c: Caller = Depends(caller)) -> dict[str, Any]:
        roster = check_roster(body.roster, body.mode)
        job = admit(c, roster, body.mode, dict(
            kind="spar", requested_by=c.login, source="api", prompt=body.prompt.strip(),
            roster=roster, options_json=json.dumps({"no_synth": body.no_synth, "mode": body.mode})))
        worker.notify()
        return job_view(job)

    @api.get("/jobs")
    def list_jobs(limit: int = 50, kind: str | None = None,
                  status: str | None = None) -> list[dict[str, Any]]:
        return [job_view(j) for j in store.list_jobs(limit, kind, status)]

    @api.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"no such job: {job_id}")
        runs = [{k: v for k, v in r.items() if k != "review_md"} | {"has_review": bool(r["review_md"])}
                for r in store.runs_for(job_id)]
        return {**job_view(job), "synthesis": job.get("synthesis_md"),
                "runs": runs, "synth": store.synth_for(job_id)}

    @api.get("/jobs/{job_id}/result", response_class=PlainTextResponse)
    def get_result(job_id: str, full: bool = True) -> str:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"no such job: {job_id}")
        if not job.get("result_md"):
            raise HTTPException(409, f"job {job_id} is '{job['status']}', no result yet")
        return job["result_md"] if full else synthesis_view(job)

    @api.get("/jobs/{job_id}/runs/{run_id}", response_class=PlainTextResponse)
    def get_review(job_id: str, run_id: int) -> str:
        for r in store.runs_for(job_id):
            if r["id"] == run_id:
                return r["review_md"] or f"(no review: {r['outcome']} — {r['failure_reason']})"
        raise HTTPException(404, "no such review")

    @api.get("/jobs/{job_id}/events")
    def get_events(job_id: str, after: int = 0) -> list[dict[str, Any]]:
        return [{**e, "payload": json.loads(e.pop("payload_json") or "{}")}
                for e in store.events_for(job_id, after)]

    @api.post("/jobs/{job_id}/cancel")
    async def cancel(job_id: str, c: Caller = Depends(caller)) -> dict[str, Any]:
        own_job(job_id, c)
        ok = await worker.cancel(job_id)
        store.audit(c.login, "job_cancel", {"job": job_id})
        return {"cancelled": ok, **job_view(store.get_job(job_id) or {})}

    @api.delete("/jobs/{job_id}")
    async def delete(job_id: str, c: Caller = Depends(caller)) -> dict[str, Any]:
        job = own_job(job_id, c)
        if job["status"] in ("queued", "running"):
            await worker.cancel(job_id)
        store.delete_job(job_id, keep_telemetry=settings["retention.keep_telemetry"])
        store.audit(c.login, "job_delete", {"job": job_id})
        return {"deleted": job_id}

    # -- stats ----------------------------------------------------------------

    @api.get("/usage")
    def usage(history: bool = False, c: Caller = Depends(caller)) -> dict[str, Any]:
        """Plan usage of the reviewer logins, per provider: the newest successful
        reading, and the newest error when it came later. `history`: 7 days of readings."""
        interval = int(current()["usage.interval_min"])
        # A provider that is no longer read (logged out, probe off) goes stale and drops out.
        if not interval:
            return {"interval_min": 0, "providers": [], **({"history": []} if history else {})}
        fresh = time.time() - max(3 * interval * 60, 3600)
        rows = store.usage_since(time.time() - 7 * 86400 if history else fresh)
        providers: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["ts"] < fresh:
                continue
            p = providers.setdefault(r["provider"], {"provider": r["provider"], "read_at": None,
                                                     "windows": [], "error": None})
            if r["error"]:
                p["error"], p["error_at"] = r["error"], r["ts"]
            else:
                if r["ts"] != p["read_at"]:
                    p["read_at"], p["windows"] = r["ts"], []
                p["windows"].append({k: r[k] for k in ("window", "used_percent", "resets_at",
                                                       "plan")})
        for p in providers.values():
            if p["read_at"] and p.get("error_at", 0) < p["read_at"]:
                p["error"] = None
            p.pop("error_at", None)
        out: dict[str, Any] = {"interval_min": interval, "providers": list(providers.values())}
        if history:
            out["history"] = [r for r in rows if not r["error"]]
        return out

    @api.get("/stats")
    def get_stats(days: int = 30, c: Caller = Depends(caller)) -> dict[str, Any]:
        since = time.time() - max(1, min(days, 3650)) * 86400
        return {"days": days, "jobs": stats.job_stats(store.job_rows(since)),
                "reviewers": stats.reviewer_stats(store.run_rows(since)),
                # Everyone's usage only for admins; others see their own row.
                "users": [u for u in store.user_rows(since)
                          if limits.is_admin(current(), c.login)
                          or (u["login"] or "").lower() == (c.login or "").lower()]}

    # -- settings (review defaults editable in the UI) -------------------------

    @api.get("/settings")
    def get_settings() -> dict[str, Any]:
        s = current()
        rows = s.public()
        return {"editable": list(EDITABLE), "settings": rows,
                "public_urls": s["server.public_urls"]}

    @api.put("/settings")
    def put_settings(body: dict[str, Any], c: Caller = Depends(caller)) -> dict[str, Any]:
        require_manager(c)
        changes: dict[str, Any] = {}
        for name, value in body.items():
            if name not in EDITABLE:
                raise HTTPException(422, f"{name} cannot be changed here")
            try:
                changes[name] = None if value is None else coerce(KEY_BY_NAME[name], value)
            except ConfigError as e:
                raise HTTPException(422, str(e)) from e
        # Validate the result as a whole before saving anything.
        trial = {**store.get_overrides(), **{k: v for k, v in changes.items() if v is not None}}
        for k, v in changes.items():
            if v is None:
                trial.pop(k, None)
        try:
            validate(settings.with_layer("ui", trial, allow_secrets=False))
        except (ConfigError, RosterError) as e:
            raise HTTPException(422, str(e)) from e
        for k, v in changes.items():
            if v is None:
                store.clear_override(k)
            else:
                store.set_override(k, v, c.login)
        store.audit(c.login, "settings_change", {k: v for k, v in changes.items()})
        return get_settings()

    # -- tokens and audit -------------------------------------------------------

    @api.get("/tokens")
    def list_tokens(c: Caller = Depends(caller)) -> list[dict[str, Any]]:
        return store.tokens_for(c.login)

    @api.post("/tokens", status_code=201)
    def create_token(body: TokenIn, c: Caller = Depends(caller)) -> dict[str, Any]:
        token = new_api_token()
        token_id = "t-" + secrets.token_hex(4)
        store.add_token(token_id, c.login, body.name.strip(), hash_token(token))
        store.audit(c.login, "token_create", {"id": token_id, "name": body.name})
        return {"id": token_id, "name": body.name, "token": token,
                "note": "Shown once. Store it now."}

    @api.delete("/tokens/{token_id}")
    def revoke_token(token_id: str, c: Caller = Depends(caller)) -> dict[str, Any]:
        if not store.revoke_token(token_id, c.login):
            raise HTTPException(404, "no such active token")
        store.audit(c.login, "token_revoke", {"id": token_id})
        return {"revoked": token_id}

    @api.get("/audit")
    def audit(limit: int = 100, c: Caller = Depends(caller)) -> list[dict[str, Any]]:
        """Admins see every audit event; everyone else sees their own."""
        n = max(1, min(limit, 1000))
        return store.audit_all(n) if limits.is_admin(current(), c.login) else store.audit_for(c.login, n)

    return api
