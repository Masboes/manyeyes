"""Run one job end to end and record everything in the store.

Shared by the CLI (local runs) and the server queue, so both write the same
telemetry: one reviewer_runs row per reviewer, one synth_runs row, live
job_events, and the final job row.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .engine.bundle import render_markdown
from .engine.models import ReviewerRun, SwarmResult, SynthRun
from .engine.roster import build_specs
from .engine.runner import Runner
from .engine.task import Task

if TYPE_CHECKING:
    from .config import Settings
    from .store.db import Store

ProgressFn = Callable[[str, dict[str, Any]], None]


def job_title(job: dict[str, Any]) -> str:
    if job["kind"] == "spar":
        q = (job.get("prompt") or "").strip().splitlines()
        return f"Spar: {q[0][:80]}" if q else "Spar"
    target = job.get("repo") or "local"
    if job.get("pr"):
        target += f"#{job['pr']}"
    elif job.get("head_ref"):
        target += f" @ {job['head_ref']}"
    return f"Review: {target}"


def _event_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind == "reviewer_finished":
        r: ReviewerRun = payload["run"]
        return {"reviewer": r.spec.name, "outcome": str(r.outcome),
                "duration_s": r.duration_s, "reason": r.failure_reason or None}
    if kind == "synth_finished":
        s: SynthRun = payload["synth"]
        return {"outcome": s.outcome, "duration_s": s.duration_s,
                "note": s.failure_detail or None}
    return payload


async def run_job(store: Store, settings: Settings, job: dict[str, Any], task: Task,
                  workdir: Path, *, extra_ro: list[Path] | None = None,
                  slots: asyncio.Semaphore | None = None,
                  progress: ProgressFn | None = None,
                  on_runner: Callable[[Runner], None] | None = None,
                  deadline: float | None = None) -> SwarmResult:
    """`deadline` (time.monotonic) bounds the job; default: limits.job_timeout_s from now."""
    job_id = job["id"]

    def on_event(kind: str, payload: dict[str, Any]) -> None:
        if kind == "reviewer_finished":
            store.record_run(job_id, payload["run"])
        elif kind == "synth_finished":
            store.record_synth(job_id, payload["synth"])
        light = _event_payload(kind, payload)
        store.add_event(job_id, kind, light)
        if progress:
            progress(kind, light)

    specs = build_specs(job["roster"], settings)
    runner = Runner(settings, run_dir=settings.data_dir / "run" / job_id,
                    slots=slots, on_event=on_event)
    if on_runner:
        on_runner(runner)
    if job.get("status") != "running":
        store.update_job(job_id, status="running", started_at=time.time())
    if isinstance(getattr(task, "head_sha", None), str):
        store.update_job(job_id, head_sha=task.head_sha, base_sha=task.base_sha,  # type: ignore[union-attr]
                         head_ref=task.head_ref, base_ref=task.base_ref)  # type: ignore[union-attr]
    limit = float(settings["limits.job_timeout_s"])
    remaining = limit if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        store.update_job(job_id, status="failed", finished_at=time.time(),
                         error=f"job timeout: no time left after preparing the code ({int(limit)}s)")
        raise TimeoutError
    try:
        result = await asyncio.wait_for(runner.run(task, specs, workdir, extra_ro=extra_ro,
                                                   deadline=time.monotonic() + remaining),
                                        timeout=remaining)
    except TimeoutError:
        store.update_job(job_id, status="failed", finished_at=time.time(),
                         error=f"job timeout: the whole job ran past {int(limit)}s")
        raise
    except asyncio.CancelledError:
        store.update_job(job_id, status="cancelled", finished_at=time.time(),
                         error="cancelled")
        raise
    except Exception as e:
        store.update_job(job_id, status="failed", finished_at=time.time(),
                         error=f"{type(e).__name__}: {e}")
        raise
    md = render_markdown(job_title(job), result)
    store.finish_job(job_id, result, md)
    return result
