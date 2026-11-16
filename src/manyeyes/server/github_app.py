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
# GitHub caps webhook payloads at 25 MB; a comment event is far smaller.
MAX_BODY = 5 * 1024 * 1024


class GitHubAppError(RuntimeError):
    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent   # trying again cannot help


@dataclass(frozen=True)
class Command:
    mode: str
    focus: str


_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")


def _fence_opens(line: str) -> str:
    """The fence a line opens ("" when it opens none). A backtick fence's info
    string holds no backtick, so ```x``` on one line is inline code."""
    m = _FENCE.match(line)
    if not m or (m.group(1)[0] == "`" and "`" in m.group(2)):
        return ""
    return m.group(1)


def _fence_closes(line: str, fence: str) -> bool:
    m = _FENCE.match(line)
    return bool(m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)
                and not m.group(2).strip())


def parse_command(body: str, slug: str) -> Command | None:
    """The command in a comment that starts with `@slug`, or None.

    The tag must open the comment: its first line that is not blank and not a
    quote (`> ...`). So a reply that quotes an older request, or a comment that
    shows how to use the App in code, does not start a review. The text after
    the tag is the instruction: an optional word "review", an optional mode
    (quick, full, deep), then free text that goes to the reviewers as the focus.
    """
    if not slug:
        return None
    lines = [ln for ln in (body or "").splitlines()
             if ln.strip() and not ln.lstrip().startswith(">")]
    if not lines:
        return None
    text = "\n".join(lines)
    m = re.match(rf" {{0,3}}@{re.escape(slug)}(?![\w-])", text, re.IGNORECASE)
    if m is None:
        return None
    rest = text[m.end():].lstrip(" \t,:;.-")
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
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest().encode()
    return hmac.compare_digest(want, header[len("sha256="):].encode("utf-8", "replace"))


def _defuse_mentions(text: str) -> str:
    """Stop every `@name` in the text from notifying a GitHub user.

    A zero-width space after the @ breaks the link. It is applied everywhere,
    code included: the text comes from reviewers, and a Markdown parser that
    is close but not exact would leave mentions active.
    """
    return re.sub(r"@(?=[A-Za-z0-9])", "@\u200b", text)


def _open_fence(text: str) -> str:
    """The fence that is still open at the end of the text ("" when none)."""
    fence = ""
    for ln in text.splitlines():
        if fence:
            if _fence_closes(ln, fence):
                fence = ""
        else:
            fence = _fence_opens(ln)
    return fence


def _cut(text: str, room: int) -> str:
    """At most `room` characters of the text, with an open code fence closed, so
    the text after it is not code."""
    cut = text[:room].rstrip() if len(text) > room else text
    fence = _open_fence(cut)
    if fence:
        cut = text[:max(0, room - len(fence) - 1)].rstrip()
        fence = _open_fence(cut)
    return f"{cut}\n{fence}" if fence else cut


def _failure(error: str) -> str:
    """A fixed, public summary of a failure. The raw reason (paths, tool output)
    stays on the job page."""
    if error.startswith("could not prepare the code"):
        return "manyeyes could not fetch the code of this pull request."
    if error.startswith("could not prepare the related repos"):
        return "manyeyes could not fetch a related repository."
    if error.startswith("job timeout"):
        return "the review ran past its time limit."
    if error.startswith(("server restarted", "the server stopped")):
        return "the server restarted while the review ran. Tag the App again to retry."
    m = re.match(r"all (\d+) reviewers failed \(\d+ ([a-z ]+)\)", error)
    if m:   # the counts and the outcome name come from manyeyes, not from tool output
        return f"all {m.group(1)} reviewers failed (mostly: {m.group(2)})."
    return "an internal error."


