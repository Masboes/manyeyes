"""Run a roster of reviewers in parallel, then the synthesis.

Lifecycle per reviewer: wait for a slot, start (in the jail), read stdout and
stderr until exit or until a deadline, classify, report.

Straggler policy: only successful reviews count toward quorum
(ceil(quorum_fraction x N), at most N - 1 for N >= 3: see quorum_of). Once quorum is reached, the reviewers still
running get `straggler_grace_s`, then they are killed. Failures never arm the
timer, so a burst of fast failures cannot cut short the reviewers that work.

The kill is family-aware (a family is a reviewer kind: codex, claude). When
the grace runs out, a reviewer whose family has no successful review yet is
not killed: agreement across families is the strongest signal, so one review
of each family is worth the wait. Such a reviewer runs on until its own
timeout (`reviewer_timeout_s`), or until a reviewer of its family succeeds;
then the rest of that family is killed at once. When the caller gives a job
deadline, a spared reviewer is also killed in time to leave the synthesis its
full budget, so a long wait never costs the reviews already done.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import signal
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import jail as jail_mod
from .classify import classify_failure, tail
from .models import Outcome, ReviewerRun, ReviewerSpec, SwarmResult
from .reviewers import ADAPTERS, Reviewer
from .synth import synthesize
from .task import Task

if TYPE_CHECKING:
    from ..config import Settings
    from ..logins import LoginPool

log = logging.getLogger("manyeyes.runner")
EventFn = Callable[[str, dict[str, Any]], Awaitable[None] | None]
KILL_GRACE_S = 3.0
MAX_ATTEMPTS = 2   # with a login pool: the first login, then one other
# Words of a used-up plan, as opposed to a passing 429 or an overloaded provider.
# Codex: "You've hit your usage limit." Claude: "5-hour limit reached ∙ resets 3pm",
# "Claude AI usage limit reached". A per-minute throttle ("try again in 2 seconds")
# is not a used-up plan.
PLAN_LIMIT_RE = re.compile(r"usage limit|quota|(5-hour|weekly|daily|session|opus|sonnet) "
                           r"limit|limit reached.{0,40}resets|hit your limit|"
                           r"out of (credits|messages)", re.IGNORECASE)
THROTTLE_RE = re.compile(r"try again in \d+(\.\d+)? ?(ms|s|sec|seconds?)\b|per minute|"
                         r"requests per|tokens per min", re.IGNORECASE)


def quorum_of(fraction: float, n: int) -> int:
    """Successes that start the straggler grace: ceil(fraction x n). With a
    fraction below 1 and three or more reviewers, at most n - 1, so a small
    roster (quick mode: 3) still gets the grace. A reviewer whose family has no
    success yet is still spared (see the module docstring)."""
    q = max(1, math.ceil(fraction * n))
    return min(q, n - 1) if fraction < 1 and n >= 3 else q


def plan_limit(text: str) -> bool:
    """A used-up plan, not a passing throttle."""
    return bool(PLAN_LIMIT_RE.search(text or "")) and not THROTTLE_RE.search(text or "")
DRAIN_S = 5.0
# Room kept before the job deadline, on top of the synthesis timeout: the
# kill itself, the result rendering and the store writes.
DEADLINE_MARGIN_S = 30.0


class SwarmError(RuntimeError):
    pass


class _Proc:
    """One reviewer process plus the reason it was killed, if it was."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.kill_reason: Outcome | None = None
        self.kill_detail = ""   # failure_reason for this kill, when the default is wrong
        self.stopped = asyncio.Event()   # set on the first kill: ends a wait for a slot
        # Any kill request, also one that came after the process had exited: a
        # retry on another login must never start after it.
        self.stop_requested: tuple[Outcome, str] | None = None

    async def kill(self, reason: Outcome, detail: str = "") -> None:
        if self.stop_requested is None:
            self.stop_requested = (reason, detail)
        p = self.proc
        if p is not None and p.returncode is not None:
            # It exited on its own: keep its real result, but reap any child
            # left in its process group.
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return
        if self.kill_reason is None:
            # The first reason wins, and its detail goes with it.
            self.kill_reason, self.kill_detail = reason, detail
        self.stopped.set()
        if p is None:
            return
        for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, KILL_GRACE_S)):
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(p.wait(), timeout=wait)
                return
            except TimeoutError:
                continue


