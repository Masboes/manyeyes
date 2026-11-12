from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from manyeyes.server.app import ServerError, create_app

BOOT = "boot-secret-token"


@pytest.fixture
def server_settings(settings, tmp_path):
    settings.allowlist_file.write_text("alice\nbob # comment\n")
    return settings.with_layer("test", {
        "auth.bootstrap_token": BOOT, "auth.bootstrap_login": "alice",
        "server.trusted_proxies": "127.0.0.1,testclient",
        "server.public_urls": "https://example.org/me",
    })


@pytest.fixture
def client(server_settings):
    app = create_app(server_settings)
    with TestClient(app, base_url="http://testserver") as c:
        yield c


H = {"Authorization": f"Bearer {BOOT}"}


def wait_for(client, job_id, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}", headers=H).json()
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish: {j}")


def test_refuses_to_start_without_sign_in(settings):
    with pytest.raises(ServerError):
        create_app(settings)


def test_health_is_public_and_api_is_not(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/me", headers=H).json() == {"login": "alice", "via": "bootstrap",
                                                       "admin": False, "can_manage": True}


def test_spar_runs_end_to_end(client):
    r = client.post("/api/jobs/spar", headers=H, json={"prompt": "q", "roster": "codex:2@ok"})
    assert r.status_code == 202
    job = wait_for(client, r.json()["id"])
    assert job["status"] == "done" and job["outcomes"] == {"success": 2}
    assert [x["outcome"] for x in job["runs"]] == ["success", "success"]
    assert job["runs"][0]["input_tokens"] == 1000
    run_id = job["runs"][0]["id"]
    assert "Looks fine" in client.get(f"/api/jobs/{job['id']}/runs/{run_id}", headers=H).text
    types = [e["type"] for e in client.get(f"/api/jobs/{job['id']}/events", headers=H).json()]
    assert types[0] == "swarm_started" and types.count("reviewer_finished") == 2
    st = client.get("/api/stats", headers=H).json()
    assert st["reviewers"][0]["runs"] == 2


def test_tokens_and_allowlist_revocation(client, server_settings):
    tok = client.post("/api/tokens", headers=H, json={"name": "laptop"}).json()["token"]
    th = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/me", headers=th).json()["via"] == "token"
    # Removing alice from the allowlist revokes her token on the next request.
    time.sleep(0.01)
    server_settings.allowlist_file.write_text("bob\n")
    import os
    os.utime(server_settings.allowlist_file, (time.time() + 5, time.time() + 5))
    assert client.get("/api/me", headers=th).status_code == 401
    assert client.get("/api/me", headers=H).status_code == 401


def test_cancel_only_own_jobs(server_settings):
    s = server_settings.with_layer("t", {"auth.admins": "carol"})   # alice is not an admin
    with TestClient(create_app(s)) as client:
        store = client.app.state.store
        other = store.create_job(kind="spar", roster="codex:1@hang", prompt="q", requested_by="bob",
                                 status="cancelled")
        assert client.post(f"/api/jobs/{other['id']}/cancel", headers=H).status_code == 403
        mine = client.post("/api/jobs/spar", headers=H,
                           json={"prompt": "q", "roster": "codex:1@hang"}).json()
        time.sleep(1.0)
        r = client.post(f"/api/jobs/{mine['id']}/cancel", headers=H)
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        # Non-admins cannot change settings.
        assert client.put("/api/settings", headers=H, json={"roster": "codex:2"}).status_code == 403


def test_settings_from_ui(client):
    r = client.put("/api/settings", headers=H, json={"roster": "codex:3", "limits.quorum_fraction": 0.5})
    rows = {x["name"]: x for x in r.json()["settings"]}
    assert rows["roster"]["value"] == "codex:3" and rows["roster"]["source"] == "ui"
    assert client.put("/api/settings", headers=H, json={"roster": "nope:1"}).status_code == 422
    assert client.put("/api/settings", headers=H, json={"synth.api_key": "x"}).status_code == 422
    assert client.put("/api/settings", headers=H, json={"server.port": 1}).status_code == 422
    r = client.put("/api/settings", headers=H, json={"roster": None})
    rows = {x["name"]: x for x in r.json()["settings"]}
    assert rows["roster"]["source"] != "ui"
    audit = client.get("/api/audit", headers=H).json()
    assert audit[0]["action"] == "settings_change"


