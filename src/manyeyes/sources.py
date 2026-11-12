"""Prepare the code a review runs against.

Two sources:
  - a local git checkout (the CLI's default): resolve base and head, nothing copied;
  - a GitHub repository (server jobs, `--repo` / `--pr` locally): clone into a
    private work directory, check the merge base, hand back the path.

The GitHub token is passed to git through GIT_CONFIG_* environment entries
(an http.extraHeader scoped to github.com), so it never appears in argv and is
never written into the clone's .git/config.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from .engine.task import ReviewTask

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class SourceError(RuntimeError):
    pass


class RefNotFound(SourceError):
    """The remote has no such branch or commit. The message is safe to show."""


# Never let a repository's own config run code on the host while we read it.
SAFE_GIT = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "protocol.file.allow=always")


def _git(cwd: Path, *args: str, input_bytes: bytes | None = None) -> str:
    try:
        r = subprocess.run(["git", *SAFE_GIT, "-C", str(cwd), *args], check=True,
                           capture_output=True, input=input_bytes)
        return r.stdout.decode(errors="replace").strip()
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode(errors="replace").strip()[:300]
        raise SourceError(f"git {' '.join(args[:2])} failed: {msg}") from e


def _check_ref(ref: str) -> str:
    if not ref or ref.startswith("-") or any(c in ref for c in " ~^:?*[\\"):
        raise SourceError(f"not a valid ref: {ref!r}")
    return ref


# A branch or base may also be a commit: 7 to 40 hex characters.
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


# --- local checkout -------------------------------------------------------

@dataclass
class LocalSource:
    root: Path              # the user's working tree (read only)
    workdir: Path           # clean copy the reviewers see
    task: ReviewTask
    name: str = ""          # "owner/name" from the GitHub remote, else the folder name


GITHUB_REMOTE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def repo_name(root: Path) -> str:
    """owner/name of the GitHub remote; follows one local hop (a clone of a clone)."""
    path: Path | None = root
    for _ in range(2):
        if path is None:
            break
        try:
            url = _git(path, "remote", "get-url", "origin")
        except SourceError:
            break
        m = GITHUB_REMOTE.search(url)
        if m:
            return m.group(1)
        local = Path(url.removeprefix("file://"))
        path = local if local.is_dir() else None
    return root.name


def default_base(root: Path) -> str:
    """origin/HEAD's branch if known, else main or master."""
    try:
        ref = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        return ref.removeprefix("refs/remotes/")
    except SourceError:
        pass
    for cand in ("origin/main", "origin/master", "main", "master"):
        try:
            _git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", cand)
            return cand
        except SourceError:
            continue
    raise SourceError("cannot find a base branch; pass --base")


MAX_UNTRACKED_FILE = 1_000_000
MAX_UNTRACKED_TOTAL = 20_000_000


def _untracked_to_copy(root: Path, paths: list[str],
                       exclude: list[Path] | None = None) -> list[str]:
    """Untracked files worth reviewing: regular files, size-capped.

    Symlinks are skipped on purpose: one could point outside the repository
    and copy a secret into the reviewers' view.
    """
    out, total = [], 0
    for rel in sorted(paths):
        f = root / rel
        if f.is_symlink() or not f.is_file():
            continue
        if any(x == f.resolve() or x in f.resolve().parents for x in (exclude or [])):
            continue   # never copy manyeyes' own data (logins, results) into a review
        size = f.stat().st_size
        if size > MAX_UNTRACKED_FILE or total + size > MAX_UNTRACKED_TOTAL:
            continue
        total += size
        out.append(rel)
    return out


