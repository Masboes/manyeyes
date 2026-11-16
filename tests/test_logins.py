from __future__ import annotations

import time
from pathlib import Path

import pytest

from manyeyes.engine.models import Outcome
from manyeyes.engine.roster import build_specs
from manyeyes.engine.runner import Runner
from manyeyes.engine.task import SparTask
from manyeyes.logins import DEFAULT, LoginError, LoginPool, list_logins, login_home
from manyeyes.store.db import Store


def add_login(settings, name: str, kind: str = "codex") -> Path:
    home = login_home(settings.auth_dir, kind, name)
    home.mkdir(parents=True, exist_ok=True)
    (home / ("auth.json" if kind == "codex" else ".credentials.json")).write_text("{}")
    return home


@pytest.fixture
def store(settings):
    return Store(settings.data_dir / "manyeyes.db")


def test_list_logins_default_first_and_safe_names(settings):
    add_login(settings, "zeta")
    add_login(settings, "alpha")
    (settings.auth_dir / "codex@Bad Name").mkdir()
    (settings.auth_dir / "codex@default").mkdir()      # reserved: "default" is auth/codex
    (settings.auth_dir / "codex@x\n").mkdir()
    assert [x.name for x in list_logins(settings.auth_dir, "codex")] == [DEFAULT, "alpha", "zeta"]
    for bad in ("..", "a/b", "", "-x", "X", "abc\n"):
        with pytest.raises(LoginError):
            login_home(settings.auth_dir, "codex", bad)


def test_pick_prefers_quota_left_then_spreads(settings, store):
    add_login(settings, "spare")
    pool = LoginPool(settings.with_layer("t", {"usage.interval_min": 30}), store)
    now = time.time()
    store.add_usage([{"provider": "codex", "login": "default", "window": "week",
                      "used_percent": 70, "resets_at": now + 3600},
                     {"provider": "codex", "login": "spare", "window": "week",
                      "used_percent": 20, "resets_at": now + 3600}])
    first = pool.pick("codex")
    assert first.name == "spare"
    # Each running reviewer counts as 10 points: spare fills up to the default's 70.
    for _ in range(5):
        pool.acquire(pool.pick("codex"))
    assert pool.inflight[("codex", "spare")] == 5
    pool.acquire(pool.pick("codex"))
    assert pool.inflight[("codex", "default")] == 1


def test_switched_off_and_resting_logins(settings, store):
    add_login(settings, "spare")
    pool = LoginPool(settings, store)
    store.set_login("codex", "default", enabled=0)
    assert pool.pick("codex").name == "spare"
    until = pool.rest(pool.pick("codex"))
    assert until > time.time()
    # Every usable login rests: the one whose rest ends first, so a reviewer fails fast.
    assert pool.pick("codex").name == "spare"
    assert pool.pick("codex", exclude=("spare",)) is None
    store.set_login("codex", "spare", enabled=0)
    assert pool.pick("codex") is None and "switched off" in pool.why_none("codex")


def test_rest_until_the_full_window_resets(settings, store):
    pool = LoginPool(settings, store)
    login = list_logins(settings.auth_dir, "codex")[0]
    reset = time.time() + 7200
    store.add_usage([{"provider": "codex", "login": "default", "window": "week",
                      "used_percent": 100, "resets_at": reset}])
    assert pool.rest(login) == reset
    assert store.login_states()[("codex", "default")]["resting_until"] == reset


async def test_rate_limited_reviewer_moves_to_another_login(settings, store, tmp_path):
    add_login(settings, "spare")
    pool = LoginPool(settings, store)
    events = []
    r = Runner(settings, pool=pool, on_event=lambda k, p: events.append((k, p)))
    res = await r.run(SparTask("q"), build_specs("codex:1@limited-spare", settings), tmp_path)
    run = res.runs[0]
    assert run.outcome == Outcome.SUCCESS and run.login == "spare"
    assert "codex@spare" in run.review
    assert [p["login"] for k, p in events if k == "reviewer_retry"] == ["default"]
    assert pool.resting_until(list_logins(settings.auth_dir, "codex")[0]) is not None
    assert not pool.inflight


async def test_no_second_login_keeps_the_rate_limit(settings, store, tmp_path):
    pool = LoginPool(settings, store)
    res = await Runner(settings, pool=pool).run(
        SparTask("q"), build_specs("codex:1@limited-spare", settings), tmp_path)
    assert res.runs[0].outcome == Outcome.RATE_LIMITED and res.runs[0].login == DEFAULT



