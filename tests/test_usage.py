from datetime import datetime
from zoneinfo import ZoneInfo

from manyeyes.usage import parse_claude, parse_codex

CLAUDE = """You are currently using your subscription to power your Claude Code usage

Current session: 100% used · resets Nov 10, 8:30pm (Europe/Amsterdam)
Current week (all models): 67% used · resets Nov 12, 4pm (Europe/Amsterdam)
Current week (Fable): 0% used · resets Nov 12, 4pm (Europe/Amsterdam)

Last 24h · 102 requests · 10 sessions
"""


def test_parse_claude():
    tz = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 11, 10, 19, 40, tzinfo=tz).timestamp()
    w = parse_claude(CLAUDE, now)
    assert [(x.window, x.used_percent) for x in w] == [
        ("5 hours", 100.0), ("week", 67.0), ("week (Fable)", 0.0)]
    assert w[0].resets_at == datetime(2026, 11, 10, 20, 30, tzinfo=tz).timestamp()
    assert w[1].resets_at == datetime(2026, 11, 12, 16, 0, tzinfo=tz).timestamp()


def test_parse_claude_year_wrap_and_unknown_zone():
    tz = ZoneInfo("UTC")
    now = datetime(2026, 12, 30, 12, tzinfo=tz).timestamp()
    w = parse_claude("Current week (all models): 5% used · resets Jan 2, 9am (UTC)", now)
    assert w[0].resets_at == datetime(2027, 1, 2, 9, tzinfo=tz).timestamp()
    w = parse_claude("Current session: 5% used · resets 9am (Not/AZone)", now)
    assert w[0].used_percent == 5.0 and w[0].resets_at is None


def test_parse_codex():
    rl = {"primary": {"usedPercent": 89, "windowDurationMins": 10080, "resetsAt": 1794396834},
          "secondary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1794300000},
          "planType": "pro"}
    w = parse_codex({"rateLimits": rl, "rateLimitsByLimitId": {"codex": rl}})
    assert [(x.window, x.used_percent, x.plan) for x in w] == [
        ("week", 89.0, "pro"), ("5 hours", 12.0, "pro")]
    assert w[0].resets_at == 1794396834


def test_reset_with_year_and_impossible_date():
    tz = ZoneInfo("UTC")
    now = datetime(2026, 12, 30, 12, tzinfo=tz).timestamp()
    w = parse_claude("Current week (all models): 5% used · resets Jan 2, 2027, 9am (UTC)", now)
    assert w[0].resets_at == datetime(2027, 1, 2, 9, tzinfo=tz).timestamp()
    w = parse_claude("Current week (all models): 5% used · resets Feb 30, 9am (UTC)", now)
    assert w[0].used_percent == 5.0 and w[0].resets_at is None


def _fake(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


async def test_probe_answers_and_kills_hung_cli(settings, tmp_path, monkeypatch):
    import asyncio
    import os

    from manyeyes import usage
    answer = ('{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":12,'
              '"windowDurationMins":300,"resetsAt":1}},"planType":"pro"}}')
    ok = _fake(tmp_path / "codex-ok", f"read x; echo '{answer}'; sleep 30\n")
    s = settings.with_layer("t", {"reviewers.codex.bin": str(ok)})
    w = await usage.probe(s)
    assert [(x.provider, x.window, x.used_percent) for x in w] == [("codex", "5 hours", 12.0)]

    pidfile = tmp_path / "child.pid"
    hang = _fake(tmp_path / "codex-hang", f"sleep 300 & echo $! > {pidfile}; wait\n")
    monkeypatch.setattr(usage, "PROBE_TIMEOUT_S", 1)
    w = await usage.probe(settings.with_layer("t", {"reviewers.codex.bin": str(hang)}))
    assert w[0].error and "did not answer" in w[0].error
    await asyncio.sleep(0.2)
    child = int(pidfile.read_text())
    try:   # the grandchild is gone (or a zombie on its way out)
        os.kill(child, 0)
        state = open(f"/proc/{child}/stat").read().split()[2]
        assert state == "Z"
    except ProcessLookupError:
        pass
    assert not list((s.data_dir / "run").glob("usage-*"))   # temp homes are removed