def local_review(path: Path, dest: Path, base: str | None = None,
                 instructions: str = "", exclude: list[Path] | None = None) -> LocalSource:
    """Copy the change under review into `dest` and describe it.

    Reviewers never see the user's working tree. They see a fresh clone at
    HEAD plus the uncommitted changes and the untracked (not ignored) files.
    Ignored files, such as `.env`, never reach a reviewer.
    """
    try:
        root = Path(_git(path, "rev-parse", "--show-toplevel"))
    except SourceError as e:
        raise SourceError(f"{path} is not inside a git repository") from e
    base = _check_ref(base or default_base(root))
    head_sha = _git(root, "rev-parse", "HEAD")
    try:
        base_sha = _git(root, "merge-base", "--end-of-options", base, "HEAD")
    except SourceError as e:
        raise SourceError(f"no merge base between {base} and HEAD") from e
    head_ref = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    patch = subprocess.run(["git", *SAFE_GIT, "-C", str(root), "diff", "--no-ext-diff",
                            "--no-textconv", "--binary", "HEAD"],
                           capture_output=True, check=True).stdout
    untracked = [p for p in _git(root, "ls-files", "-z", "--others", "--exclude-standard")
                 .split("\0") if p]
    untracked = _untracked_to_copy(root, untracked, [p.resolve() for p in (exclude or [])])
    dirty = bool(patch.strip()) or bool(untracked)
    if base_sha == head_sha and not dirty:
        raise SourceError(f"nothing to review: HEAD is at the merge base with {base}")

    # A partial-clone source repository may lack the old versions of the
    # changed files; a real patch makes git fetch them here, with the user's
    # own credentials, before the copy is made.
    # Best effort: inside the container the repository is read-only.
    try:
        _git(root, "diff", "--no-ext-diff", "--no-textconv", "--patch", "--output=/dev/null",
             base_sha, "HEAD")
    except SourceError:
        pass
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git(dest.parent, "clone", "--quiet", "--local", "--no-hardlinks", "--no-checkout",
             str(root), str(dest))
        _git(dest, "checkout", "--quiet", "--detach", head_sha)
        _git(dest, "remote", "remove", "origin")
        if patch.strip():
            _git(dest, "apply", "--binary", "--whitespace=nowarn", "-", input_bytes=patch)
        for rel in untracked:
            src_f, dst_f = root / rel, dest / rel
            if src_f.is_file() and not src_f.is_symlink():
                dst_f.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_f, dst_f)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    name = repo_name(root)
    return LocalSource(root, dest, ReviewTask(
        label=name, base_ref=base, base_sha=base_sha, head_ref=head_ref,
        head_sha=head_sha, dirty=dirty, instructions=instructions), name)


# --- context repositories ---------------------------------------------------

CONTEXT_MAX = 5
_CONTEXT_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:@([A-Za-z0-9_./-]+))?$")


def parse_context(items: list[str] | None) -> list[tuple[str, str | None]]:
    """["owner/name", "owner/other@branch"] -> [(repo, ref or None)], validated."""
    out: list[tuple[str, str | None]] = []
    for raw in items or []:
        item = str(raw).strip()
        if not item:
            continue
        m = _CONTEXT_RE.match(item)
        if not m:
            raise SourceError(f"context repository must look like owner/name or "
                              f"owner/name@branch, got {item!r}")
        repo, ref = m.group(1), m.group(2)
        if ref:
            _check_ref(ref)
        if all(r != repo for r, _ in out):
            out.append((repo, ref))
    if len(out) > CONTEXT_MAX:
        raise SourceError(f"at most {CONTEXT_MAX} context repositories, got {len(out)}")
    return out


async def _locked(path: Path, timeout: float = 600.0):
    """An exclusive file lock (CLI and server may share a cache). Polls without
    blocking, so a cancelled or timed-out job never leaves a thread stuck."""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")  # noqa: SIM115 - released by the caller
    waited = 0.0
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if waited >= timeout:
                fh.close()
                raise SourceError(f"timed out waiting for the cache lock {path.name}") from None
            await asyncio.sleep(0.5)
            waited += 0.5
        except BaseException:
            fh.close()
            raise


def context_cache_dir(cache_root: Path, repo: str) -> Path:
    """Cache folder of one context repository: <cache>/repos/<owner>/<name>.
    `repo` must be a validated owner/name (see parse_context)."""
    owner, name = repo.split("@", 1)[0].split("/")
    return cache_root / "repos" / owner / name


def context_lock(cache_root: Path, repo: str) -> Path:
    owner, name = repo.split("@", 1)[0].split("/")
    return cache_root / "locks" / owner / name


KEEP_REFS = 10