def test_rest_uses_the_full_window_and_never_shrinks(settings, store):
    pool = LoginPool(settings, store)
    login = list_logins(settings.auth_dir, "codex")[0]
    now = time.time()
    # The 5-hour window is full; the weekly one at 91 % does not block.
    store.add_usage([{"provider": "codex", "login": "default", "window": "5 hours",
                      "used_percent": 100, "resets_at": now + 3600},
                     {"provider": "codex", "login": "default", "window": "week",
                      "used_percent": 91, "resets_at": now + 5 * 86400}])
    assert pool.rest(login) == pytest.approx(now + 3600)
    store.set_login("codex", "default", resting_until=now + 9000)
    assert pool.rest(login) == pytest.approx(now + 9000)


async def test_a_passing_429_retries_without_a_rest(settings, store, tmp_path, monkeypatch):
    from manyeyes.engine import runner as runner_mod
    add_login(settings, "spare")
    pool = LoginPool(settings, store)
    monkeypatch.setattr(runner_mod, "plan_limit", lambda reason: False)
    res = await Runner(settings, pool=pool).run(
        SparTask("q"), build_specs("codex:1@limited-spare", settings), tmp_path)
    assert res.runs[0].ok and res.runs[0].login == "spare"
    assert store.login_states() == {}


async def test_a_kill_between_attempts_stops_the_retry(settings, store, tmp_path, monkeypatch):
    add_login(settings, "spare")
    pool = LoginPool(settings, store)
    r = Runner(settings, pool=pool)
    real_rest = pool.rest

    def rest_then_kill(login, settings=None):
        until = real_rest(login, settings)
        # A straggler kill that lands after the first process exited: kill() keeps
        # the attempt's own result but records the request (see the test below).
        r._procs["codex#1"].stop_requested = (Outcome.STRAGGLER_KILLED, "")
        return until
    monkeypatch.setattr(pool, "rest", rest_then_kill)
    res = await r.run(SparTask("q"), build_specs("codex:1@limited-spare", settings), tmp_path)
    assert res.runs[0].outcome == Outcome.RATE_LIMITED and res.runs[0].login == DEFAULT


async def test_kill_after_a_finished_attempt_is_recorded(settings, store, tmp_path):
    from manyeyes.engine.runner import _Proc
    h = _Proc()
    h.proc = None              # what the retry loop leaves between attempts
    await h.kill(Outcome.STRAGGLER_KILLED)
    assert h.kill_reason == Outcome.STRAGGLER_KILLED and h.stopped.is_set()



async def test_kill_of_an_exited_process_is_still_a_stop_request():
    import asyncio

    from manyeyes.engine.runner import _Proc
    h = _Proc()
    h.proc = await asyncio.create_subprocess_exec("true")
    await h.proc.wait()
    await h.kill(Outcome.STRAGGLER_KILLED)
    assert h.kill_reason is None                        # the attempt keeps its result
    assert h.stop_requested == (Outcome.STRAGGLER_KILLED, "")


@pytest.mark.parametrize("text,plan", [
    ("rate limit: You've hit your usage limit. Try again later.", True),
    ("5-hour limit reached ∙ resets 3pm", True),
    ("Claude AI usage limit reached|1794870000", True),
    ("insufficient_quota: You exceeded your current quota", True),
    ("rate limit: stream error: 429 Too Many Requests", False),
    ("Rate limit reached for requests per min. Please try again in 2s.", False),
    ("overloaded_error: Overloaded", False),
])
def test_plan_limit_words(text, plan):
    from manyeyes.engine.runner import plan_limit
    assert plan_limit(text) is plan


def test_rest_is_atomic_max_and_forget_clears_the_admin(settings, store):
    now = time.time()
    store.set_login("codex", "default", enabled=0, by="Alice")
    assert store.extend_rest("codex", "default", now + 7200, by="manyeyes") == now + 7200
    assert store.extend_rest("codex", "default", now + 1800, by="manyeyes") == now + 7200
    store.set_login("codex", "default", enabled=1, by="Alice")
    store.forget_user("alice")
    assert store.login_states()[("codex", "default")]["updated_by"] == "(forgotten)"


async def test_three_logins_two_attempts_no_false_retry_event(settings, store, tmp_path):
    add_login(settings, "aaa")
    add_login(settings, "bbb")
    pool = LoginPool(settings, store)
    events = []
    res = await Runner(settings, pool=pool, on_event=lambda k, p: events.append(k)).run(
        SparTask("q"), build_specs("codex:1@limited-zzz", settings), tmp_path)
    run = res.runs[0]
    assert run.outcome == Outcome.RATE_LIMITED
    assert events.count("reviewer_retry") == 1          # two attempts, one retry
    assert run.usage.input_tokens is None or run.usage.input_tokens >= 0