def result_comment(job: dict[str, Any], job_url: str | None) -> str:
    """The comment the App posts when a job ends."""
    who = f"@{job['requested_by']}" if job.get("requested_by") else ""
    link = f"[Full result with every review]({job_url})" if job_url else ""
    status = job.get("status")
    synthesis = job.get("synthesis_md") or ""
    if status == "done" and synthesis:
        sha = (job.get("head_sha") or "")[:7]
        head = f"{who} manyeyes reviewed commit {sha}.".strip() if sha else \
            f"{who} manyeyes finished the review.".strip()
        if job.get("partial"):
            head += " Some reviewers did not finish; the result is partial."
        # Defuse first, then cut: the cut sees the final length.
        body = _defuse_mentions(synthesis)
        room = MAX_COMMENT - len(head) - len(link) - 300
        cut = _cut(body, room)
        if len(body) > room:
            cut += "\n\n… (cut: the result is too long for a comment)"
        body = cut
        return "\n\n".join(p for p in (head, body, "---\n" + link if link else "") if p)
    if status == "done":
        msg = f"{who} manyeyes finished the review, but the synthesis is missing."
    elif status == "cancelled":
        msg = f"{who} The manyeyes review was cancelled."
    else:
        msg = f"{who} The manyeyes review failed: {_failure(job.get('error') or '')}"
    return "\n\n".join(p for p in (msg.strip(), link) if p)


