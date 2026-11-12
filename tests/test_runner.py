from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from manyeyes.engine.models import Outcome
from manyeyes.engine.roster import build_specs
from manyeyes.engine.runner import Runner
from manyeyes.engine.task import SparTask

from .conftest import needs_bwrap


async def run(settings, roster: str, workdir: Path, **layer):
    s = settings.with_layer("test", layer) if layer else settings
    r = Runner(s)
    return await r.run(SparTask("q"), build_specs(roster, s), workdir)


async def test_all_succeed(settings, tmp_path):
    res = await run(settings, "codex:3@ok", tmp_path)
    assert [r.outcome for r in res.runs] == [Outcome.SUCCESS] * 3
    assert res.status == "done" and not res.partial
    u = res.runs[0].usage
    assert (u.input_tokens, u.cached_input_tokens, u.output_tokens, u.reasoning_tokens) == \
        (1000, 400, 50, 20)
    assert res.synth.outcome == "skipped"


async def test_straggler_killed_after_quorum(settings, tmp_path):
    # 5 reviewers, quorum ceil(0.8*5)=4. Four succeed, the fifth hangs and is
    # killed after the 1 s grace, long before the 20 s timeout.
    res = await run(settings, "codex:4@ok,codex:1@hang", tmp_path)
    outcomes = [r.outcome for r in res.runs]
    assert outcomes.count(Outcome.SUCCESS) == 4
    assert outcomes[-1] == Outcome.STRAGGLER_KILLED
    assert res.runs[-1].duration_s < 10
    assert res.status == "done" and res.partial


async def test_failures_do_not_arm_the_straggler_timer(settings, tmp_path):
    # Three fast rate-limit failures: quorum (4 of 5) becomes impossible, so
    # the two slow reviewers must run to completion, not be killed.
    res = await run(settings, "codex:3@ratelimit,codex:2@slow-2", tmp_path)
    outcomes = [r.outcome for r in res.runs]
    assert outcomes == [Outcome.RATE_LIMITED] * 3 + [Outcome.SUCCESS] * 2


async def test_timeout(settings, tmp_path):
    res = await run(settings, "codex:1@hang", tmp_path, **{"limits.reviewer_timeout_s": 1})
    r = res.runs[0]
    assert r.outcome == Outcome.TIMEOUT and "timeout after 1s" in r.failure_reason
    assert res.status == "failed"
    assert "timeout" in res.failure_summary()


async def test_classification(settings, tmp_path):
    res = await run(settings, "codex:1@ratelimit,codex:1@auth,codex:1@empty", tmp_path)
    o = {r.spec.model: r for r in res.runs}
    assert o["ratelimit"].outcome == Outcome.RATE_LIMITED
    assert "usage limit" in o["ratelimit"].failure_reason
    assert o["auth"].outcome == Outcome.AUTH_FAILED
    assert o["empty"].outcome == Outcome.ERROR


async def test_missing_login_is_auth_failure(settings, tmp_path):
    (settings.auth_dir / "codex" / "auth.json").unlink()
    res = await run(settings, "codex:1@ok", tmp_path)
    assert res.runs[0].outcome == Outcome.AUTH_FAILED