def test_prefix_and_public_url_check(client):
    # Trusted proxy: the forwarded prefix is honored, and the base must be a
    # configured public URL before an OAuth redirect is built.
    r = client.get("/auth/config", headers={"x-forwarded-prefix": "/me",
                                            "x-forwarded-proto": "https",
                                            "x-forwarded-host": "example.org"})
    assert r.json()["mcp_url"] == "https://example.org/me/mcp"


def test_mcp_tools_match_previous_contract(client):
    fixture = json.loads((Path(__file__).parent / "fixtures" / "mcp_v1_tools.json").read_text())
    from manyeyes.server.mcp import create_mcp
    m = create_mcp(client.app.state.worker.base, client.app.state.store,
                   client.app.state.worker, client.app.state.auth)
    tools = {t.name: t for t in asyncio.run(m.list_tools())}
    fixture.pop("_source")
    for name, spec in fixture.items():
        assert name in tools, name
        props = tools[name].parameters.get("properties", {})
        required = set(tools[name].parameters.get("required", []))
        for arg in spec["args"]:
            assert arg in props, f"{name}.{arg} missing"
        assert required == set(spec["required"]), f"{name} required args changed"


def test_restart_marks_running_jobs_failed(server_settings):
    from manyeyes.store.db import Store
    store = Store(server_settings.db_path)
    j = store.create_job(kind="spar", roster="codex:1", prompt="q")
    store.claim_next()
    q = store.create_job(kind="spar", roster="codex:1@ok", prompt="q2")
    app = create_app(server_settings)
    with TestClient(app) as c:
        assert store.get_job(j["id"])["status"] == "failed"
        done = wait_for(c, q["id"])
        assert done["status"] == "done"


def test_mcp_oauth_discovery_behind_prefix(server_settings):
    s = server_settings.with_layer("t", {"auth.github_client_id": "x",
                                         "auth.github_client_secret": "y",
                                         "server.public_urls": "https://ex.org/pre"})
    with TestClient(create_app(s)) as c:
        r = c.post("/mcp", json={}, headers={"accept": "application/json, text/event-stream"})
        assert r.status_code == 401   # no redirect on /mcp, and auth is required
        meta = r.headers["www-authenticate"].split('resource_metadata="')[1].rstrip('"')
        assert meta == "https://ex.org/.well-known/oauth-protected-resource/pre/mcp"
        path = meta.removeprefix("https://ex.org")
        prm = c.get(path, follow_redirects=False)
        assert prm.status_code == 200 and prm.json()["resource"] == "https://ex.org/pre/mcp"
        asm = c.get("/.well-known/oauth-authorization-server/pre/mcp", follow_redirects=False)
        assert asm.json()["authorization_endpoint"] == "https://ex.org/pre/mcp/authorize"
        # The MCP OAuth callback must not collide with the web sign-in callback.
        assert c.get("/auth/callback", follow_redirects=False).status_code == 400
    # OAuth client registrations persist in the data dir, not in a default home.
    assert (s.data_dir / "fastmcp" / "oauth-proxy").is_dir()