class GitHubApp:
    """GitHub App credentials and the few API calls the trigger needs."""

    def __init__(self, settings: Settings) -> None:
        self.app_id = str(settings["github_app.id"] or "")
        self.private_key = settings["github_app.private_key"] or ""
        self.webhook_secret = settings["github_app.webhook_secret"] or ""
        self._slug: str | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.transport: httpx.AsyncBaseTransport | None = None   # tests replace GitHub

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.private_key and self.webhook_secret)

    def claim(self, delivery_id: str | None) -> bool:
        """False when this webhook delivery is handled, or was handled, already.

        GitHub redelivers only when someone asks for it, so memory is enough.
        The claim comes before any await: two copies at once cannot both pass.
        """
        if not delivery_id:
            return True
        if delivery_id in self._seen:
            return False
        self._seen[delivery_id] = None
        while len(self._seen) > 2000:
            self._seen.popitem(last=False)
        return True

    def release(self, delivery_id: str | None) -> None:
        """Forget a claim whose handling failed, so a redelivery can try again."""
        if delivery_id:
            self._seen.pop(delivery_id, None)

    def _jwt(self) -> str:
        import jwt
        now = int(time.time())
        # GitHub allows at most 10 minutes; start 60 s back for clock drift.
        try:
            return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id},
                              self.private_key, algorithm="RS256")
        except (ValueError, TypeError, jwt.PyJWTError) as e:
            raise GitHubAppError(f"cannot sign with github_app.private_key: "
                                 f"{type(e).__name__}") from e

    async def _call(self, method: str, path: str, token: str,
                    body: dict[str, Any] | None = None, once: bool = False) -> httpx.Response:
        """One API call. Retries server and connection errors, except with `once`:
        a comment that GitHub took but did not confirm would then appear twice.
        A connection that never opened is always safe to retry."""
        headers = {"Accept": "application/vnd.github+json",
                   "Authorization": f"Bearer {token}",
                   "X-GitHub-Api-Version": "2022-11-28"}
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            for delay in (1.0, 3.0, None):
                try:
                    r = await client.request(method, API + path, headers=headers, json=body)
                except httpx.TransportError as e:
                    if delay is None or (once and not isinstance(e, httpx.ConnectError)):
                        raise GitHubAppError(f"cannot reach GitHub: {type(e).__name__}") from e
                else:
                    if r.status_code < 500 or delay is None or once:
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

    async def installation_token(self, installation_id: int, repo: str,
                                 read_only: bool = False) -> str:
        """A token for one repository, valid for one hour. `read_only`: only what a
        clone needs (contents and pull requests, read)."""
        name = repo.split("/", 1)[1]
        body: dict[str, Any] = {"repositories": [name]}
        if read_only:
            body["permissions"] = {"contents": "read", "pull_requests": "read"}
        r = await self._call("POST", f"/app/installations/{int(installation_id)}/access_tokens",
                             self._jwt(), body)
        if r.status_code != 201:
            # 404: the App is no longer installed there.
            raise GitHubAppError(f"GitHub returned {r.status_code} for an installation "
                                 f"token for {repo}", permanent=r.status_code == 404)
        try:
            return str(r.json()["token"])
        except (ValueError, KeyError, TypeError) as e:
            raise GitHubAppError("GitHub sent an unreadable installation token") from e

    async def comment(self, installation_id: int, repo: str, number: int, body: str) -> None:
        token = await self.installation_token(installation_id, repo)
        r = await self._call("POST", f"/repos/{repo}/issues/{int(number)}/comments", token,
                             {"body": body}, once=True)
        if r.status_code != 201:
            raise GitHubAppError(f"GitHub returned {r.status_code} for a comment on "
                                 f"{repo}#{number}")

    async def answer(self, installation_id: int, repo: str, number: int, body: str,
                     marker: str, since: str) -> bool:
        """Post `body` on the pull request, unless a comment that holds `marker`
        (created after `since`) is there already. True when it posted.

        The check makes a second try safe after an interrupted send that GitHub
        took. A 404, 410 or 422 is permanent: the pull request or the App is gone.
        """
        token = await self.installation_token(installation_id, repo)
        path = f"/repos/{repo}/issues/{int(number)}/comments"
        if await self._has_marker(token, path, marker, since):
            return False
        r = await self._call("POST", path, token, {"body": f"{body}\n\n{marker}"}, once=True)
        if r.status_code != 201:
            # 404 and 410: the pull request or the App is gone. A 422 can be a
            # temporary spam block, so it is not permanent.
            raise GitHubAppError(f"GitHub returned {r.status_code} for a comment on "
                                 f"{repo}#{number}", permanent=r.status_code in (404, 410))
        return True

    async def _has_marker(self, token: str, path: str, marker: str, since: str) -> bool:
        """Whether a comment created after `since` holds `marker`. Any doubt raises:
        posting without knowing could post the answer twice."""
        for page in range(1, 51):
            r = await self._call("GET", f"{path}?per_page=100&page={page}&since={since}", token)
            if r.status_code in (404, 410):
                raise GitHubAppError(f"GitHub returned {r.status_code} for the comments",
                                     permanent=True)
            if r.status_code != 200:
                raise GitHubAppError(f"GitHub returned {r.status_code} for the comments")
            try:
                rows = r.json()
                if any(marker in (c.get("body") or "") for c in rows):
                    return True
            except (ValueError, AttributeError, TypeError) as e:
                raise GitHubAppError("GitHub sent an unreadable comment list") from e
            if len(rows) < 100:
                return False
        raise GitHubAppError("too many comments to check for an earlier answer")

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
    if gh.configured:
        try:
            gh._jwt()
        except GitHubAppError as e:
            log.error("GitHub App: %s; tagged comments will fail", e)
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
        # Strangers stop here, before any parsing of their text.
        if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
            return "ignored: not a collaborator"
        cmd = parse_command(comment.get("body") or "", slug)
        if cmd is None:
            return "ignored: the App is not tagged"
        login = str(sender.get("login") or "")
        repo = str(p["repository"]["full_name"])
        number, inst = int(issue["number"]), int(p["installation"]["id"])
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
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                raise HTTPException(413, "body too large")
            chunks.append(chunk)
        body = b"".join(chunks)
        if not verify_signature(gh.webhook_secret, body,
                                request.headers.get("x-hub-signature-256")):
            raise HTTPException(401, "bad signature")
        event = request.headers.get("x-github-event", "")
        delivery = request.headers.get("x-github-delivery")
        if event == "ping":
            return JSONResponse({"result": "pong"})
        try:
            payload = json.loads(body)
        except ValueError as e:
            raise HTTPException(400, "the body is not JSON") from e
        if not isinstance(payload, dict):
            raise HTTPException(400, "the body is not a JSON object")
        if not gh.claim(delivery):
            return JSONResponse({"result": "ignored: repeated delivery"})
        try:
            result = await handle(event, payload)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            gh.release(delivery)
            raise HTTPException(400, f"unexpected payload: {type(e).__name__}") from e
        except BaseException:
            gh.release(delivery)
            raise
        return JSONResponse({"result": result})

    return router