async def test_reviewer_env_has_no_secrets(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MANYEYES_SYNTH_OPENAI_API_KEY", "sk-also-not")
    monkeypatch.setenv("MANYEYES_GITHUB_TOKEN", "ghp-not")
    res = await run(settings, "codex:1@envdump", tmp_path)
    env = json.loads(res.runs[0].review)
    assert not any(k.startswith(("OPENAI", "MANYEYES", "GH_", "GITHUB")) for k in env)
    assert env["CODEX_HOME"].endswith("codex-home-1")


async def test_cancel(settings, tmp_path):
    r = Runner(settings)
    t = asyncio.create_task(r.run(SparTask("q"), build_specs("codex:2@hang", settings), tmp_path))
    await asyncio.sleep(1.0)
    await r.cancel()
    res = await asyncio.wait_for(t, 15)
    assert all(x.outcome == Outcome.CANCELLED for x in res.runs)
    assert res.status == "cancelled" and res.synth is None


async def test_slots_limit_concurrency(settings, tmp_path):
    slots = asyncio.Semaphore(1)
    r = Runner(settings, slots=slots)
    res = await r.run(SparTask("q"), build_specs("codex:2@slow-1", settings), tmp_path)
    a, b = sorted(res.runs, key=lambda x: x.started_at)
    assert b.started_at >= a.finished_at - 0.05


@needs_bwrap
async def test_runs_inside_jail(settings, tmp_path):
    s = settings.with_layer("test", {"jail.enabled": True})
    work = tmp_path / "work"
    work.mkdir()
    res = await Runner(s).run(SparTask("q"), build_specs("codex:1@envdump", s), work)
    assert res.runs[0].ok, res.runs[0].failure_detail
    env = json.loads(res.runs[0].review)
    assert set(env) <= {"PATH", "HOME", "TERM", "NO_COLOR", "LANG", "CODEX_HOME", "PWD",
                        "GIT_TERMINAL_PROMPT", "GIT_NO_LAZY_FETCH"} | {
        k for k in os.environ if k in ("TZ", "LC_ALL")}


@needs_bwrap
async def test_jail_self_test_detects_a_weak_jail(tmp_path):
    from manyeyes.engine import jail
    assert await jail.self_test(tmp_path / "data") == []
    leaks = await jail.self_test(tmp_path / "data", no_jail=True)
    assert any(x.startswith("env:") for x in leaks)
    assert any(x.startswith("file:") for x in leaks)
    leaks = await jail.self_test(tmp_path / "data", bind_data_dir=True)
    assert any(x.startswith("file:") for x in leaks)


@pytest.mark.parametrize("roster", ["", "gemini:1", "codex:0", "codex:40"])
def test_bad_rosters(settings, roster):
    from manyeyes.engine.roster import RosterError
    with pytest.raises(RosterError):
        build_specs(roster, settings)


def test_roster_overrides(settings):
    specs = build_specs("codex:2,codex:1@gpt-x/high,claude", settings)
    assert [s.name for s in specs] == ["codex#1", "codex#2", "codex#3", "claude#1"]
    assert specs[2].model == "gpt-x" and specs[2].effort == "high"
    assert specs[0].model == settings["reviewers.codex.model"]


@needs_bwrap
async def test_jail_refuses_workdir_containing_data_dir(settings, tmp_path):
    from manyeyes.engine.runner import SwarmError
    s = settings.with_layer("test", {"jail.enabled": True})
    with pytest.raises(SwarmError, match="data directory"):
        await Runner(s).run(SparTask("q"), build_specs("codex:1@ok", s), tmp_path)


def test_runner_works_under_uvloop(settings, tmp_path):
    # The server runs under uvicorn's uvloop, which rejects some subprocess
    # arguments the default loop accepts (process_group).
    uvloop = pytest.importorskip("uvloop")

    async def go():
        return await Runner(settings).run(SparTask("q"), build_specs("codex:2@ok", settings),
                                          tmp_path)
    res = uvloop.run(go())
    assert [r.outcome for r in res.runs] == [Outcome.SUCCESS] * 2


@needs_bwrap
async def test_jail_refuses_tool_dir_containing_data_dir(settings, tmp_path, fake_bin):
    # A CLI installed in a directory that contains the data dir must not be mounted.
    import shutil as sh
    exe = settings.data_dir.parent / "codex"
    sh.copy(fake_bin / "codex", exe)
    s = settings.with_layer("test", {"jail.enabled": True, "reviewers.codex.bin": str(exe)})
    work = tmp_path / "work"
    work.mkdir()
    res = await Runner(s).run(SparTask("q"), build_specs("codex:1@ok", s), work)
    assert res.runs[0].outcome == Outcome.ERROR
    assert "contains the data directory" in res.runs[0].failure_reason


def test_jail_builder_refuses_to_expose_protected_dir(tmp_path):
    from manyeyes.engine import jail
    if not jail.bwrap_path():
        pytest.skip("no bwrap")
    spec = jail.JailSpec(workdir=tmp_path / "w", env={}, ro=[tmp_path], protect=[tmp_path / "d"])
    with pytest.raises(jail.JailError, match="refusing to mount"):
        jail.build_argv(spec)
    # Implicit system mounts are checked too: a data dir under /opt is refused.
    spec = jail.JailSpec(workdir=tmp_path / "w", env={}, protect=[Path("/opt/manyeyes-data")])
    if Path("/opt").is_dir():
        with pytest.raises(jail.JailError):
            jail.build_argv(spec)


async def test_prompt_goes_on_stdin(settings, tmp_path):
    res = await Runner(settings).run(SparTask("the question"),
                                     build_specs("codex:1@stdin", settings), tmp_path)
    assert res.runs[0].review == "the question"


async def test_children_left_behind_are_reaped(settings, tmp_path):
    import os
    import time as t
    res = await Runner(settings).run(SparTask("q"), build_specs("codex:1@orphan", settings),
                                     tmp_path)
    assert res.runs[0].ok
    pid = int((tmp_path / "orphan.pid").read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        # A zombie (dead, not yet reaped by init) also counts as gone.
        if open(f"/proc/{pid}/stat").read().split()[2] == "Z":
            break
        t.sleep(0.1)
    else:
        raise AssertionError(f"child {pid} survived the reviewer")


def test_launcher_applies_resource_limits(settings, monkeypatch):
    from manyeyes.cli import launcher
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/usr/bin/docker")
    s = settings.with_layer("t", {"container.memory": "6g", "container.cpus": "8"})
    argv, _ = launcher.docker_argv(s, ["review"], interactive=False)
    joined = " ".join(argv)
    assert "--memory 6g --memory-swap 6g" in joined and "--cpus 8" in joined


def test_depth_reaches_the_prompt(settings, tmp_path):
    from manyeyes.engine.task import ReviewTask
    t = ReviewTask(label="x", base_ref="main", base_sha="a" * 40, head_ref="f", head_sha="b" * 40)
    assert "thorough assessment" not in t.prompt()
    import asyncio
    s = settings.with_layer("t", {"review.depth": "thorough"})
    # The fake reviewer returns the prompt it received on stdin.
    res = asyncio.run(Runner(s).run(t, build_specs("codex:1@stdin", s), tmp_path))
    assert res.runs[0].ok and "thorough assessment" in res.runs[0].review


def test_quick_flag_respects_set_overrides():
    from manyeyes.cli.main import build_parser, settings_from
    a = build_parser().parse_args(["--set", "roster_quick=codex:2", "review", "--quick"])
    assert settings_from(a)["roster"] == "codex:2"
    a = build_parser().parse_args(["review", "--quick", "--roster", "codex:7"])
    assert settings_from(a)["roster"] == "codex:7"


def test_claude_reports_the_model_that_answered(settings, tmp_path):
    import json as _j

    from manyeyes.engine.models import ReviewerSpec
    from manyeyes.engine.reviewers.claude import ClaudeReviewer
    rev = ClaudeReviewer(ReviewerSpec("claude", 1, "sonnet", "high"), settings, tmp_path)
    out = _j.dumps({"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 2},
                    "modelUsage": {"claude-haiku-5-5": {"outputTokens": 3},
                                   "claude-sonnet-5-5": {"outputTokens": 900}}})
    assert rev.parse(out, "").model == "claude-sonnet-5-5"


def test_deep_flag_sets_roster_and_depth():
    from manyeyes.cli.main import build_parser, settings_from
    s = settings_from(build_parser().parse_args(["review", "--deep"]))
    assert s["roster"] == s["roster_deep"] and s["review.depth"] == "thorough"
    s = settings_from(build_parser().parse_args(["review", "--quick"]))
    assert s["roster"] == s["roster_quick"] and s["review.depth"] == "standard"


def test_synthesis_prompt_keeps_singletons():
    # Without this rule every tested model dropped single-reviewer side issues.
    from manyeyes.engine.models import Outcome, ReviewerRun, ReviewerSpec
    from manyeyes.engine.synth import system_prompt
    runs = [ReviewerRun(ReviewerSpec("codex", 1, "m", "low"), Outcome.SUCCESS, "x"),
            ReviewerRun(ReviewerSpec("claude", 1, "m", "low"), Outcome.SUCCESS, "y")]
    assert "never drop one" in system_prompt(runs, "review")
    assert "never drop one" in system_prompt(runs[:1] * 2, "review")   # single family
