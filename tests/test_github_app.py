"""Reviews started from a pull request comment that tags the GitHub App."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from manyeyes.server import github_app
from manyeyes.server.app import create_app
from manyeyes.server.github_app import GitHubApp, parse_command, result_comment
from manyeyes.sources import SourceError

SECRET = "hook-secret"


def rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.mark.parametrize("body,want", [
    ("@manyeyes-bot", ("full", "")),
    ("@ManyEyes-Bot review", ("full", "")),
    ("@manyeyes-bot review quick: look at auth", ("quick", "look at auth")),
    ("\n  @manyeyes-bot deep\nmind the migrations", ("deep", "mind the migrations")),
    ("@manyeyes-bot check the cache", ("full", "check the cache")),
    ("@manyeyes-bot, review quick", ("quick", "")),
    ("@manyeyes-bot quick check:\n\n> keep this\nand this", ("quick", "check:\n\n> keep this\nand this")),
    ("@manyeyes-bot/team please look", None),
    ("> earlier text\n@manyeyes-bot quick", ("quick", "")),
    ("> @manyeyes-bot review\nthanks!", None),
    ("please @manyeyes-bot review", None),
    ("@manyeyes-botty review", None),
    ("mail me@manyeyes-bot", None),
    ("ping @other", None),
    ("Use `@manyeyes-bot review` to start one.", None),
    ("``@manyeyes-bot review``", None),
    ("    @manyeyes-bot review", None),
    ("```\n@manyeyes-bot review deep\n```", None),
    ("- example:\n  ```\n  @manyeyes-bot review\n  ```", None),
])

def test_parse_command(body, want):
    cmd = parse_command(body, "manyeyes-bot")
    assert (None if cmd is None else (cmd.mode, cmd.focus)) == want


def test_signature():
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert github_app.verify_signature(SECRET, body, good)
    assert not github_app.verify_signature(SECRET, body + b" ", good)
    assert not github_app.verify_signature(SECRET, body, None)
    assert not github_app.verify_signature("", body, good)
    assert not github_app.verify_signature(SECRET, body, "sha256=é")


def test_result_comment():
    job = {"id": "j1", "status": "done", "requested_by": "alice", "head_sha": "abcdef123",
           "partial": 1, "synthesis_md": "Ask @bob, see `@types/node` and\n```\n@decorator\n```"}
    out = result_comment(job, "https://x/#/jobs/j1")
    assert out.startswith("@alice manyeyes reviewed commit abcdef1. Some reviewers")
    assert "@\u200bbob" in out and "`@\u200btypes/node`" in out and "@\u200bdecorator" in out
    assert out.endswith("---\n[Full result with every review](https://x/#/jobs/j1)")
    long = result_comment({**job, "synthesis_md": "x" * 100_000}, None)
    assert len(long) < 65_536 and long.endswith("too long for a comment)")
    fenced = result_comment({**job, "synthesis_md": "````md\n" + "y\n" * 40_000}, "u")
    assert "\n````\n\n… (cut" in fenced and fenced.endswith("(u)")
    mentions = result_comment({**job, "synthesis_md": "@a " * 30_000}, "u")
    assert len(mentions) < 65_536
    failed = result_comment({"status": "failed", "requested_by": "alice",
                             "error": "could not prepare the code: git: /data/work/x"}, None)
    assert failed == ("@alice The manyeyes review failed: manyeyes could not fetch the code "
                      "of this pull request.")
    every = result_comment({"status": "failed",
                            "error": "all 3 reviewers failed (2 rate limited): /x/y"}, None)
    assert every.endswith("failed: all 3 reviewers failed (mostly: rate limited).")
    crash = result_comment({"status": "failed", "error": "OSError: /data/work/x"}, None)
    assert "/data" not in crash and "internal error" in crash
    assert "cancelled" in result_comment({"status": "cancelled"}, None)
    short = result_comment({**job, "synthesis_md": "```\nopen"}, "u")
    assert "open\n```\n\n---" in short
    no_synth = result_comment({**job, "synthesis_md": None, "result_md": "every review"}, None)
    assert "synthesis is missing" in no_synth and "every review" not in no_synth


def test_jwt_is_signed_with_the_app_key(settings):
    import jwt
    from cryptography.hazmat.primitives import serialization
    pem = rsa_pem()
    app = GitHubApp(settings.with_layer("t", {"github_app.id": "42",
                                              "github_app.private_key": pem,
                                              "github_app.webhook_secret": SECRET}))
    pub = serialization.load_pem_private_key(pem.encode(), None).public_key()
    claims = jwt.decode(app._jwt(), pub, algorithms=["RS256"])
    assert claims["iss"] == "42" and claims["exp"] - claims["iat"] <= 600


# -- the webhook, with GitHub faked ------------------------------------------------

class FakeGitHub:
    def __init__(self):
        self.comments: list[tuple[str, int, str]] = []
        self.reactions: list[int] = []

    slow = 0.0

    async def slug(self):
        await asyncio.sleep(self.slow)
        return "manyeyes-bot"

    async def installation_token(self, installation_id, repo, read_only=False):
        assert installation_id == 7 and read_only
        return "inst-token"

    async def comment(self, installation_id, repo, number, body):
        self.comments.append((repo, number, body))

    async def answer(self, installation_id, repo, number, body, marker, since):
        self.comments.append((repo, number, body))
        return True

    async def react(self, installation_id, repo, comment_id, content="eyes"):
        self.reactions.append(comment_id)


@pytest.fixture
def fake(monkeypatch):
    f = FakeGitHub()
    for name in ("slug", "installation_token", "comment", "answer", "react"):
        monkeypatch.setattr(GitHubApp, name, lambda self, *a, _n=name, **k: getattr(f, _n)(*a, **k))

    async def no_clone(*a, **k):
        assert k["token"] == "inst-token"
        raise SourceError("no network in tests")
    monkeypatch.setattr("manyeyes.server.queue.clone_review", no_clone)
    return f


@pytest.fixture
def app_settings(settings):
    settings.allowlist_file.write_text("alice\n")
    return settings.with_layer("test", {
        "auth.bootstrap_token": "boot", "auth.bootstrap_login": "alice",
        "server.public_urls": "https://example.org/me",
        "github_app.id": "42", "github_app.private_key": "unused",
        "github_app.webhook_secret": SECRET})


def payload(body="@manyeyes-bot review quick: auth", login="alice", assoc="MEMBER",
            kind="User", pr=True, comment_id=99):
    issue = {"number": 5, **({"pull_request": {"url": "x"}} if pr else {})}
    return {"action": "created", "issue": issue, "repository": {"full_name": "acme/app"},
            "installation": {"id": 7}, "sender": {"login": login, "type": kind},
            "comment": {"id": comment_id, "body": body, "author_association": assoc}}


def send(client, data, event="issue_comment", delivery="d1", secret=SECRET):
    raw = json.dumps(data).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/github/webhook", content=raw, headers={
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": sig,
        "Content-Type": "application/json"})


def wait(cond, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_tag_queues_a_review_and_answers_on_the_pull_request(app_settings, fake):
    with TestClient(create_app(app_settings)) as c:
        r = send(c, payload())
        assert r.status_code == 200 and r.json()["result"].startswith("queued ")
        job_id = r.json()["result"].split()[1]
        wait(lambda: fake.comments)
        job = c.app.state.store.get_job(job_id)
        assert (job["source"], job["requested_by"], job["repo"], job["pr"], job["prompt"]) == \
            ("github", "alice", "acme/app", 5, "auth")
        assert json.loads(job["options_json"])["mode"] == "quick"
        assert fake.reactions == [99]
        repo, number, body = fake.comments[0]
        assert (repo, number) == ("acme/app", 5)
        assert body.startswith("@alice The manyeyes review failed: manyeyes could not fetch")
        assert f"https://example.org/me/#/jobs/{job_id}" in body
        # GitHub redelivers: no second job.
        assert send(c, payload()).json()["result"] == "ignored: repeated delivery"
        wait(lambda: json.loads(c.app.state.store.get_job(job_id)["options_json"])
             ["github"].get("answered") is True)
    # The answer was recorded as sent: a restart does not post it again.
    with TestClient(create_app(app_settings)):
        time.sleep(0.3)
    assert len(fake.comments) == 1


def test_two_copies_of_one_delivery_at_once_make_one_job(app_settings, fake):
    from concurrent.futures import ThreadPoolExecutor
    fake.slow = 0.3
    with TestClient(create_app(app_settings)) as c:
        with ThreadPoolExecutor(2) as pool:
            results = sorted(r.json()["result"].split()[0] for r in
                             pool.map(lambda _: send(c, payload()), range(2)))
        assert results == ["ignored:", "queued"]


def test_cancel_of_a_queued_job_answers(app_settings, fake):
    s = app_settings.with_layer("q", {"server.max_concurrent_jobs": 1})
    with TestClient(create_app(s)) as c:
        worker = c.app.state.worker
        worker.tasks["blocker"] = None   # the queue is full: the job stays queued
        job_id = send(c, payload()).json()["result"].split()[1]
        assert c.post(f"/api/jobs/{job_id}/cancel",
                      headers={"Authorization": "Bearer boot"}).status_code == 200
        wait(lambda: fake.comments)
        assert "was cancelled" in fake.comments[0][2]
        worker.tasks.pop("blocker")


@pytest.mark.parametrize("data,result", [
    (payload(assoc="NONE"), "ignored: not a collaborator"),
    (payload(kind="Bot"), "ignored: comment by a bot"),
    (payload(pr=False), "ignored: not a pull request"),
    (payload(body="> @manyeyes-bot review\nlgtm"), "ignored: the App is not tagged"),
    ({**payload(), "action": "edited"}, "ignored: not a new comment"),
])
def test_comments_that_start_nothing(app_settings, fake, data, result):
    with TestClient(create_app(app_settings)) as c:
        assert send(c, data).json()["result"] == result
        assert c.app.state.store.list_jobs() == [] and fake.comments == []


def test_not_on_the_allowlist_gets_an_answer(app_settings, fake):
    with TestClient(create_app(app_settings)) as c:
        assert send(c, payload(login="mallory")).json()["result"] == \
            "denied: not on the allowlist"
        wait(lambda: fake.comments)
        assert "not on the allowlist" in fake.comments[0][2]
        assert c.app.state.store.list_jobs() == []


def test_limits_apply_to_the_commenter(app_settings, fake):
    s = app_settings.with_layer("lim", {"limits.user_jobs_per_day": 1})
    with TestClient(create_app(s)) as c:
        assert send(c, payload(), delivery="a").json()["result"].startswith("queued")
        assert send(c, payload(), delivery="b").json()["result"] == "denied: limit"
        wait(lambda: len(fake.comments) == 2)
        assert any(b.startswith("@alice limit reached: 1 reviews per day.")
                   for _, _, b in fake.comments)


def test_bad_signature_ping_and_off(app_settings, settings, fake):
    with TestClient(create_app(app_settings)) as c:
        assert send(c, payload(), secret="wrong").status_code == 401
        assert send(c, {}, event="ping").json() == {"result": "pong"}
        raw = b"not json"
        sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        assert c.post("/github/webhook", content=raw, headers={
            "X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": sig}).status_code == 400
        assert send(c, [1, 2]).status_code == 400
        big = b"x" * (6 * 1024 * 1024)
        assert c.post("/github/webhook", content=big).status_code == 413
    off = settings.with_layer("t", {"auth.bootstrap_token": "b", "auth.bootstrap_login": "alice"})
    with TestClient(create_app(off)) as c:
        assert send(c, payload()).status_code == 404


def test_restart_tells_the_pull_request(app_settings, fake):
    from manyeyes.store.db import Store
    store = Store(app_settings.db_path)
    job = store.create_job(kind="review", source="github", requested_by="alice",
                           repo="acme/app", pr=5, roster="codex:1",
                           options_json=json.dumps({"github": {"installation": 7,
                                                               "comment": 1}}))
    store.update_job(job["id"], status="running")
    with TestClient(create_app(app_settings)):
        wait(lambda: fake.comments)
    assert "server restarted" in fake.comments[0][2]


def test_a_stop_is_not_a_cancel_and_the_answer_waits_for_the_next_start(app_settings, fake):
    s = app_settings.with_layer("slow", {"limits.reviewer_timeout_s": 60})
    with TestClient(create_app(s)) as c:
        store = c.app.state.store
        job = store.create_job(kind="spar", source="github", requested_by="alice",
                               repo="acme/app", pr=5, prompt="q", roster="codex:1@slow-30",
                               options_json=json.dumps({"github": {"installation": 7}}))
        c.app.state.worker.notify()
        wait(lambda: store.get_job(job["id"])["status"] == "running")
        time.sleep(0.5)
    j = store.get_job(job["id"])
    assert (j["status"], j["error"]) == ("failed", "the server stopped while this job ran")
    assert fake.comments == []
    with TestClient(create_app(s)):
        wait(lambda: fake.comments)
    assert "server restarted while the review ran" in fake.comments[0][2]


# -- the GitHub calls themselves, against a fake GitHub API ---------------------------

def real_app(settings, handler):
    import httpx
    app = GitHubApp(settings.with_layer("t", {"github_app.id": "42",
                                              "github_app.private_key": rsa_pem(),
                                              "github_app.webhook_secret": SECRET}))
    app.transport = httpx.MockTransport(handler)
    return app


def test_answer_posts_once_with_a_marker(settings):
    import httpx
    calls, existing = [], []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, json.loads(req.content or b"null")))
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "t"})
        if req.method == "GET":
            return httpx.Response(200, json=existing)
        existing.append({"body": json.loads(req.content)["body"], "user": {"type": "Bot"}})
        return httpx.Response(201, json={})

    app = real_app(settings, handler)
    args = (7, "acme/app", 5, "hello", "<!-- manyeyes job j1 -->", "2026-01-01T00:00:00Z")
    assert asyncio.run(app.answer(*args)) is True
    assert [e["body"] for e in existing] == ["hello\n\n<!-- manyeyes job j1 -->"]
    assert calls[0][2] == {"repositories": ["app"]}         # a write token: no narrowing
    assert asyncio.run(app.answer(*args)) is False          # the marker is there: no repost
    assert len(existing) == 1
    calls.clear()
    asyncio.run(app.installation_token(7, "acme/app", read_only=True))
    assert calls[0][2]["permissions"] == {"contents": "read", "pull_requests": "read"}


def test_answer_errors(settings):
    import httpx
    status = {"post": 422}

    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "t"})
        if req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(status["post"])

    app = real_app(settings, handler)
    args = (7, "acme/app", 5, "b", "m", "s")
    with pytest.raises(github_app.GitHubAppError) as e:
        asyncio.run(app.answer(*args))
    assert e.value.permanent is False      # a 422 can be a temporary spam block
    status["post"] = 404
    with pytest.raises(github_app.GitHubAppError) as e:
        asyncio.run(app.answer(*args))
    assert e.value.permanent
    status["post"] = 502          # not retried (GitHub may have taken it), not permanent
    with pytest.raises(github_app.GitHubAppError) as e:
        asyncio.run(app.answer(*args))
    assert not e.value.permanent
    bad = GitHubApp(settings.with_layer("t", {"github_app.id": "1",
                                              "github_app.private_key": "not a key",
                                              "github_app.webhook_secret": SECRET}))
    with pytest.raises(github_app.GitHubAppError, match="cannot sign"):
        bad._jwt()


def test_answer_lookup_fails_closed_and_follows_pages(settings):
    import httpx
    bot = {"type": "Bot"}
    pages: dict[int, list] = {1: [{"body": "x", "user": bot}] * 99 + [
        {"body": "<!--m--> by a person", "user": {"type": "User"}}],
        2: [{"body": "a <!--m-->", "user": bot}]}
    posts, lookup = [], {"status": 200}

    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "t"})
        if req.method == "GET":
            if lookup["status"] != 200:
                return httpx.Response(lookup["status"])
            return httpx.Response(200, json=pages.get(int(req.url.params["page"]), []))
        posts.append(req)
        return httpx.Response(201, json={})

    app = real_app(settings, handler)
    args = (7, "acme/app", 5, "b", "<!--m-->", "s")
    assert asyncio.run(app.answer(*args)) is False     # found on page 2
    lookup["status"] = 503
    with pytest.raises(github_app.GitHubAppError) as e:
        asyncio.run(app.answer(*args))
    assert not e.value.permanent and posts == []      # in doubt: no post



def test_result_comment_shows_the_notes():
    from manyeyes.engine.bundle import NOTE_PREFIX
    job = {"status": "done", "requested_by": "ann", "head_sha": "abc1234def",
           "synthesis_md": "All good.",
           "result_md": f"# Review\n\n**Reviews:** 2 of 2 succeeded\n\n{NOTE_PREFIX}MANYEYES.md "
                        "was not used.\n\n## Synthesis\n\nAll good.\n"}
    out = result_comment(job, None)
    assert f"{NOTE_PREFIX}MANYEYES.md was not used." in out
    assert out.index("MANYEYES.md") < out.index("All good.")