async def _emit(on_event: EventFn | None, kind: str, payload: dict[str, Any]) -> None:
    """Report an event. A failing listener (for example a busy database) is
    logged and never stops the reviewers."""
    if on_event is None:
        return
    try:
        res = on_event(kind, payload)
        if asyncio.iscoroutine(res):
            await res
    except Exception:  # noqa: BLE001
        log.exception("event listener failed for %s", kind)


MAX_OUTPUT = 16 * 1024 * 1024


async def _read_all(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
    """Read a stream to the end, keeping at most the last MAX_OUTPUT bytes.

    The CLIs write their final answer last, so the tail is the part to keep;
    tool output in between can be large and is not needed.
    """
    if stream is None:
        return
    while chunk := await stream.read(65536):
        buf.extend(chunk)
        if len(buf) > MAX_OUTPUT:
            del buf[: len(buf) - MAX_OUTPUT]


def jailed_command(settings: Settings, rev: Reviewer, argv: list[str], workdir: Path,
                   extra_ro: list[Path] | tuple[Path, ...] = ()
                   ) -> tuple[list[str], dict[str, str]]:
    """argv and env that run `argv` with the reviewer's environment, in the jail when
    `jail.enabled`. Anything that runs a reviewer CLI must go through here."""
    env = rev.env()
    if not settings["jail.enabled"]:
        return argv, env
    ro = list(extra_ro)
    for b in (rev.binary_name, "node", "git"):
        ro += jail_mod.tool_dirs(b)
    rw = rev.rw_paths()
    for p in [*ro, *rw]:
        if jail_mod.exposes(p, settings.data_dir):
            raise jail_mod.JailError(
                f"refusing to mount {p}: it contains the data directory {settings.data_dir}. "
                f"Install {rev.binary_name} outside it (for example under /usr/local).")
    spec = jail_mod.JailSpec(workdir=workdir, env=env, ro=ro, rw=rw,
                             protect=[settings.data_dir, *settings.secret_files])
    # bwrap gets an empty environment; the jail sets the reviewer's.
    return jail_mod.build_argv(spec) + argv, {"PATH": "/usr/bin:/bin"}


class Runner:
    def __init__(self, settings: Settings, *, run_dir: Path | None = None,
                 slots: asyncio.Semaphore | None = None,
                 on_event: EventFn | None = None,
                 pool: LoginPool | None = None) -> None:
        self.s = settings
        self.pool = pool
        self.run_dir = run_dir or settings.data_dir / "run" / uuid.uuid4().hex[:12]
        self.slots = slots
        self.on_event = on_event
        self.use_jail = bool(settings["jail.enabled"])
        self._procs: dict[str, _Proc] = {}
        self._cancelled = False

    # -- one reviewer -----------------------------------------------------

    def _command(self, rev: Reviewer, prompt: str, workdir: Path,
                 extra_ro: list[Path]) -> tuple[list[str], dict[str, str] | None]:
        return jailed_command(self.s, rev, rev.argv(prompt), workdir, extra_ro)

    @asynccontextmanager
    async def _slot(self, handle: _Proc) -> AsyncIterator[None]:
        """Hold a reviewer slot. A kill ends the wait for one: a reviewer that is
        killed or cancelled while queued returns at once, without a slot."""
        if self.slots is None:
            yield
            return
        acquire = asyncio.ensure_future(self.slots.acquire())
        stop = asyncio.ensure_future(handle.stopped.wait())
        held = False
        try:
            try:
                await asyncio.wait({acquire, stop}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop.cancel()
                if not acquire.done():
                    acquire.cancel()
                try:
                    held = await acquire   # True when the slot came before the cancel
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise   # our own task was cancelled: never swallow that
                    held = False
            yield
        finally:
            if held:
                self.slots.release()

    async def _run_one(self, rev: Reviewer, prompt: str, workdir: Path,
                       extra_ro: list[Path]) -> ReviewerRun:
        """One reviewer. With a login pool, the login is chosen once the reviewer
        has a slot. On a rate limit or a failed login, the reviewer runs once
        more on another login, within the same reviewer time limit. A plan limit
        also puts the login to rest."""
        if self.pool is None:
            run = await self._attempt(rev, prompt, workdir, extra_ro)
            run.login = rev.login
            return run
        handle = self._procs[rev.spec.name]
        limit = float(self.s["limits.reviewer_timeout_s"])
        async with self._slot(handle):
            first: ReviewerRun | None = None
            tried: tuple[str, ...] = ()
            end = time.monotonic() + limit
            for attempt in range(MAX_ATTEMPTS):
                if self._cancelled or handle.kill_reason or handle.stop_requested:
                    if first is not None:
                        return first
                    reason, detail = handle.stop_requested or (handle.kill_reason, "")
                    now = time.time()
                    return ReviewerRun(rev.spec, reason or Outcome.CANCELLED, started_at=now,
                                       finished_at=now,
                                       failure_reason=detail or "cancelled before start")
                login = self.pool.pick(rev.kind, exclude=tried, settings=self.s)
                if login is None:
                    if first is not None:
                        return first     # keep the real failure, not "no login"
                    now = time.time()
                    return ReviewerRun(rev.spec, Outcome.AUTH_FAILED, started_at=now,
                                       finished_at=now,
                                       failure_reason=self.pool.why_none(rev.kind, self.s))
                rev.login = login.name
                self.pool.acquire(login)
                try:
                    run = await self._attempt(rev, prompt, workdir, extra_ro, slot=False,
                                              end=end)
                finally:
                    self.pool.release(login)
                run.login = login.name
                if first is not None:
                    run.started_at = first.started_at   # the reviewer's whole time
                    run.usage = first.usage.add(run.usage)   # and all its tokens
                if run.outcome not in (Outcome.RATE_LIMITED, Outcome.AUTH_FAILED):
                    return run
                # failure_reason comes from the CLI's own error event or stderr;
                # failure_detail can hold stdout, which is the reviewed code.
                if run.outcome == Outcome.RATE_LIMITED and plan_limit(run.failure_reason):
                    until = self.pool.rest(login, settings=self.s)
                else:
                    until = None   # a passing 429 or an expired login: no rest
                first = first or run
                tried += (login.name,)
                # Forget the finished process: a kill from now on must stop the
                # next attempt (or its start), not reap the old one.
                handle.proc = None
                if (attempt + 1 >= MAX_ATTEMPTS
                        or self._cancelled or handle.kill_reason is not None
                        or handle.stop_requested is not None
                        or end - time.monotonic() < min(30.0, limit / 4)
                        or not self.pool.has_other(rev.kind, tried, settings=self.s)):
                    return run
                await _emit(self.on_event, "reviewer_retry",
                            {"reviewer": rev.spec.name, "login": login.name,
                             "outcome": str(run.outcome), "resting_until": until})
            return run

    async def _attempt(self, rev: Reviewer, prompt: str, workdir: Path,
                       extra_ro: list[Path], *, slot: bool = True,
                       end: float | None = None) -> ReviewerRun:
        """One process run. `end` (time.monotonic) is the reviewer's own deadline
        across attempts; without it, limits.reviewer_timeout_s from the start."""
        spec = rev.spec
        handle = self._procs[spec.name]
        async with self._slot(handle) if slot else nullcontext():
            if self._cancelled or handle.kill_reason is not None:
                now = time.time()
                return ReviewerRun(spec, handle.kill_reason or Outcome.CANCELLED,
                                   failure_reason=handle.kill_detail or "cancelled before start",
                                   started_at=now, finished_at=now)
            await asyncio.sleep(rev.start_delay)
            if self._cancelled or handle.kill_reason is not None:
                now = time.time()
                return ReviewerRun(spec, handle.kill_reason or Outcome.CANCELLED,
                                   failure_reason=handle.kill_detail or "cancelled before start",
                                   started_at=now, finished_at=now)
            started = time.time()
            await _emit(self.on_event, "reviewer_started",
                        {"reviewer": spec.name, "model": spec.model, "effort": spec.effort})
            if not rev.binary():
                return ReviewerRun(spec, Outcome.ERROR, started_at=started, finished_at=time.time(),
                                   failure_reason=f"{rev.binary_name} CLI not found")
            if not rev.is_logged_in():
                return ReviewerRun(spec, Outcome.AUTH_FAILED, started_at=started,
                                   finished_at=time.time(),
                                   failure_reason=f"auth: no {spec.kind} login in {rev.auth_home}")
            try:
                rev.prepare()
                argv, env = self._command(rev, prompt, workdir, extra_ro)
                handle.proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=str(workdir), env=env,
                    stdin=asyncio.subprocess.PIPE if rev.stdin_prompt
                    else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    # Own session and process group (pgid == pid) so killpg reaches
                    # the CLI's children. start_new_session works under uvloop too;
                    # process_group does not.
                    start_new_session=True,
                )
                if rev.stdin_prompt and handle.proc.stdin is not None:
                    handle.proc.stdin.write(prompt.encode())
                    await handle.proc.stdin.drain()
                    handle.proc.stdin.close()
            except (OSError, jail_mod.JailError) as e:
                if handle.proc is not None:
                    await handle.kill(Outcome.ERROR)
                return ReviewerRun(spec, Outcome.ERROR, started_at=started,
                                   finished_at=time.time(),
                                   failure_reason=f"could not start: {e}")
            proc = handle.proc
            if self._cancelled or handle.kill_reason is not None:
                # A kill (straggler, cancel) arrived while the process was being created.
                await handle.kill(handle.kill_reason or Outcome.CANCELLED)
            out, err = bytearray(), bytearray()
            readers = asyncio.gather(_read_all(proc.stdout, out), _read_all(proc.stderr, err))
            timeout = float(self.s["limits.reviewer_timeout_s"])
            wait_s = timeout if end is None else max(0.1, end - time.monotonic())
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait_s)
            except TimeoutError:
                await handle.kill(Outcome.TIMEOUT)
            except asyncio.CancelledError:
                await handle.kill(Outcome.CANCELLED)
                raise
            # The CLI is gone; reap anything it left behind in its process group.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                # A grandchild that escaped the group can hold a pipe open.
                await asyncio.wait_for(asyncio.shield(readers), timeout=DRAIN_S)
            except TimeoutError:
                readers.cancel()
            finished = time.time()

        stdout = out.decode("utf-8", errors="replace")
        stderr = err.decode("utf-8", errors="replace")
        parsed = rev.parse(stdout, stderr)
        run = ReviewerRun(spec, Outcome.SUCCESS, started_at=started, finished_at=finished,
                          exit_code=proc.returncode, usage=parsed.usage,
                          model_resolved=parsed.model)
        reason = handle.kill_reason
        if reason is not None:
            run.outcome = reason
            run.failure_reason = handle.kill_detail or {
                Outcome.TIMEOUT: f"timeout after {int(timeout)}s",
                Outcome.STRAGGLER_KILLED: "straggler kill: quorum reached, grace "
                                          f"of {self.s['limits.straggler_grace_s']}s ran out",
                Outcome.CANCELLED: "cancelled",
            }.get(reason, str(reason))
            run.failure_detail = tail(stderr or stdout)
            run.review = parsed.review   # partial text, kept for debugging only
        elif parsed.review and proc.returncode == 0:
            run.review = parsed.review
        elif proc.returncode == -9:
            # SIGKILL that we did not send: in a memory-limited container this is
            # the kernel's out-of-memory killer.
            run.outcome = Outcome.ERROR
            run.failure_reason = ("killed by SIGKILL, most likely out of memory "
                                  "(raise the container's memory limit or run fewer reviewers)")
            run.failure_detail = tail(stderr or stdout)
        else:
            # stdout carries tool output (source code), so it is not scanned for
            # rate-limit or auth words; only the CLI's error events and stderr are.
            run.outcome, run.failure_reason = classify_failure(
                parsed.error_text, stderr, "", proc.returncode)
            run.failure_detail = tail("\n".join(
                x for x in (parsed.error_text, stderr or stdout) if x))
        return run

    # -- the swarm --------------------------------------------------------

    async def cancel(self) -> None:
        self._cancelled = True
        await asyncio.gather(*(p.kill(Outcome.CANCELLED) for p in self._procs.values()))

    async def run(self, task: Task, specs: list[ReviewerSpec], workdir: Path,
                  extra_ro: list[Path] | None = None,
                  deadline: float | None = None) -> SwarmResult:
        """`deadline` (time.monotonic) is the job's own limit; spared stragglers
        are killed in time for the synthesis to finish before it."""
        if not specs:
            raise SwarmError("no reviewers")
        if self.use_jail and not jail_mod.bwrap_path():
            raise SwarmError("the reviewer jail needs bubblewrap (bwrap); install it, or pass "
                             "--no-jail to run reviewers without it")
        if self.use_jail:
            for p in [workdir, *(extra_ro or [])]:
                if jail_mod.exposes(p, self.s.data_dir):
                    raise SwarmError(f"{p} contains the data directory {self.s.data_dir}; "
                                     "the jail would expose it. Move the data directory.")
        prompt = task.prompt(depth=self.s["review.depth"])
        self.run_dir.mkdir(parents=True, exist_ok=True)
        reviewers = [ADAPTERS[s.kind](s, self.s, self.run_dir) for s in specs]
        self._procs = {s.name: _Proc() for s in specs}
        n = len(specs)
        quorum = quorum_of(float(self.s["limits.quorum_fraction"]), n)
        grace = float(self.s["limits.straggler_grace_s"])
        spare_until: float | None = None
        if deadline is not None:
            synth_s = float(self.s["limits.synth_timeout_s"]) if self.s["synth.enabled"] else 0.0
            spare_until = deadline - synth_s - DEADLINE_MARGIN_S
        await _emit(self.on_event, "swarm_started",
                    {"reviewers": [s.name for s in specs], "quorum": quorum})

        tasks = {asyncio.create_task(self._run_one(r, prompt, workdir, extra_ro or [])): r
                 for r in reviewers}
        results: dict[str, ReviewerRun] = {}
        pending = set(tasks)
        successes = 0
        succeeded_kinds: set[str] = set()
        killed: set[str] = set()
        grace_deadline: float | None = None
        grace_over = False
        spared: set[str] = set()
        kind_of = {s.name: s.kind for s in specs}
        try:
            while pending:
                timeout = None
                if grace_deadline is not None and not grace_over:
                    timeout = max(0.0, grace_deadline - time.monotonic())
                elif spare_until is not None and any(
                        tasks[t].spec.name in spared and tasks[t].spec.name not in killed
                        for t in pending):
                    timeout = max(0.0, spare_until - time.monotonic())
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    rev = tasks[t]
                    try:
                        run = t.result()
                    except Exception as e:  # noqa: BLE001 - one reviewer never sinks the run
                        now = time.time()
                        run = ReviewerRun(rev.spec, Outcome.ERROR, started_at=now,
                                          finished_at=now, failure_reason=f"engine error: {e!r}")
                    results[rev.spec.name] = run
                    successes += run.ok
                    if run.ok:
                        succeeded_kinds.add(rev.spec.kind)
                    await _emit(self.on_event, "reviewer_finished", {"run": run})
                if grace_deadline is None and successes >= quorum and pending:
                    # The grace never runs past the time the synthesis needs.
                    now = time.monotonic()
                    grace_deadline = now + grace
                    if spare_until is not None:
                        grace_deadline = max(now, min(grace_deadline, spare_until))
                    await _emit(self.on_event, "quorum_reached",
                                {"successes": successes, "grace_s": grace_deadline - now})
                if (grace_deadline is not None and not grace_over
                        and time.monotonic() >= grace_deadline):
                    grace_over = True
                    spared = {tasks[t].spec.name for t in pending
                              if tasks[t].spec.kind not in succeeded_kinds}
                    if spared:
                        log.info("grace over; %s keep running: no review of their "
                                 "family has succeeded yet", ", ".join(sorted(spared)))
                if grace_over:
                    # Kill the stragglers of every family that has a success. A
                    # spared family joins here as soon as one of its reviewers
                    # succeeds. At the spare limit, the spared ones go too.
                    out_of_time = spare_until is not None and time.monotonic() >= spare_until
                    doomed = [tasks[t].spec.name for t in pending
                              if (tasks[t].spec.kind in succeeded_kinds or out_of_time)
                              and tasks[t].spec.name not in killed]
                    details = {
                        name: "" if name not in spared
                        else "straggler kill: another reviewer of its family succeeded"
                        if kind_of[name] in succeeded_kinds
                        else "straggler kill: no time left before the job deadline"
                        for name in doomed}
                    killed.update(doomed)
                    await asyncio.gather(*(self._procs[name].kill(
                        Outcome.STRAGGLER_KILLED, details[name]) for name in doomed))
        except asyncio.CancelledError:
            await self.cancel()
            for t in pending:
                t.cancel()
            raise
        finally:
            shutil.rmtree(self.run_dir, ignore_errors=True)

        runs = [results[s.name] for s in specs]
        result = SwarmResult(runs=runs, synth=None, prompt=prompt)
        if self._cancelled:
            return result
        await _emit(self.on_event, "synth_started", {})
        result.synth = await synthesize(prompt, runs, self.s, kind=task.kind)
        await _emit(self.on_event, "synth_finished", {"synth": result.synth})
        return result