async def prepare_context(items: list[str] | None, cache_root: Path, dest_root: Path, *,
                          token: str, timeout: float) -> list[tuple[str, str, str, str]]:
    """Read-only snapshots of related repositories for one job.

    Each repository has a full clone in `cache_root` that is only fetched
    after the first time, so a context repository costs seconds per job. The
    job gets its own clone that shares the cache's objects (`--shared`), so
    jobs never see each other's checkouts and the reviewers never need the
    network or a token. Returns (owner/name, ref, sha, path) tuples.
    """
    env = git_auth_env(token)
    out: list[tuple[str, str, str, str]] = []
    for repo, ref in parse_context(items):
        cache = context_cache_dir(cache_root, repo)
        lock = await _locked(context_lock(cache_root, repo), timeout)
        try:
            url = f"https://github.com/{repo}.git"
            if not (cache / ".git").exists():
                cache.mkdir(parents=True, exist_ok=True)
                await _run(["git", "init", "-q", str(cache)], env, timeout)
            # Repair a cache that a cancelled run left half set up.
            try:
                await _run(["git", "-C", str(cache), "remote", "get-url", "origin"], env, timeout)
            except SourceError:
                await _run(["git", "-C", str(cache), "remote", "add", "origin", url], env, timeout)
            if not ref:
                head = await _run_net(["git", "-C", str(cache), "ls-remote", "--symref",
                                       "origin", "HEAD"], env, timeout)
                m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", head)
                ref = m.group(1) if m else "main"
            # One flat ref per fetch: cached remote-tracking refs could otherwise
            # block a branch name below an old one (release vs release/v2).
            try:
                await _fetch_branch_or_commit(cache, env, timeout, ref,
                                              "refs/manyeyes/fetched", "refs/manyeyes/fetched")
            except RefNotFound as e:
                raise SourceError(f"{repo}: {e}") from e
            except SourceError as e:
                # git's own words, without the command (it names a local path).
                msg = str(e)
                cause = ("timed out" if msg.startswith("timed out")
                         else msg.split(" failed: ", 1)[-1].strip()[-200:])
                raise SourceError(f"{repo}: cannot fetch {ref!r}: {cause}") from e
            sha = await _run(["git", "-C", str(cache), "rev-parse", "refs/manyeyes/fetched"],
                             env, timeout)
            # Keep recent commits reachable for jobs still reading them; drop older ones.
            await _run(["git", "-C", str(cache), "update-ref", f"refs/manyeyes/keep/{sha}", sha],
                       env, timeout)
            keeps = (await _run(["git", "-C", str(cache), "for-each-ref",
                                 "--sort=-creatordate", "--format=%(refname)",
                                 "refs/manyeyes/keep/"], env, timeout)).split()
            for old in keeps[KEEP_REFS:]:
                await _run(["git", "-C", str(cache), "update-ref", "-d", old], env, timeout)
            context_lock(cache_root, repo).touch()   # "last used", for eviction
        finally:
            lock.close()
        owner, name = repo.split("/")
        dest = dest_root / owner / name
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await _run(["git", "clone", "-q", "--shared", "--no-checkout", str(cache), str(dest)],
                   env, timeout)
        await _run(["git", "-C", str(dest), "checkout", "-q", "--detach", sha], env, timeout)
        out.append((repo, ref, sha, str(dest)))
    return out


def evict_context_cache(cache_root: Path, older_than_s: float) -> list[str]:
    """Drop cached repositories nobody used for a while. A locked (busy) one is skipped."""
    import fcntl
    import time as _t
    removed = []
    locks = cache_root / "locks"
    if not locks.is_dir():
        return removed
    for lock in locks.glob("*/*"):
        try:
            if lock.stat().st_mtime > _t.time() - older_than_s:
                continue
            with open(lock, "a") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                repo = f"{lock.parent.name}/{lock.name}"
                shutil.rmtree(context_cache_dir(cache_root, repo), ignore_errors=True)
                removed.append(repo)
        except OSError:
            continue
    return removed


# --- GitHub ---------------------------------------------------------------