def test_mcp_accepts_api_token(client):
    tok = client.post("/api/tokens", headers=H, json={"name": "mcp"}).json()["token"]
    hdr = {"Authorization": f"Bearer {tok}", "accept": "application/json, text/event-stream",
           "content-type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}}}
    r = client.post("/mcp", json=init, headers=hdr, follow_redirects=False)
    assert r.status_code == 200, r.text


def test_mcp_response_shape_matches_previous_contract(client, monkeypatch):
    import manyeyes.server.mcp as mcp_mod
    monkeypatch.setattr(mcp_mod, "_caller_login", lambda: "alice")
    # Call the tools through FastMCP itself and check the keys old clients read.
    from fastmcp import Client

    from manyeyes.server.mcp import create_mcp
    m = create_mcp(client.app.state.worker.base, client.app.state.store,
                   client.app.state.worker, client.app.state.auth)

    async def go():
        async with Client(m) as c:
            r = await c.call_tool("swarm_spar", {"prompt": "q", "agents": "codex:2@ok"})
            res = r.structured_content
            ls = (await c.call_tool("swarm_ls", {"limit": 5})).structured_content
            got = await c.call_tool("swarm_get", {"id": res["id"]})
            st = (await c.call_tool("swarm_status", {"id": res["id"]})).structured_content
            return res, ls, got, st
    res, ls, got, st = asyncio.run(go())
    assert {"kind", "id", "status", "waited_seconds"} <= set(res)
    assert res["kind"] == "spar" and res["status"] == "done" and "synthesis" in res
    assert "Looks fine" in got.content[0].text   # no synthesis key: the full result
    assert st["status"] == "done" and "result_md" not in st
    rows = ls["result"] if isinstance(ls, dict) and "result" in ls else ls
    assert rows[0]["id"] == res["id"] and "log" not in rows[0]


def test_settings_bad_value_is_422(client):
    r = client.put("/api/settings", headers=H, json={"limits.reviewer_timeout_s": "abc"})
    assert r.status_code == 422


def test_cli_running_jobs_survive_server_restart(server_settings):
    from manyeyes.store.db import Store
    store = Store(server_settings.db_path)
    j = store.create_job(kind="spar", roster="codex:1", prompt="q", source="cli",
                         status="running", started_at=time.time())
    with TestClient(create_app(server_settings)):
        time.sleep(0.5)
        assert store.get_job(j["id"])["status"] == "running"


def test_ui_assets_are_content_addressed(client):
    import re
    page = client.get("/").text
    assert "__VERSION__" not in page
    css = re.search(r'href="(static/[0-9a-f]{12}/style\.css)"', page).group(1)
    r = client.get("/" + css)
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    font = css.rsplit("/", 1)[0] + "/fonts/space-grotesk-latin-600-normal.woff2"
    assert client.get("/" + font).status_code == 200


def test_quick_mode_uses_the_quick_roster(client):
    client.put("/api/settings", headers=H, json={"roster_quick": "codex:2@ok", "roster": "codex:3@ok"})
    q = client.post("/api/jobs/spar", headers=H, json={"prompt": "q", "mode": "quick"}).json()
    f = client.post("/api/jobs/spar", headers=H, json={"prompt": "q"}).json()
    assert q["roster"] == "codex:2@ok" and f["roster"] == "codex:3@ok"
    assert client.post("/api/jobs/spar", headers=H, json={"prompt": "q", "mode": "x"}).status_code == 422
    client.put("/api/settings", headers=H, json={"roster_deep": "codex:1@stdin"})
    d = client.post("/api/jobs/spar", headers=H, json={"prompt": "q", "mode": "deep"}).json()
    assert d["roster"] == "codex:1@stdin" and d["options"]["mode"] == "deep"
    wait_for(client, q["id"])
    wait_for(client, f["id"])
    wait_for(client, d["id"])


def test_review_context_validation(client):
    bad = client.post("/api/jobs/review", headers=H, json={"repo": "o/r", "pr": 1,
                                                            "context_repos": ["not a repo"]})
    assert bad.status_code == 422
    ok = client.post("/api/jobs/review", headers=H, json={"repo": "o/r", "pr": 1,
                                                           "context_repos": ["acme/lib@main"]})
    assert ok.status_code == 202 and ok.json()["options"]["context"] == ["acme/lib@main"]
    client.post(f"/api/jobs/{ok.json()['id']}/cancel", headers=H)


def test_user_limits(server_settings):
    s = server_settings.with_layer("t", {"auth.admins": "carol",
                                         "limits.user_jobs_per_hour": 2,
                                         "limits.user_reviewer_runs_per_day": 5})
    with TestClient(create_app(s)) as client:
        spar = {"prompt": "q", "roster": "codex:1@ok"}
        assert client.post("/api/jobs/spar", headers=H, json=spar).status_code == 202
        assert client.post("/api/jobs/spar", headers=H, json=spar).status_code == 202
        r = client.post("/api/jobs/spar", headers=H, json=spar)
        assert r.status_code == 429 and "per hour" in r.json()["detail"]
        assert int(r.headers["retry-after"]) > 0
        # The reviewer-run budget weighs roster size.
        store = client.app.state.store
        store._write("UPDATE usage_ledger SET ts = ts - 7200")  # out of the hour window
        # Deleting a job does not give the quota back.
        assert len(store.ledger_since("alice", 0)) == 2
        big = client.post("/api/jobs/spar", headers=H, json={"prompt": "q", "roster": "codex:4@ok"})
        assert big.status_code == 429 and "reviewer runs" in big.json()["detail"]


def test_fair_queue_caps_running_jobs_per_user(tmp_path):
    from manyeyes.store.db import Store
    st = Store(tmp_path / "x.db")
    a1 = st.create_job(kind="spar", roster="codex:1", prompt="1", requested_by="ann")
    st.create_job(kind="spar", roster="codex:1", prompt="2", requested_by="ann")
    b1 = st.create_job(kind="spar", roster="codex:1", prompt="3", requested_by="ben")
    assert st.claim_next(max_per_user=1)["id"] == a1["id"]
    # ann has one running: ben's job goes first even though ann's second is older.
    assert st.claim_next(max_per_user=1)["id"] == b1["id"]
    assert st.claim_next(max_per_user=1) is None


def test_stats_by_user(client):
    r = client.post("/api/jobs/spar", headers=H, json={"prompt": "q", "roster": "codex:2@ok"})
    wait_for(client, r.json()["id"])
    users = client.get("/api/stats", headers=H).json()["users"]
    assert users[0]["login"] == "alice" and users[0]["reviewer_runs"] == 2


def test_empty_admin_list_keeps_limits_and_ownership(server_settings):
    s = server_settings.with_layer("t", {"limits.user_jobs_per_hour": 1})
    with TestClient(create_app(s)) as client:
        spar = {"prompt": "q", "roster": "codex:1@ok"}
        assert client.post("/api/jobs/spar", headers=H, json=spar).status_code == 202
        assert client.post("/api/jobs/spar", headers=H, json=spar).status_code == 429
        other = client.app.state.store.create_job(kind="spar", roster="codex:1", prompt="q",
                                                  requested_by="bob", status="cancelled")
        assert client.post(f"/api/jobs/{other['id']}/cancel", headers=H).status_code == 403
        # Settings stay editable for everyone while no admins are named.
        assert client.put("/api/settings", headers=H, json={"roster": "codex:2"}).status_code == 200


def test_oversized_job_says_waiting_will_not_help(server_settings):
    s = server_settings.with_layer("t", {"limits.user_reviewer_runs_per_day": 3})
    with TestClient(create_app(s)) as client:
        r = client.post("/api/jobs/spar", headers=H, json={"prompt": "q", "roster": "codex:4@ok"})
        assert r.status_code == 429 and "more than the daily limit" in r.json()["detail"]
        assert "retry-after" not in r.headers


def test_version_endpoint(client):
    v = client.get("/api/version", headers=H).json()
    assert v["current"] and "update_available" in v


def test_limits_ignore_login_casing_and_forget_erases_usage(tmp_path, server_settings):
    from manyeyes.server import limits
    from manyeyes.store.db import Store
    st = Store(tmp_path / "x.db")
    s = server_settings.with_layer("t", {"limits.user_jobs_per_day": 1})
    fields = {"kind": "spar", "roster": "codex:1", "prompt": "q", "source": "api"}
    limits.admit(s, st, "Alice", "codex:1", "full", {**fields, "requested_by": "Alice"})
    with pytest.raises(limits.LimitHit):
        limits.admit(s, st, "alice", "codex:1", "full", {**fields, "requested_by": "alice"})
    with pytest.raises(limits.NotSignedIn):
        limits.admit(s, st, None, "codex:1", "full", fields)
    st.forget_user("Alice")
    assert st.ledger_since("alice", 0) == []


def test_retry_hint_after_lowering_a_limit(tmp_path, server_settings):
    import time as _t

    from manyeyes.server import limits
    from manyeyes.store.db import Store
    st = Store(tmp_path / "x.db")
    now = _t.time()
    for age in (3000, 2000, 1000):   # three jobs in the last hour, oldest first
        st.create_job(charge=("ann", "full", 1), kind="spar", roster="codex:1", prompt="q",
                      requested_by="ann", created_at=now - age)
    s = server_settings.with_layer("t", {"limits.user_jobs_per_hour": 2})
    with pytest.raises(limits.LimitHit) as e:
        limits.check(s, st, "ann", "codex:1", "full")
    # Two entries must leave the hour: the second-oldest decides (~1600 s), not the oldest (~600 s).
    assert 1500 < e.value.retry_after_s <= 1600
    assert st.prune_ledger(now - 1500) == 2


def test_fair_queue_ignores_casing_and_skips_ownerless(tmp_path):
    from manyeyes.store.db import Store
    st = Store(tmp_path / "x.db")
    a = st.create_job(kind="spar", roster="codex:1", prompt="1", requested_by="Ann")
    st.create_job(kind="spar", roster="codex:1", prompt="2", requested_by="ann")
    n = st.create_job(kind="spar", roster="codex:1", prompt="3")
    assert st.claim_next(max_per_user=1)["id"] == a["id"]
    assert st.claim_next(max_per_user=1)["id"] == n["id"]   # "ann" waits: same person
    assert st.claim_next(max_per_user=1) is None


def test_warns_when_limits_have_no_admins(server_settings, caplog):
    import logging

    from manyeyes.server import limits
    log = logging.getLogger("t")
    with caplog.at_level(logging.WARNING, "t"):
        limits.warn_if_unenforced(server_settings.with_layer("t", {"limits.user_jobs_per_day": 5}), log)
        limits.warn_if_unenforced(server_settings.with_layer(
            "t", {"limits.user_jobs_per_day": 5, "auth.admins": "carol"}), log)
    assert len(caplog.records) == 1


def test_longest_blocking_limit_wins_and_forget_ignores_case(tmp_path, server_settings):
    import time as _t

    from manyeyes.server import limits
    from manyeyes.store.db import Store
    st = Store(tmp_path / "x.db")
    st.create_job(charge=("Ann", "full", 1), kind="spar", roster="codex:1", prompt="q",
                  requested_by="Ann", created_at=_t.time() - 1000)
    s = server_settings.with_layer("t", {"limits.user_jobs_per_hour": 1,
                                         "limits.user_jobs_per_day": 1})
    with pytest.raises(limits.LimitHit) as e:
        limits.check(s, st, "ann", "codex:1", "full")
    assert "per day" in e.value.message and e.value.retry_after_s > 80000
    assert st.user_rows(0)[0]["login"] == "ann"
    st.forget_user("ann")
    assert st.list_jobs() == [] and st.ledger_since("ann", 0) == []


def test_usage_route(client, server_settings, monkeypatch):
    store = client.app.state.store
    # Probe off (the test default): nothing is shown, however recent.
    store.add_usage([{"provider": "codex", "window": "week", "used_percent": 1}])
    assert client.get("/api/usage", headers=H).json()["providers"] == []

    async def no_probe(settings):
        return []
    monkeypatch.setattr("manyeyes.usage.probe", no_probe)
    with TestClient(create_app(server_settings.with_layer("t", {"usage.interval_min": 30}))) as c:
        store = c.app.state.store
        store._write("DELETE FROM usage_snapshots")
        t = time.time()
        store.add_usage([{"provider": "codex", "window": "week", "used_percent": 40}], ts=t - 200)
        store.add_usage([{"provider": "codex", "window": "week", "used_percent": 45}], ts=t - 100)
        store.add_usage([{"provider": "codex", "window": "", "error": "boom"}], ts=t)
        store.add_usage([{"provider": "claude", "window": "week", "used_percent": 9}],
                        ts=t - 5 * 3600)
        u = c.get("/api/usage", headers=H).json()
        # The newest good reading stays; the later error shows next to it; stale data drops.
        assert [(p["provider"], [w["used_percent"] for w in p["windows"]], p["error"])
                for p in u["providers"]] == [("codex", [45], "boom")]
        assert "history" not in u
        h = c.get("/api/usage?history=true", headers=H).json()["history"]
        assert [r["used_percent"] for r in h] == [9, 40, 45]   # oldest first
