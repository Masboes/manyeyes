from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from manyeyes.sources import SourceError, local_review


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / ".gitignore").write_text(".env\n")
    (r / "a.py").write_text("x = 1\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "base")
    git(r, "checkout", "-qb", "feature")
    (r / "a.py").write_text("x = 2\n")
    git(r, "commit", "-qam", "change")
    return r


def test_clean_copy_hides_ignored_files(repo, tmp_path):
    (repo / ".env").write_text("SECRET=1\n")             # ignored
    (repo / "new.py").write_text("y = 1\n")              # untracked, not ignored
    (repo / "a.py").write_text("x = 3\n")                # uncommitted change
    src = local_review(repo, tmp_path / "work", base="main")
    w = src.workdir
    assert not (w / ".env").exists()
    assert (w / "new.py").read_text() == "y = 1\n"
    assert (w / "a.py").read_text() == "x = 3\n"
    assert src.task.dirty and src.task.base_ref == "main"
    # The reviewers' git sees the committed change by SHA.
    out = subprocess.run(["git", "-C", str(w), "diff", "--stat",
                          f"{src.task.base_sha}...{src.task.head_sha}"],
                         capture_output=True, text=True, check=True).stdout
    assert "a.py" in out


def test_nothing_to_review(repo, tmp_path):
    git(repo, "checkout", "-q", "main")
    with pytest.raises(SourceError, match="nothing to review"):
        local_review(repo, tmp_path / "work", base="main")


@pytest.mark.parametrize("bad", ["-x", "a b", "a..b~1"])
def test_bad_base_ref(repo, tmp_path, bad):
    with pytest.raises(SourceError):
        local_review(repo, tmp_path / "work", base=bad)


async def test_clone_review_partial_clone_has_the_change(repo, tmp_path, monkeypatch):
    # A bare "remote" served over the file protocol stands in for GitHub.
    from manyeyes import sources
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
    git(remote, "config", "uploadpack.allowFilter", "true")
    monkeypatch.setattr(sources, "REPO_RE", __import__("re").compile(".*"))
    real_run = sources._run

    async def fake_run(argv, env, timeout, ok_codes=(0,)):
        argv = [str(remote) if a == "https://github.com/x/y.git" else a for a in argv]
        return await real_run(argv, env, timeout, ok_codes)
    monkeypatch.setattr(sources, "_run", fake_run)
    task = await sources.clone_review("x/y", tmp_path / "clone", token="", timeout=60,
                                      head="feature", base="main")
    out = subprocess.run(["git", "-C", str(tmp_path / "clone"),
                          "diff", f"{task.base_sha}...{task.head_sha}"],
                         capture_output=True, text=True, check=True,
                         env={"GIT_NO_LAZY_FETCH": "1", "PATH": "/usr/bin:/bin"}).stdout
    assert "-x = 1" in out and "+x = 2" in out


def test_repo_name_from_github_remote(repo, tmp_path):
    from manyeyes.sources import repo_name
    git(repo, "remote", "add", "origin", "git@github.com:acme/api.git")
    assert repo_name(repo) == "acme/api"
    # A clone of that clone still finds the GitHub name (one local hop).
    copy = tmp_path / "copy"
    subprocess.run(["git", "clone", "-q", str(repo), str(copy)], check=True)
    assert repo_name(copy) == "acme/api"
    git(repo, "remote", "set-url", "origin", "/nowhere")
    assert repo_name(repo) == "repo"


async def test_context_repos_are_cached_and_snapshotted(repo, tmp_path, monkeypatch):
    from manyeyes import sources
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
    real_run = sources._run
    calls = []

    async def fake_run(argv, env, timeout, ok_codes=(0,)):
        calls.append(argv)
        argv = [str(remote) if a == "https://github.com/acme/lib.git" else a for a in argv]
        return await real_run(argv, env, timeout, ok_codes)
    monkeypatch.setattr(sources, "_run", fake_run)
    cache, dest = tmp_path / "cache", tmp_path / "job-ctx"
    ctx = await sources.prepare_context(["acme/lib@main"], cache, dest, token="", timeout=60)
    (name, ref, sha, path), = ctx
    assert (name, ref) == ("acme/lib", "main") and (Path(path) / "a.py").read_text() == "x = 1\n"
    assert sources.context_cache_dir(cache, "acme/lib") == cache / "repos" / "acme" / "lib"
    # The second job reuses the cache: a fetch, no new clone of the remote.
    calls.clear()
    await sources.prepare_context(["acme/lib@main"], cache, tmp_path / "job2", token="", timeout=60)
    assert not any("init" in c for c in calls)
    # The snapshot shares the cache's objects and works offline.
    log = subprocess.run(["git", "-C", path, "log", "--oneline"], capture_output=True, text=True,
                         check=True, env={"PATH": "/usr/bin:/bin", "GIT_NO_LAZY_FETCH": "1"})
    assert "base" in log.stdout


@pytest.mark.parametrize("bad", [["nope"], ["a/b@-x"], [f"o/r{i}" for i in range(6)]])
def test_context_validation(bad):
    from manyeyes.sources import parse_context
    with pytest.raises(SourceError):
        parse_context(bad)


def test_context_in_prompt():
    from manyeyes.engine.task import ReviewTask
    t = ReviewTask(label="x", base_ref="main", base_sha="a" * 40, head_ref="f", head_sha="b" * 40,
                   context=(("acme/lib", "main", "c" * 40, "/data/work/j-context/acme__lib"),))
    p = t.prompt()
    assert "acme/lib at main" in p and "/data/work/j-context/acme__lib" in p and "READ-ONLY" in p


def test_empty_context_is_ignored_not_the_cache_root():
    from manyeyes.sources import parse_context
    assert parse_context(["", "  "]) == []


def test_eviction_skips_busy_and_recent(tmp_path):
    import fcntl
    import os
    import time as t

    from manyeyes.sources import context_cache_dir, context_lock, evict_context_cache
    for repo in ("a/old", "a/busy", "a/new"):
        context_cache_dir(tmp_path, repo).mkdir(parents=True)
        lk = context_lock(tmp_path, repo)
        lk.parent.mkdir(parents=True, exist_ok=True)
        lk.touch()
        if repo != "a/new":
            os.utime(lk, (t.time() - 30 * 86400,) * 2)
    held = open(context_lock(tmp_path, "a/busy"))
    fcntl.flock(held, fcntl.LOCK_EX)
    assert evict_context_cache(tmp_path, 14 * 86400) == ["a/old"]
    held.close()


def _bare_remote(repo: Path, tmp_path: Path, monkeypatch, url: str) -> Path:
    """A bare "remote" served over the file protocol stands in for GitHub."""
    from manyeyes import sources
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
    git(remote, "config", "uploadpack.allowFilter", "true")
    real_run = sources._run

    async def fake_run(argv, env, timeout, ok_codes=(0,)):
        argv = [str(remote) if a == url else a for a in argv]
        return await real_run(argv, env, timeout, ok_codes)
    monkeypatch.setattr(sources, "_run", fake_run)
    return remote


def _sha(repo: Path, ref: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.mark.parametrize("head_len,base_len", [(40, 9), (9, 40), (7, 7)])
async def test_clone_review_accepts_commit_shas(repo, tmp_path, monkeypatch, head_len, base_len):
    from manyeyes import sources
    _bare_remote(repo, tmp_path, monkeypatch, "https://github.com/x/y.git")
    head, base = _sha(repo, "feature"), _sha(repo, "main")
    task = await sources.clone_review("x/y", tmp_path / "clone", token="", timeout=60,
                                      head=head[:head_len], base=base[:base_len])
    assert (task.head_sha, task.base_sha) == (head, base)
    assert task.base_ref == base[:base_len]          # no "origin/" for a commit


async def test_clone_review_prefers_a_branch_that_looks_like_hex(repo, tmp_path, monkeypatch):
    from manyeyes import sources
    git(repo, "branch", "deadbeef", "feature")
    _bare_remote(repo, tmp_path, monkeypatch, "https://github.com/x/y.git")
    task = await sources.clone_review("x/y", tmp_path / "clone", token="", timeout=60,
                                      head="deadbeef", base="main")
    assert task.head_sha == _sha(repo, "feature") and task.base_ref == "origin/main"


async def test_clone_review_unknown_short_sha(repo, tmp_path, monkeypatch):
    from manyeyes import sources
    _bare_remote(repo, tmp_path, monkeypatch, "https://github.com/x/y.git")
    with pytest.raises(SourceError, match="short SHA"):
        await sources.clone_review("x/y", tmp_path / "clone", token="", timeout=60,
                                   head="abcdef0", base="main")


async def test_context_repo_at_a_commit(repo, tmp_path, monkeypatch):
    from manyeyes import sources
    _bare_remote(repo, tmp_path, monkeypatch, "https://github.com/acme/lib.git")
    sha = _sha(repo, "main")
    for ref in (sha, sha[:8]):
        ((_, got_ref, got_sha, path),) = await sources.prepare_context(
            [f"acme/lib@{ref}"], tmp_path / "cache", tmp_path / f"job-{len(ref)}",
            token="", timeout=60)
        assert (got_ref, got_sha) == (ref, sha)
        assert (Path(path) / "a.py").read_text() == "x = 1\n"


async def test_git_network_error_is_retried_once(monkeypatch):
    from manyeyes import sources
    monkeypatch.setattr(sources, "RETRY_DELAYS_S", (0.0, 0.0))
    calls = []

    async def flaky(argv, env, timeout, ok_codes=(0,)):
        calls.append(argv)
        if len(calls) == 1:
            raise SourceError("git fetch failed: The requested URL returned error: 503")
        return "ok"
    monkeypatch.setattr(sources, "_run", flaky)
    assert await sources._run_net(["git", "fetch"], {}, 5) == "ok" and len(calls) == 2

    calls.clear()

    async def denied(argv, env, timeout, ok_codes=(0,)):
        calls.append(argv)
        raise SourceError("git fetch failed: The requested URL returned error: 403")
    monkeypatch.setattr(sources, "_run", denied)
    with pytest.raises(SourceError, match="403"):
        await sources._run_net(["git", "fetch"], {}, 5)
    assert len(calls) == 1


def _github(monkeypatch, responses):
    """resolve_pr talks to a fake GitHub that answers `responses` in order."""
    import httpx

    from manyeyes import sources
    monkeypatch.setattr(sources, "RETRY_DELAYS_S", (0.0, 0.0))
    seen = []

    def handler(request):
        seen.append(request.url.path)
        r = responses[len(seen) - 1]
        if isinstance(r, Exception):
            raise r
        return httpx.Response(r, json={"head": {"ref": "f", "repo": {"full_name": "o/r"}},
                                       "base": {"ref": "main"}})
    real = httpx.AsyncClient
    monkeypatch.setattr(sources.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


async def test_resolve_pr_retries_5xx_and_connection_errors(monkeypatch):
    import httpx

    from manyeyes import sources
    seen = _github(monkeypatch, [503, httpx.ConnectError("boom"), 200])
    assert await sources.resolve_pr("o/r", 7, "") == ("f", "main")
    assert len(seen) == 3


async def test_resolve_pr_gives_up_after_two_retries(monkeypatch):
    from manyeyes import sources
    seen = _github(monkeypatch, [502, 503, 504])
    with pytest.raises(SourceError, match="504"):
        await sources.resolve_pr("o/r", 7, "")
    assert len(seen) == 3


async def test_resolve_pr_does_not_retry_4xx(monkeypatch):
    from manyeyes import sources
    seen = _github(monkeypatch, [404])
    with pytest.raises(SourceError, match="404"):
        await sources.resolve_pr("o/r", 7, "")
    assert len(seen) == 1
