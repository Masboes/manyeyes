"""Run a roster of reviewers in parallel, then the synthesis.

Lifecycle per reviewer: wait for a slot, start (in the jail), read stdout and
stderr until exit or until a deadline, classify, report.

Straggler policy: only successful reviews count toward quorum
(ceil(quorum_fraction x N)). Once quorum is reached, the reviewers still
running get `straggler_grace_s`, then they are killed. Failures never arm the
timer, so a burst of fast failures cannot cut short the reviewers that work.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import signal
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
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

log = logging.getLogger("manyeyes.runner")
EventFn = Callable[[str, dict[str, Any]], Awaitable[None] | None]
KILL_GRACE_S = 3.0
DRAIN_S = 5.0


class SwarmError(RuntimeError):
    pass


class _Proc:
    """One reviewer process plus the reason it was killed, if it was."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.kill_reason: Outcome | None = None

    async def kill(self, reason: Outcome) -> None:
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
            self.kill_reason = reason
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
                 on_event: EventFn | None = None) -> None:
        self.s = settings
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

    async def _run_one(self, rev: Reviewer, prompt: str, workdir: Path,
                       extra_ro: list[Path]) -> ReviewerRun:
        spec = rev.spec
        handle = self._procs[spec.name]
        async with self.slots if self.slots is not None else nullcontext():
            if self._cancelled or handle.kill_reason is not None:
                now = time.time()
                return ReviewerRun(spec, handle.kill_reason or Outcome.CANCELLED,
                                   failure_reason="cancelled before start",
                                   started_at=now, finished_at=now)
            await asyncio.sleep(rev.start_delay)
            if self._cancelled or handle.kill_reason is not None:
                now = time.time()
                return ReviewerRun(spec, handle.kill_reason or Outcome.CANCELLED,
                                   failure_reason="cancelled before start",
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
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
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
            run.failure_reason = {
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
                  extra_ro: list[Path] | None = None) -> SwarmResult:
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
        quorum = max(1, math.ceil(float(self.s["limits.quorum_fraction"]) * n))
        grace = float(self.s["limits.straggler_grace_s"])
        await _emit(self.on_event, "swarm_started",
                    {"reviewers": [s.name for s in specs], "quorum": quorum})

        tasks = {asyncio.create_task(self._run_one(r, prompt, workdir, extra_ro or [])): r
                 for r in reviewers}
        results: dict[str, ReviewerRun] = {}
        pending = set(tasks)
        successes = 0
        grace_deadline: float | None = None
        stragglers_killed = False
        try:
            while pending:
                timeout = None
                if grace_deadline is not None and not stragglers_killed:
                    timeout = max(0.0, grace_deadline - time.monotonic())
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
                    await _emit(self.on_event, "reviewer_finished", {"run": run})
                if grace_deadline is None and successes >= quorum and pending:
                    grace_deadline = time.monotonic() + grace
                    await _emit(self.on_event, "quorum_reached",
                                {"successes": successes, "grace_s": grace})
                if (grace_deadline is not None and pending and not stragglers_killed
                        and time.monotonic() >= grace_deadline):
                    stragglers_killed = True
                    await asyncio.gather(*(self._procs[tasks[t].spec.name].kill(
                        Outcome.STRAGGLER_KILLED) for t in pending))
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
