"""The job queue worker: claims queued jobs, prepares the code, runs the swarm.

Settings are resolved per job (file + env + web UI overrides at the moment the
job starts), so a change in the UI applies to the next job, never to a running
one. One semaphore caps the reviewers across all jobs.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
import time
from typing import TYPE_CHECKING

from .. import modes
from ..config import ConfigError, Settings
from ..engine.task import SparTask
from ..jobs import run_job
from ..sources import (
    SourceError,
    clone_review,
    context_cache_dir,
    evict_context_cache,
    prepare_context,
)

if TYPE_CHECKING:
    from ..store.db import Store

log = logging.getLogger("manyeyes.queue")


def effective_settings(base: Settings, store: Store) -> Settings:
    """Base settings plus the overrides saved from the web UI."""
    overrides = store.get_overrides()
    try:
        return base.with_layer("ui", overrides, allow_secrets=False)
    except ConfigError as e:
        log.warning("ignoring invalid UI settings: %s", e)
        return base


class Worker:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.base = settings
        self.store = store
        self.slots = asyncio.Semaphore(int(settings["server.max_concurrent_reviewers"]))
        self.tasks: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._loop: asyncio.Task | None = None
        self._retention: asyncio.Task | None = None
        self._updates: asyncio.Task | None = None
        self._usage: asyncio.Task | None = None
        from ..updates import UpdateInfo
        self.update_info = UpdateInfo()

    def _sweep_leftovers(self) -> None:
        """Remove work and run directories left by a crash.

        Only old ones: a CLI run sharing this data dir may be using a fresh one.
        """
        cutoff = time.time() - 2 * float(self.base["limits.job_timeout_s"]) - 600
        live = {j["id"] for j in self.store.list_jobs(500, status="running")}
        for sub in ("work", "run"):
            root = self.base.data_dir / sub
            if not root.is_dir():
                continue
            for d in root.iterdir():
                if d.name.split("-context")[0] in live:
                    continue   # a running job's folder, whatever its age
                try:
                    if d.is_dir() and d.stat().st_mtime < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    continue
        # Context repositories nobody asked for in 14 days are dropped from the cache.
        evict_context_cache(self.base.data_dir / "context-cache", 14 * 86400)

    def start(self) -> None:
        # Close out jobs a crash left "running" first, so the sweep can clean their folders.
        n = self.store.reconcile_orphans()
        self._sweep_leftovers()
        if n:
            log.warning("marked %d job(s) failed: the server restarted while they ran", n)
        from .limits import warn_if_unenforced
        warn_if_unenforced(effective_settings(self.base, self.store), log)
        self._loop = asyncio.create_task(self._run_loop(), name="manyeyes-queue")
        self._retention = asyncio.create_task(self._retention_loop(), name="manyeyes-retention")
        self._updates = asyncio.create_task(self._update_loop(), name="manyeyes-updates")
        self._usage = asyncio.create_task(self._usage_loop(), name="manyeyes-usage")

    def notify(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        loops = (self._loop, self._retention, self._updates, self._usage, *self.tasks.values())
        for t in loops:
            if t is not None:
                t.cancel()
        await asyncio.gather(*[t for t in loops if t is not None], return_exceptions=True)

    async def cancel(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        if job is None or job["status"] in ("done", "failed", "cancelled"):
            return False
        task = self.tasks.get(job_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A task cancelled before its first step never ran its own cleanup.
            j = self.store.get_job(job_id)
            if j and j["status"] in ("queued", "running"):
                self.store.update_job(job_id, status="cancelled", finished_at=time.time(),
                                      error="cancelled")
        else:
            self.store.update_job(job_id, status="cancelled", finished_at=time.time(),
                                  error="cancelled before start")
        return True

    async def _run_loop(self) -> None:
        while True:
            started = False
            if len(self.tasks) < int(self.base["server.max_concurrent_jobs"]):
                try:
                    es = effective_settings(self.base, self.store)
                    job = self.store.claim_next(
                        max_per_user=int(es["limits.user_concurrent_jobs"]),
                        exempt=frozenset(a.lower() for a in es["auth.admins"]))
                except Exception:  # noqa: BLE001 - never let the loop die
                    log.exception("claim failed")
                    job = None
                if job is not None:
                    t = asyncio.create_task(self._guarded(job), name=f"job-{job['id']}")
                    self.tasks[job["id"]] = t
                    t.add_done_callback(lambda _t, i=job["id"]: self.tasks.pop(i, None))
                    started = True
            if not started:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                except TimeoutError:
                    pass

    async def _guarded(self, job: dict) -> None:
        try:
            await self._run(job)
        except asyncio.CancelledError:
            j = self.store.get_job(job["id"])
            if j and j["status"] == "running":
                self.store.update_job(job["id"], status="cancelled", finished_at=time.time(),
                                      error="cancelled")
        except Exception as e:  # noqa: BLE001
            log.exception("job %s crashed", job["id"])
            j = self.store.get_job(job["id"])
            if j and j["status"] == "running":
                self.store.update_job(job["id"], status="failed", finished_at=time.time(),
                                      error=f"{type(e).__name__}: {e}")
        finally:
            self.notify()

    async def _run(self, job: dict) -> None:
        s = effective_settings(self.base, self.store)
        opts = json.loads(job.get("options_json") or "{}")
        mode = opts.get("mode", "full")
        layer: dict = {"review.depth": modes.depth_for(mode, s)}
        if mode == "deep":
            layer.update(modes.deep_limits(s))
        if opts.get("no_synth"):
            layer["synth.enabled"] = False
        s = s.with_layer("job", layer)
        work = s.data_dir / "work" / job["id"]
        ctx_dir = s.data_dir / "work" / f"{job['id']}-context"
        extra_ro: list = []
        # One deadline for the whole job: clone, reviewers and synthesis.
        deadline = time.monotonic() + float(s["limits.job_timeout_s"])
        try:
            if job["kind"] == "spar":
                task = SparTask(job["prompt"] or "")
                work.mkdir(parents=True, exist_ok=True)
            else:
                self.store.add_event(job["id"], "clone_started", {"repo": job["repo"]})
                try:
                    budget = min(float(s["limits.clone_timeout_s"]),
                                 deadline - time.monotonic())
                    task = await asyncio.wait_for(clone_review(
                        job["repo"], work, token=s["github.token"],
                        timeout=budget, pr=job.get("pr"),
                        head=job.get("head_ref"), base=job.get("base_ref"),
                        instructions=job.get("prompt") or ""), timeout=budget)
                except TimeoutError:
                    self.store.update_job(job["id"], status="failed", finished_at=time.time(),
                                          error=f"could not prepare the code within {int(budget)}s")
                    return
                except SourceError as e:
                    self.store.update_job(job["id"], status="failed", finished_at=time.time(),
                                          error=f"could not prepare the code: {e}")
                    return
                if opts.get("context"):
                    self.store.add_event(job["id"], "context_started",
                                         {"repos": opts["context"]})
                    left = deadline - time.monotonic()
                    try:
                        ctx = await asyncio.wait_for(prepare_context(
                            opts["context"], s.data_dir / "context-cache", ctx_dir,
                            token=s["github.token"],
                            timeout=min(float(s["limits.clone_timeout_s"]), left)),
                            timeout=max(1.0, left))
                    except (SourceError, TimeoutError) as e:
                        self.store.update_job(job["id"], status="failed",
                                              finished_at=time.time(),
                                              error=f"could not prepare the related repos: {e}")
                        return
                    task = dataclasses.replace(task, context=tuple(ctx))
                    # The snapshots, and ONLY the cache folders of these repos (their
                    # objects live there), read-only. Other cached repos stay hidden.
                    extra_ro = [ctx_dir] + [context_cache_dir(s.data_dir / "context-cache", c[0])
                                            for c in ctx]
            try:
                await run_job(self.store, s, job, task, work, slots=self.slots,
                              deadline=deadline, extra_ro=extra_ro)
            except TimeoutError:
                pass   # run_job recorded the job timeout
        finally:
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(ctx_dir, ignore_errors=True)

    async def _update_loop(self) -> None:
        from ..updates import check
        while True:
            self.update_info = await check(self.base)
            if self.update_info.update_available:
                log.info("manyeyes %s is available (running %s)", self.update_info.latest,
                         self.update_info.current)
            await asyncio.sleep(12 * 3600)

    async def _usage_loop(self) -> None:
        from ..usage import probe
        while (minutes := int(self.base["usage.interval_min"])) > 0:
            try:
                windows = await probe(self.base)
                if windows:
                    self.store.add_usage([w.to_dict() for w in windows])
                self.store.prune_usage(time.time() - 30 * 86400)
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("usage probe failed")
            await asyncio.sleep(minutes * 60)

    async def _retention_loop(self) -> None:
        while True:
            days = int(self.base["retention.days"])
            if days > 0:
                cutoff = time.time() - days * 86400
                for job_id in self.store.jobs_older_than(cutoff):
                    self.store.delete_job(job_id, self.base["retention.keep_telemetry"])
                    (self.base.data_dir / "results" / f"{job_id}.md").unlink(missing_ok=True)
                self._sweep_leftovers()
            # The limits look back 24 hours at most.
            self.store.prune_ledger(time.time() - 2 * 86400)
            await asyncio.sleep(6 * 3600)
