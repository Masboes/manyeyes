"""Reviews started from a pull request comment that tags the GitHub App.

GitHub sends every new comment on the repositories where the App is installed
to POST /github/webhook. A comment on a pull request that tags the App
(`@<app-slug>`) queues a review of that pull request. When the job ends, the
App posts the synthesis as a comment on the pull request.

The App's tokens are short-lived installation tokens, limited to the one
repository. Like `github.token`, they are only given to git and to the
GitHub API calls in this process, never to a reviewer.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from ..config import Settings
    from ..store.db import Store
    from .auth import Auth
    from .queue import Worker

log = logging.getLogger("manyeyes.github_app")

API = "https://api.github.com"
# GitHub refuses comments above 65,536 characters; keep room for the header and footer.
MAX_COMMENT = 60_000
# Only people GitHub lists as owner, member or collaborator of the repository can
# start a review. On a public repository, strangers cannot spend the plan.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
MODES = ("quick", "full", "deep")


class GitHubAppError(RuntimeError):
    pass


@dataclass(frozen=True)
class Command:
    mode: str
    focus: str


def parse_command(body: str, slug: str) -> Command | None:
    """The command in a comment that tags `@slug`, or None when it does not tag it.

    Quoted lines (`> ...`) do not count, so a reply that quotes an older request
    does not start a second review. The text after the tag is the instruction:
    an optional word "review", an optional mode (quick, full, deep), then free
    text that goes to the reviewers as the focus.
    """
    if not slug:
        return None
    tag = re.compile(rf"(?<![\w@/.-])@{re.escape(slug)}(?![\w-])", re.IGNORECASE)
    lines = [ln for ln in (body or "").splitlines() if not ln.lstrip().startswith(">")]
    text = "\n".join(lines)
    m = tag.search(text)
    if m is None:
        return None
    rest = text[m.end():].strip()
    words = rest.split(maxsplit=1)
    if words and words[0].lower().strip(":,.") == "review":
        rest = words[1] if len(words) > 1 else ""
        words = rest.split(maxsplit=1)
    mode = "full"
    if words and words[0].lower().strip(":,.") in MODES:
        mode = words[0].lower().strip(":,.")
        rest = words[1] if len(words) > 1 else ""
    return Command(mode=mode, focus=rest.strip()[:4000])


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check GitHub's X-Hub-Signature-256 header."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[len("sha256="):])


_MENTION = re.compile(r"(`[^`\n]*`)|(?<![\w`])@(?=[A-Za-z0-9])")


def _defuse_mentions(text: str) -> str:
    """Stop `@name` in reviewer text from notifying GitHub users.

    Code (fenced blocks and inline spans) stays as it is: GitHub does not link
    mentions there. Elsewhere a zero-width space after the @ breaks the link.
    """
    parts = re.split(r"(^```.*?^```[^\n]*$)", text, flags=re.MULTILINE | re.DOTALL)
    for i in range(0, len(parts), 2):
        parts[i] = _MENTION.sub(lambda m: m.group(1) or "@​", parts[i])
    return "".join(parts)


def result_comment(job: dict[str, Any], job_url: str | None) -> str:
    """The comment the App posts when a job ends."""
    who = f"@{job['requested_by']}" if job.get("requested_by") else ""
    link = f"[Full result with every review]({job_url})" if job_url else ""
    status = job.get("status")
    if status == "done":
        sha = (job.get("head_sha") or "")[:7]
        head = f"{who} manyeyes reviewed commit {sha}.".strip() if sha else \
            f"{who} manyeyes finished the review.".strip()
        if job.get("partial"):
            head += " Some reviewers did not finish; the result is partial."
        body = job.get("synthesis_md") or job.get("result_md") or "(no result)"
        body = _defuse_mentions(body)
        room = MAX_COMMENT - len(head) - len(link) - 200
        if len(body) > room:
            body = body[:room].rstrip() + "\n\n… (cut: the result is too long for a comment)"
        return "\n\n".join(p for p in (head, body, "---\n" + link if link else "") if p)
    if status == "cancelled":
        msg = f"{who} The manyeyes review was cancelled."
    else:
        error = (job.get("error") or "unknown error").splitlines()[0][:500]
        msg = f"{who} The manyeyes review failed: {error}"
    return "\n\n".join(p for p in (msg.strip(), link) if p)


class GitHubApp:
    """GitHub App credentials and the few API calls the trigger needs."""

    def __init__(self, settings: Settings) -> None:
        self.app_id = str(settings["github_app.id"] or "")
        self.private_key = settings["github_app.private_key"] or ""
        self.webhook_secret = settings["github_app.webhook_secret"] or ""
        self._slug: str | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.private_key and self.webhook_secret)

    def seen(self, delivery_id: str | None) -> bool:
        """True when this webhook delivery was handled before (GitHub can redeliver)."""
        return bool(delivery_id) and delivery_id in self._seen

    def remember(self, delivery_id: str | None) -> None:
        if delivery_id:
            self._seen[delivery_id] = None
            while len(self._seen) > 2000:
                self._seen.popitem(last=False)

    def _jwt(self) -> str:
        import jwt
        now = int(time.time())
        # GitHub allows at most 10 minutes; start 60 s back for clock drift.
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id},
                          self.private_key, algorithm="RS256")

    async def _call(self, method: str, path: str, token: str,
                    body: dict[str, Any] | None = None) -> httpx.Response:
        headers = {"Accept": "application/vnd.github+json",
                   "Authorization": f"Bearer {token}",
                   "X-GitHub-Api-Version": "2022-11-28"}
        async with httpx.AsyncClient(timeout=30) as client:
            for delay in (1.0, 3.0, None):
                try:
                    r = await client.request(method, API + path, headers=headers, json=body)
                except httpx.TransportError as e:
                    if delay is None:
                        raise GitHubAppError(f"cannot reach GitHub: {type(e).__name__}") from e
                else:
                    if r.status_code < 500 or delay is None:
                        return r
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def slug(self) -> str:
        """The App's slug: the tag is `@<slug>`. Read once from GitHub."""
        if self._slug is None:
            r = await self._call("GET", "/app", self._jwt())
            if r.status_code != 200:
                raise GitHubAppError(f"GitHub returned {r.status_code} for the App")
            self._slug = str(r.json().get("slug") or "")
            if not self._slug:
                raise GitHubAppError("GitHub returned no slug for the App")
        return self._slug

    async def installation_token(self, installation_id: int, repo: str) -> str:
        """A token for one repository, valid for one hour."""
        name = repo.split("/", 1)[1]
        r = await self._call("POST", f"/app/installations/{int(installation_id)}/access_tokens",
                             self._jwt(), {"repositories": [name]})
        if r.status_code != 201:
            raise GitHubAppError(f"GitHub returned {r.status_code} for an installation "
                                 f"token for {repo}")
        return str(r.json()["token"])

    async def comment(self, installation_id: int, repo: str, number: int, body: str) -> None:
        token = await self.installation_token(installation_id, repo)
        r = await self._call("POST", f"/repos/{repo}/issues/{int(number)}/comments", token,
                             {"body": body})
        if r.status_code != 201:
            raise GitHubAppError(f"GitHub returned {r.status_code} for a comment on "
                                 f"{repo}#{number}")

    async def react(self, installation_id: int, repo: str, comment_id: int,
                    content: str = "eyes") -> None:
        token = await self.installation_token(installation_id, repo)
        r = await self._call("POST", f"/repos/{repo}/issues/comments/{int(comment_id)}/reactions",
                             token, {"content": content})
        if r.status_code not in (200, 201):
            raise GitHubAppError(f"GitHub returned {r.status_code} for a reaction")


