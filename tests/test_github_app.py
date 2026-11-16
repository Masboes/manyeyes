"""Reviews started from a pull request comment that tags the GitHub App."""
from __future__ import annotations

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
    ("please @manyeyes-bot review quick: look at auth", ("quick", "look at auth")),
    ("@manyeyes-bot deep\nmind the migrations", ("deep", "mind the migrations")),
    ("@manyeyes-bot check the cache", ("full", "check the cache")),
    ("> @manyeyes-bot review\nthanks!", None),
    ("@manyeyes-botty review", None),
    ("mail me@manyeyes-bot", None),
    ("ping @other", None),
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


def test_result_comment():
    job = {"id": "j1", "status": "done", "requested_by": "alice", "head_sha": "abcdef123",
           "partial": 1, "synthesis_md": "Ask @bob, see `@types/node` and\n```\n@decorator\n```"}
    out = result_comment(job, "https://x/#/jobs/j1")
    assert out.startswith("@alice manyeyes reviewed commit abcdef1. Some reviewers")
    assert "@​bob" in out and "`@types/node`" in out and "\n@decorator\n" in out
    assert out.endswith("---\n[Full result with every review](https://x/#/jobs/j1)")
    long = result_comment({**job, "synthesis_md": "x" * 100_000}, None)
    assert len(long) < 65_536 and long.endswith("too long for a comment)")
    failed = result_comment({"status": "failed", "requested_by": "alice",
                             "error": "could not prepare the code: boom\ntrace"}, None)
    assert failed == "@alice The manyeyes review failed: could not prepare the code: boom"
    assert "cancelled" in result_comment({"status": "cancelled"}, None)


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

    async def slug(self):
        return "manyeyes-bot"

    async def installation_token(self, installation_id, repo):
        assert installation_id == 7
        return "inst-token"

    async def comment(self, installation_id, repo, number, body):
        self.comments.append((repo, number, body))

    async def react(self, installation_id, repo, comment_id, content="eyes"):
        self.reactions.append(comment_id)


@pytest.fixture
def fake(monkeypatch):
    f = FakeGitHub()
    for name in ("slug", "installation_token", "comment", "react"):
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
        assert body.startswith("@alice The manyeyes review failed: could not prepare the code")
        assert f"https://example.org/me/#/jobs/{job_id}" in body
        # GitHub redelivers: no second job.
        assert send(c, payload()).json()["result"] == "ignored: repeated delivery"


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
        assert any("1 per day" in b or "limit" in b.lower() for _, _, b in fake.comments)


def test_bad_signature_ping_and_off(app_settings, settings, fake):
    with TestClient(create_app(app_settings)) as c:
        assert send(c, payload(), secret="wrong").status_code == 401
        assert send(c, {}, event="ping").json() == {"result": "pong"}
        raw = b"not json"
        sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        assert c.post("/github/webhook", content=raw, headers={
            "X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": sig}).status_code == 400
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
