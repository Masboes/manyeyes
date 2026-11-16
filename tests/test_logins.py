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
        r._procs["codex#1"].kill_reason = Outcome.STRAGGLER_KILLED   # what kill() records
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
