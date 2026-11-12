from __future__ import annotations

import pytest

from manyeyes.config import ConfigError, load
from manyeyes.engine.models import Outcome, ReviewerRun, ReviewerSpec, SwarmResult, SynthRun, Usage
from manyeyes.store import stats
from manyeyes.store.db import Store


def test_precedence(tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    (data / "config.toml").write_text('roster = "codex:4"\n[reviewers.codex]\nmodel = "m-file"\n')
    (data / "secrets.env").write_text("MANYEYES_SYNTH_OPENAI_API_KEY=from-env-file\n")
    s = load(env={"MANYEYES_DATA_DIR": str(data), "MANYEYES_REVIEWERS_CODEX_MODEL": "m-env"})
    assert s["roster"] == "codex:4" and s.sources["roster"] == "file"
    assert s["reviewers.codex.model"] == "m-env" and s.sources["reviewers.codex.model"] == "env"
    assert s["synth.api_key"] == "from-env-file"
    ui = s.with_layer("ui", {"roster": "codex:6"})
    assert ui["roster"] == "codex:6" and ui.sources["roster"] == "ui"
    pub = {r["name"]: r for r in ui.public()}
    assert pub["synth.api_key"]["value"] is True   # secrets show only as set / not set


def test_secret_in_file_is_rejected(tmp_path):
    (tmp_path / "config.toml").write_text('[synth]\napi_key = "sk-x"\n')
    with pytest.raises(ConfigError, match="secret"):
        load(env={"MANYEYES_DATA_DIR": str(tmp_path)})


def test_secret_from_file_ref(tmp_path):
    f = tmp_path / "key"
    f.write_text("sk-from-file\n")
    s = load(env={"MANYEYES_DATA_DIR": str(tmp_path), "MANYEYES_SYNTH_OPENAI_API_KEY_FILE": str(f)})
    assert s["synth.api_key"] == "sk-from-file"


@pytest.mark.parametrize("env", [
    {"MANYEYES_LIMITS_QUORUM_FRACTION": "1.5"},
    {"MANYEYES_ROSTER": "nope:1"},
    {"MANYEYES_BOOTSTRAP_TOKEN": "t"},
    {"MANYEYES_REVIEWERS_CODEX_EFFORT": "ultra"},
    {"MANYEYES_REVIEWERS_CODEX_SANDBOX": "danger-full-access", "MANYEYES_JAIL_ENABLED": "0"},
])
def test_invalid(tmp_path, env):
    with pytest.raises(Exception):  # noqa: B017 - ConfigError or RosterError
        load(env={"MANYEYES_DATA_DIR": str(tmp_path), **env})


def _run(name, outcome, dur=10.0):
    kind, inst = name.split("#")
    return ReviewerRun(ReviewerSpec(kind, int(inst), "m", "medium"), outcome,
                       review="r" if outcome == Outcome.SUCCESS else "",
                       failure_reason="" if outcome == Outcome.SUCCESS else "rate limit: x",
                       started_at=100.0, finished_at=100.0 + dur,
                       usage=Usage(1000, 0, 100, 10))


def test_store_roundtrip_and_stats(tmp_path):
    st = Store(tmp_path / "x.db")
    job = st.create_job(kind="review", roster="codex:2", requested_by="me", repo="o/r", pr=1)
    assert job["status"] == "queued" and job["id"].startswith("r-")
    assert st.claim_next()["id"] == job["id"]
    runs = [_run("codex#1", Outcome.SUCCESS), _run("codex#2", Outcome.RATE_LIMITED, 2)]
    for r in runs:
        st.record_run(job["id"], r)
    res = SwarmResult(runs, SynthRun("s", "skipped", failure_detail="x"), "p")
    st.finish_job(job["id"], res, "# md")
    j = st.get_job(job["id"])
    assert j["status"] == "done" and j["partial"] == 1 and j["result_md"] == "# md"
    rs = stats.reviewer_stats(st.run_rows())
    assert rs[0]["runs"] == 2 and rs[0]["success_rate"] == 0.5
    assert rs[0]["top_failures"][0][0] == "rate limit"
    js = stats.job_stats(st.job_rows())
    assert js["success_rate"] == 1.0 and js["partial"] == 1
    # telemetry survives deletion of the job
    assert st.delete_job(job["id"], keep_telemetry=True)
    assert len(st.run_rows()) == 2 and st.get_job(job["id"]) is None


def test_reconcile_and_overrides(tmp_path):
    st = Store(tmp_path / "x.db")
    j = st.create_job(kind="spar", roster="codex:1", prompt="q")
    st.claim_next()
    assert st.reconcile_orphans() == 1
    assert st.get_job(j["id"])["status"] == "failed"
    st.set_override("roster", "codex:4", "me")
    st.set_override("roster", "codex:5", "me")
    assert st.get_overrides() == {"roster": "codex:5"}
    st.clear_override("roster")
    assert st.get_overrides() == {}
    # reopening applies no migration twice
    Store(tmp_path / "x.db")


def test_forget_user(tmp_path):
    st = Store(tmp_path / "x.db")
    st.touch_user("alice")
    st.add_token("t1", "alice", "laptop", "h1")
    st.audit("alice", "login")
    j = st.create_job(kind="spar", roster="codex:1", prompt="q", requested_by="alice")
    st.forget_user("alice")
    assert st.token_by_hash("h1") is None
    assert st.get_job(j["id"]) is None
