"""MCP tools (streamable HTTP), compatible with the previous swarm server.

Same tool names, argument names and response keys as before, so existing
clients keep working. Review and spar BLOCK until the job is done and return
the synthesis; progress goes out every 10 s so no proxy or client idle timer
closes the stream.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from fastmcp import Context, FastMCP
from fastmcp.server.auth import AccessToken, MultiAuth, TokenVerifier
from fastmcp.server.dependencies import get_access_token

from ..store import stats
from ..store.db import TERMINAL

if TYPE_CHECKING:
    from ..config import Settings
    from ..store.db import Store
    from .auth import Auth
    from .queue import Worker

INSTRUCTIONS = (
    "This server is manyeyes (formerly called agent-swarm or the swarm; clients may still "
    "list it under that name — the tools are the same). "
    "Multi-agent code review (swarm_review) and free-form sparring (swarm_spar). Several "
    "independent reviewers work the same problem in parallel, then a synthesis step groups "
    "the findings by how many reviewers (and, with a mixed roster, how many provider "
    "families) agreed — that agreement is the signal worth weighting. BOTH TOOLS BLOCK "
    "until the run finishes and hand back the synthesis directly: a small spar takes under "
    "a minute, a review takes minutes (typically 5-15, and no reviewer may run beyond 20). "
    "Call them when you have a real question and can afford the wait, not twice on the same "
    "diff. Afterwards: swarm_ls to find past runs, swarm_get to re-read one, swarm_logs to "
    "debug a failure, swarm_stats for timings."
)
PROMPT_PREVIEW = 160
POLL_S = 2.0
PROGRESS_S = 10.0
HIDDEN = ("result_md", "synthesis_md", "outcomes_json")


class TokenCheck(TokenVerifier):
    """Personal API tokens and the bootstrap token, as MCP bearer tokens."""

    def __init__(self, auth: Auth, base_url: str | None) -> None:
        super().__init__(base_url=base_url)
        self.auth = auth

    async def verify_token(self, token: str) -> AccessToken | None:
        hit = self.auth.token_login(token)
        if hit is None:
            return None
        login, via = hit
        return AccessToken(token=token, client_id=via, scopes=[], expires_at=None,
                           claims={"login": login, "via": via})


def build_auth(settings: Settings, auth: Auth):
    urls = settings["server.public_urls"]
    base = f"{urls[0].rstrip('/')}/mcp" if urls else None
    verifier = TokenCheck(auth, base)
    if not (auth.github_enabled and settings["auth.mcp_oauth"] and base):
        return verifier
    import fastmcp
    from fastmcp.server.auth.providers.github import GitHubProvider

    # OAuth client registrations live on disk under fastmcp's home. Keep them
    # in the data dir so MCP sign-ins survive a container replacement. (The
    # FASTMCP_HOME variable would be read too early, at import.)
    fastmcp.settings.home = settings.data_dir / "fastmcp"

    class AllowlistGitHub(GitHubProvider):
        def _get_resource_url(self, path=None):
            # Advertise the resource without a trailing slash: clients normalize
            # the configured URL that way, and RFC 9728 compares it exactly.
            return self.resource_base_url or self.base_url

        async def _extract_upstream_claims(self, idp_tokens):
            tok = (idp_tokens or {}).get("access_token")
            if not tok:
                return None
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://api.github.com/user", headers={
                    "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
            return {"login": r.json().get("login")} if r.status_code == 200 else None

        async def verify_token(self, token: str) -> AccessToken | None:
            at = await super().verify_token(token)
            if at is None:
                return None
            claims = at.claims or {}
            login = claims.get("login") or (claims.get("upstream_claims") or {}).get("login")
            if not auth.allowlist.allows(login):
                return None
            claims["login"] = login
            claims["via"] = "mcp-oauth"
            at.claims = claims
            return at

    gh = AllowlistGitHub(
        client_id=settings["auth.github_client_id"],
        client_secret=settings["auth.github_client_secret"],
        base_url=base,
        redirect_path="/auth/callback",
        # No scopes: the GitHub /user call proves the token, the allowlist
        # decides access. Requiring scopes broke verification before.
        required_scopes=[],
        jwt_signing_key=auth.session_secret,
        require_authorization_consent="remember",
    )
    class NoSlashMultiAuth(MultiAuth):
        # The 401 challenge points clients at the resource metadata; build it
        # from the resource URL without a trailing slash, so the proxy needs
        # one exact location for it.
        def _get_resource_url(self, path=None):
            return gh._get_resource_url(path)

    return NoSlashMultiAuth(server=gh, verifiers=[verifier])


def _caller_login() -> str | None:
    at = get_access_token()
    return (at.claims or {}).get("login") if at is not None else None


def _row(job: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in job.items() if k not in HIDDEN}
    p = out.get("prompt")
    if isinstance(p, str) and len(p) > PROMPT_PREVIEW:
        out["prompt"] = p[:PROMPT_PREVIEW] + "… (truncated)"
    if job.get("outcomes_json"):
        out["outcomes"] = json.loads(job["outcomes_json"])
    out.pop("options_json", None)
    return out


def synthesis_view(job: dict[str, Any]) -> str:
    """The synthesis with a short header; the full result when there is none."""
    full = job.get("result_md") or ""
    synth = job.get("synthesis_md")
    if not synth:
        return full
    head = full.split("\n## Synthesis", 1)[0].rstrip()
    return (f"{head}\n\n## Synthesis\n\n{synth}\n\n---\n_Synthesis only. Call swarm_get "
            "with full=True for every reviewer's complete review._\n")


def create_mcp(settings: Settings, store: Store, worker: Worker, auth: Auth) -> FastMCP:
    mcp = FastMCP("manyeyes", instructions=INSTRUCTIONS, auth=build_auth(settings, auth))

    def wait_budget(job: dict[str, Any] | None = None) -> float:
        from .. import modes
        limit = float(settings["limits.job_timeout_s"])
        if job and json.loads(job.get("options_json") or "{}").get("mode") == "deep":
            limit = float(modes.deep_limits(settings)["limits.job_timeout_s"])
        return limit + 120.0

    async def await_job(job_id: str, ctx: Context | None) -> dict[str, Any]:
        t0 = time.monotonic()
        last_progress = 0.0
        run_started: float | None = None
        while True:
            job = store.get_job(job_id) or {}
            status = job.get("status")
            waited = time.monotonic() - t0
            if status in TERMINAL:
                out: dict[str, Any] = {"id": job_id, "status": status,
                                       "waited_seconds": round(waited),
                                       "partial": bool(job.get("partial"))}
                if job.get("outcomes_json"):
                    out["outcomes"] = json.loads(job["outcomes_json"])
                if status == "done":
                    out["synthesis"] = synthesis_view(job)
                else:
                    out["error"] = job.get("error")
                return out
            if status == "running" and run_started is None:
                run_started = time.monotonic()
            # Queue time does not eat the budget; running time does.
            ran = time.monotonic() - run_started if run_started else 0.0
            if ran > wait_budget(job) or waited > 3 * wait_budget(job):
                return {"id": job_id, "status": status, "waited_seconds": round(waited),
                        "note": f"still {status} after {round(waited)}s — the run continues on "
                                "the server. Poll swarm_status, then swarm_get."}
            if ctx is not None and waited - last_progress >= PROGRESS_S:
                last_progress = waited
                done = sum(1 for e in store.events_for(job_id) if e["type"] == "reviewer_finished")
                await ctx.report_progress(progress=round(waited), total=None,
                                          message=f"{job_id}: {status}, {done} reviews in, "
                                                  f"{round(waited)}s")
            await asyncio.sleep(POLL_S)

    def submit(**fields: Any) -> str:
        from . import limits
        from .queue import effective_settings
        login = _caller_login()
        opts = json.loads(fields.get("options_json") or "{}")
        try:
            job = limits.admit(effective_settings(settings, store), store, login,
                               fields["roster"], opts.get("mode", "full"),
                               {**fields, "requested_by": login, "source": "mcp"})
        except (limits.LimitHit, limits.NotSignedIn) as e:
            raise ValueError(str(e)) from e
        worker.notify()
        return job["id"]

    def roster_of(agents: str | None, mode: str = "full") -> str:
        from .. import modes
        from ..engine.roster import build_specs
        from .queue import effective_settings
        modes.check(mode)
        s = effective_settings(settings, store)
        roster = (agents or "").strip() or modes.roster_for(mode, s)
        build_specs(roster, s)   # raises a clear error for a bad roster
        return roster

    @mcp.tool
    async def swarm_review(repo: str, pr: int | None = None, branch: str | None = None,
                           base: str | None = None, agents: str | None = None,
                           no_synth: bool = False, focus: str | None = None,
                           mode: str = "full", context_repos: list[str] | None = None,
                           ctx: Context = None) -> dict:
        """Review a GitHub PR or branch with a swarm of independent reviewers and return the
        synthesis, which groups findings by how many reviewers agreed.

        THIS CALL BLOCKS until the review is finished: minutes, typically 5-15; no reviewer
        may run beyond 20. Budget for the wait, do not call it for a trivial question, and do
        not call it twice on the same diff.

        `repo` is owner/name. Give EITHER `pr` (a PR number) OR `branch` (head) with an
        optional `base` (default: the repository's default branch). `agents` is an optional
        roster such as "codex:10" or "codex:8,claude:2". `focus` adds instructions for the
        reviewers. `no_synth` skips the synthesis.

        `mode` — how much of the swarm to use:
        - "full" (default, the normal choice): Codex and Claude reviewers with the standard
          prompt, about 1-2 minutes. Use it for an ordinary review before merging.
        - "quick": a few reviewers, about a minute. Use it while iterating; it finds fewer issues.
        - "deep": thorough prompt — reviewers read every changed file in full and trace
          callers and dependencies. About 3-5 minutes and roughly 5x the tokens of a full run,
          so it is EXPENSIVE: use it only for risky changes (concurrency, auth, data
          migrations, timeouts, behaviour that depends on third-party libraries), not by default.

        `context_repos` — optional, at most 5, as "owner/name" or "owner/name@branch": other
        repositories the reviewers get read-only, to check whether this change breaks
        something there (a caller, a shared API contract, an event or message shape, a
        schema). Pass only repos this change actually talks to; each one adds reading time.

        Returns {id, status, waited_seconds, synthesis}. If your client gives up first, the
        run still completes on the server: find it with swarm_ls, read it with swarm_get."""
        from ..sources import SourceError, parse_context
        repo = (repo or "").strip()
        try:
            context = [f"{r}@{ref}" if ref else r for r, ref in parse_context(context_repos)]
        except SourceError as e:
            raise ValueError(str(e)) from e
        branch = (branch or "").strip() or None
        if not repo:
            raise ValueError("repo is required (owner/name)")
        if pr is None and not branch:
            raise ValueError("give a pr number or a head branch")
        # Admission takes a lock and writes SQLite: keep it off the event loop.
        job_id = await asyncio.to_thread(
            submit, kind="review", repo=repo, pr=pr, head_ref=branch,
            base_ref=(base or "").strip() or None, roster=roster_of(agents, mode),
            prompt=((focus or "").strip() or None),
            options_json=json.dumps({"no_synth": bool(no_synth), "mode": mode,
                                     "context": context}))
        return {"kind": "review", **await await_job(job_id, ctx)}

    @mcp.tool
    async def swarm_spar(prompt: str, agents: str | None = None, no_synth: bool = False,
                         mode: str = "full", ctx: Context = None) -> dict:
        """Ask several independent reviewers the same question and return the synthesis,
        which merges and contrasts their answers. Use for design debates and second opinions.

        THIS CALL BLOCKS until every reviewer has answered. A short question with a small
        roster ("codex:3") comes back in under a minute. `mode` works as in swarm_review:
        "full" (default), "quick", or "deep" (expensive; rarely needed for a question).

        Returns {id, status, waited_seconds, synthesis}."""
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        job_id = await asyncio.to_thread(
            submit, kind="spar", prompt=prompt, roster=roster_of(agents, mode),
            options_json=json.dumps({"no_synth": bool(no_synth), "mode": mode}))
        return {"kind": "spar", **await await_job(job_id, ctx)}

    @mcp.tool
    def swarm_ls(limit: int = 20) -> list[dict]:
        """List recent jobs, newest first (id, kind, status, target, timestamps)."""
        return [_row(j) for j in store.list_jobs(max(1, min(limit, 200)))]

    @mcp.tool
    def swarm_status(id: str) -> dict:
        """One job's status (queued/running/done/failed/cancelled) and metadata."""
        job = store.get_job(id)
        if job is None:
            raise ValueError(f"no such job: {id}")
        return _row(job)

    @mcp.tool
    def swarm_get(id: str, full: bool = False) -> str:
        """A finished job's result: the synthesis. `full=True` adds every reviewer's complete
        review (about five times longer); use it only to check one reviewer's reasoning."""
        job = store.get_job(id)
        if job is None:
            raise ValueError(f"no such job: {id}")
        if job["status"] != "done":
            raise ValueError(f"job {id} is '{job['status']}', not done"
                             + (f": {job['error']}" if job.get("error") else ""))
        return (job.get("result_md") or "") if full else synthesis_view(job)

    @mcp.tool
    def swarm_logs(id: str) -> str:
        """A job's progress log: reviewer starts and finishes, failures and their reasons."""
        job = store.get_job(id)
        if job is None:
            raise ValueError(f"no such job: {id}")
        lines = []
        for e in store.events_for(id):
            ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
            lines.append(f"{ts} {e['type']} {e['payload_json']}")
        if job.get("error"):
            lines.append(f"error: {job['error']}")
        text = "\n".join(lines)
        return text[-20000:]

    @mcp.tool
    async def swarm_cancel(id: str) -> dict:
        """Cancel one of your own queued or running jobs."""
        job = store.get_job(id)
        if job is None:
            raise ValueError(f"no such job: {id}")
        login = _caller_login()
        from . import limits
        if ((job.get("requested_by") or "").lower() != (login or "").lower()
                and not limits.is_admin(settings, login)):
            raise ValueError("you can cancel only your own jobs")
        ok = await worker.cancel(id)
        store.audit(login, "job_cancel", {"job": id, "via": "mcp"})
        return {"id": id, "cancelled": ok, "status": (store.get_job(id) or {}).get("status")}

    @mcp.tool
    def swarm_stats(days: int = 30) -> dict:
        """Timings and outcomes over recent jobs: per reviewer model, success rate,
        duration, tokens and the top failure reasons."""
        since = time.time() - max(1, days) * 86400
        return {"jobs": stats.job_stats(store.job_rows(since)),
                "reviewers": stats.reviewer_stats(store.run_rows(since))}

    return mcp