def git_auth_env(token: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"   # English messages: the ref and retry checks match on them
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"})
    return env


# Waits before each retry of a GitHub call that failed with a 5xx or a
# connection error. A 4xx is never retried, and neither is a timeout: the
# 30 s timeout is already the budget for one call.
RETRY_DELAYS_S = (1.0, 3.0)
_HTTP_RETRY = (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)


async def _github_get(url: str, headers: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30) as client:
        for delay in (*RETRY_DELAYS_S, None):
            try:
                r = await client.get(url, headers=headers)
            except _HTTP_RETRY:
                if delay is None:
                    raise
            else:
                if r.status_code < 500 or delay is None:
                    return r
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def resolve_pr(repo: str, pr: int, token: str) -> tuple[str, str]:
    """(head ref, base ref) of a pull request."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{repo}/pulls/{pr}"
    try:
        r = await _github_get(url, headers)
    except httpx.TransportError as e:
        raise SourceError(f"cannot reach GitHub for {repo}#{pr}: {type(e).__name__}") from e
    if r.status_code != 200:
        raise SourceError(f"GitHub returned {r.status_code} for {repo}#{pr}")
    data = r.json()
    head, base = (data.get("head") or {}), (data.get("base") or {})
    head_repo = (head.get("repo") or {}).get("full_name")
    if head_repo is None or head_repo.lower() != repo.lower():
        # Fork PR, or the fork was deleted: fetch the PR ref itself.
        return f"refs/pull/{pr}/head", base.get("ref", "")
    return head.get("ref", ""), base.get("ref", "")


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    import os
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        pass


async def _run(argv: list[str], env: dict[str, str], timeout: float,
               ok_codes: tuple[int, ...] = (0,)) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True)   # own process group: git's helpers die with it
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _kill_group(proc)
        raise SourceError(f"timed out after {int(timeout)}s: {' '.join(argv[:3])}") from None
    except asyncio.CancelledError:
        await _kill_group(proc)
        raise
    text = out.decode(errors="replace")
    if proc.returncode not in ok_codes:
        raise SourceError(f"{' '.join(argv[:3])} failed: {text.strip()[-300:]}")
    return text.strip()


# git errors worth one retry: a 5xx from the server or a dropped connection.
# An HTTP 4xx in the same message wins: it never gets a retry.
HTTP_4XX_RE = re.compile(r"returned error: 4\d\d|HTTP 4\d\d")
NET_ERROR_RE = re.compile(
    r"returned error: 5\d\d|HTTP 5\d\d|Could not resolve host|Failed to connect"
    r"|Connection reset|Connection refused|Connection timed out|early EOF"
    r"|remote end hung up unexpectedly|unexpected disconnect|gnutls_handshake"
    r"|SSL_ERROR_SYSCALL|SSL_connect|TLS connection was non-properly terminated")


async def _run_net(argv: list[str], env: dict[str, str], timeout: float) -> str:
    """_run for a git command that talks to the remote: retry a network error once."""
    try:
        return await _run(argv, env, timeout)
    except SourceError as e:
        msg = str(e)
        if ("couldn't find remote ref" in msg or HTTP_4XX_RE.search(msg)
                or not NET_ERROR_RE.search(msg)):
            raise
    await asyncio.sleep(RETRY_DELAYS_S[0])
    return await _run(argv, env, timeout)


# git's words for "the remote has no such object" when fetching by SHA.
MISSING_OBJECT_RE = re.compile(r"not our ref|couldn't find remote ref|no such remote ref"
                               r"|unadvertised object|upload-pack: not our")


async def _fetch_branch_or_commit(repo_dir: Path, env: dict[str, str], timeout: float,
                                  name: str, branch_ref: str, commit_ref: str,
                                  *fetch_opts: str) -> str:
    """Fetch branch `name` into `branch_ref` and return that ref.

    When the remote has no such branch and `name` looks like a commit SHA, fetch
    that commit into `commit_ref` instead. The branch goes first, so a branch
    whose name looks like hex still works. GitHub serves a reachable commit by
    its full SHA; a short SHA is resolved against all branches.
    """
    fetch = ["git", "-C", str(repo_dir), "fetch", "-q", "--no-tags", *fetch_opts, "origin"]
    try:
        await _run_net([*fetch, f"+refs/heads/{name}:{branch_ref}"], env, timeout)
        return branch_ref
    except SourceError as e:
        if "couldn't find remote ref" not in str(e):
            raise
        if not SHA_RE.fullmatch(name):
            raise RefNotFound(f"no branch {name!r} (tags are not supported)") from e
    sha = name.lower()
    if len(sha) == 40:
        try:
            await _run_net([*fetch, f"+{sha}:{commit_ref}"], env, timeout)
        except SourceError as e:
            if not MISSING_OBJECT_RE.search(str(e)):
                raise
            raise RefNotFound(f"{name!r} is not a branch, and the remote does not serve "
                              f"it as a commit") from e
        full = sha
    else:
        await _run_net([*fetch, "--prune", "+refs/heads/*:refs/manyeyes/scan/*"],
                       env, timeout)
        full = ""
    try:
        full = await _run(["git", "-C", str(repo_dir), "rev-parse", "--verify",
                           "--end-of-options", f"{full or sha}^{{commit}}"], env, timeout)
    except SourceError as e:
        msg = str(e)
        if "timed out" in msg:
            raise
        if "ambiguous" in msg:
            raise RefNotFound(f"short SHA {name!r} is ambiguous; pass more characters") from e
        hint = "" if full else " on any branch (pass the full 40-character SHA)"
        raise RefNotFound(f"{name!r} is not a branch, and not a commit{hint}") from e
    await _run(["git", "-C", str(repo_dir), "update-ref", commit_ref, full], env, timeout)
    return commit_ref


async def clone_review(repo: str, dest: Path, *, token: str, timeout: float,
                       pr: int | None = None, head: str | None = None,
                       base: str | None = None, instructions: str = "") -> ReviewTask:
    """Clone `repo` into `dest` with head and base fetched; return the task.

    A partial clone (commits and trees, no file contents) keeps large
    repositories fast. The checkout fetches every file at the head, and a diff
    against the merge base fetches the old versions of the changed files, so
    reviewers find everything a review needs locally: inside the jail git has
    no token and lazy fetching is switched off.
    """
    if not REPO_RE.match(repo or ""):
        raise SourceError(f"repo must look like owner/name, got {repo!r}")
    if pr is not None:
        head, pr_base = await resolve_pr(repo, pr, token)
        base = base or pr_base
    if not head:
        raise SourceError("give a PR number or a head branch")
    _check_ref(head)
    if base:
        _check_ref(base)
    env = git_auth_env(token)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        return await _clone(repo, dest, env, timeout, pr, head, base, instructions)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise


async def _clone(repo: str, dest: Path, env: dict[str, str], timeout: float, pr: int | None,
                 head: str, base: str | None, instructions: str) -> ReviewTask:
    url = f"https://github.com/{repo}.git"
    await _run(["git", "init", "-q", str(dest)], env, timeout)
    await _run(["git", "-C", str(dest), "remote", "add", "origin", url], env, timeout)
    if not base:
        out = await _run_net(["git", "-C", str(dest), "ls-remote", "--symref", "origin",
                              "HEAD"], env, timeout)
        m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", out)
        base = m.group(1) if m else "main"
    # A PR always fetches refs/pull/N/head: it exists for forks, deleted forks
    # and moved branches alike.
    # Outside refs/remotes/origin/, so no base branch name can overwrite it.
    head_ref = "refs/manyeyes/head"
    blobless = "--filter=blob:none"
    if pr is not None or head.startswith("refs/"):
        head_src = f"refs/pull/{pr}/head" if pr is not None else head
        await _run_net(["git", "-C", str(dest), "fetch", "-q", "--no-tags", blobless, "origin",
                        f"+{head_src}:{head_ref}"], env, timeout)
    else:
        await _fetch_branch_or_commit(dest, env, timeout, head, head_ref, head_ref, blobless)
    base_ref = await _fetch_branch_or_commit(dest, env, timeout, base,
                                             f"refs/remotes/origin/{base}",
                                             "refs/manyeyes/base", blobless)
    await _run(["git", "-C", str(dest), "checkout", "-q", "--detach", head_ref], env, timeout)
    head_sha = await _run(["git", "-C", str(dest), "rev-parse", "HEAD"], env, timeout)
    try:
        base_sha = await _run(["git", "-C", str(dest), "merge-base", base_ref, "HEAD"],
                              env, timeout)
    except SourceError as e:
        raise SourceError(f"{head} and {base} share no history") from e
    # Prefetch the base side of every changed file (the head side came with
    # the checkout).
    # (A real patch: `--quiet` compares object ids and would fetch nothing.)
    await _run(["git", "-C", str(dest), "diff", "--no-ext-diff", "--no-textconv", "--patch",
                "--output=/dev/null", base_sha, "HEAD"], env, timeout)
    label = f"{repo}#{pr}" if pr is not None else f"{repo}@{head}"
    # A commit base shows as its full SHA: a short one can turn ambiguous later.
    base_label = (f"origin/{base}" if base_ref.startswith("refs/remotes/") else
                  await _run(["git", "-C", str(dest), "rev-parse", base_ref], env, timeout))
    return ReviewTask(label=label, base_ref=base_label, base_sha=base_sha,
                      head_ref=head if pr is None else f"PR #{pr} ({head})",
                      head_sha=head_sha, instructions=instructions)