def github_options(payload: dict[str, Any]) -> dict[str, Any]:
    """What the worker needs later to fetch the code and answer on the pull request."""
    return {"installation": int(payload["installation"]["id"]),
            "comment": int(payload["comment"]["id"])}


def job_url(settings: Settings, job_id: str) -> str | None:
    """The job's page in the web UI, when a public URL is configured."""
    urls = settings["server.public_urls"]
    return f"{urls[0].rstrip('/')}/#/jobs/{job_id}" if urls else None


def create_webhook_router(settings: Settings, store: Store, worker: Worker,
                          auth: Auth) -> APIRouter:
    from .. import modes
    from . import limits
    from .queue import effective_settings

    router = APIRouter()
    gh = worker.github
    background: set[asyncio.Task] = set()

    def later(coro, what: str) -> None:
        async def run() -> None:
            try:
                await coro
            except Exception as e:  # noqa: BLE001 - a lost reaction must not break anything
                log.warning("GitHub App: %s failed: %s", what, e)
        t = asyncio.create_task(run())
        background.add(t)
        t.add_done_callback(background.discard)

    async def handle(event: str, p: dict[str, Any]) -> str:
        if event != "issue_comment" or p.get("action") != "created":
            return "ignored: not a new comment"
        issue, comment, sender = p.get("issue") or {}, p.get("comment") or {}, p.get("sender") or {}
        if not issue.get("pull_request"):
            return "ignored: not a pull request"
        if sender.get("type") == "Bot":
            return "ignored: comment by a bot"
        try:
            slug = await gh.slug()
        except GitHubAppError as e:
            raise HTTPException(502, str(e)) from e
        cmd = parse_command(comment.get("body") or "", slug)
        if cmd is None:
            return "ignored: the App is not tagged"
        login = str(sender.get("login") or "")
        repo = str(p["repository"]["full_name"])
        number, inst = int(issue["number"]), int(p["installation"]["id"])
        if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
            log.info("GitHub App: %s tagged the App on %s#%s but is not a collaborator",
                     login, repo, number)
            return "ignored: not a collaborator"
        if not auth.allowlist.allows(login):
            store.audit(login, "github_denied", {"repo": repo, "pr": number})
            later(gh.comment(inst, repo, number,
                             f"@{login} You are not on the allowlist of this manyeyes server. "
                             "Ask its admin to add you."), "the allowlist answer")
            return "denied: not on the allowlist"
        s = effective_settings(settings, store)
        roster = modes.roster_for(cmd.mode, s)
        try:
            job = limits.admit(s, store, login, roster, cmd.mode, dict(
                kind="review", requested_by=login, source="github", repo=repo, pr=number,
                roster=roster, prompt=cmd.focus or None,
                options_json=json.dumps({"no_synth": False, "mode": cmd.mode, "context": [],
                                         "github": github_options(p)})))
        except limits.LimitHit as e:
            later(gh.comment(inst, repo, number, f"@{login} {e.message}"), "the limit answer")
            return "denied: limit"
        worker.notify()
        later(gh.react(inst, repo, int(comment["id"])), "the reaction")
        return f"queued {job['id']}"

    @router.post("/github/webhook", include_in_schema=False)
    async def webhook(request: Request) -> JSONResponse:
        if not gh.configured:
            raise HTTPException(404, "the GitHub App is not configured")
        body = await request.body()
        if not verify_signature(gh.webhook_secret, body,
                                request.headers.get("x-hub-signature-256")):
            raise HTTPException(401, "bad signature")
        event = request.headers.get("x-github-event", "")
        delivery = request.headers.get("x-github-delivery")
        if event == "ping":
            return JSONResponse({"result": "pong"})
        if gh.seen(delivery):
            return JSONResponse({"result": "ignored: repeated delivery"})
        try:
            payload = json.loads(body)
            result = await handle(event, payload)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, f"unexpected payload: {type(e).__name__}") from e
        gh.remember(delivery)
        return JSONResponse({"result": result})

    return router
