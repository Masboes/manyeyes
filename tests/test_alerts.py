from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from manyeyes.alerts import long_window, message, plan, send, usage_alerts
from manyeyes.config import ConfigError, Settings, validate
from manyeyes.store.db import Store
from manyeyes.usage import Window

URL = "https://ntfy.example/topic-abc"


def w(pct, window="week", provider="codex", login="default", error=None, resets_at=None):
    return Window(provider, window, pct, resets_at, login=login, error=error)


def test_long_windows_only():
    for name in ("week", "week (Fable)", "7 days", "2 days (Extra)", "36 hours"):
        assert long_window(name), name
    for name in ("5 hours", "300 min", "window"):
        assert not long_window(name), name


K = ("codex", "default", "week")


def test_plan_alerts_once_per_period():
    assert plan([w(81)], {}, 80) == ([w(81)], [], [])
    assert plan([w(85)], {K: {"resets_at": None}}, 80) == ([], [], [])   # already sent
    assert plan([w(77)], {K: {"resets_at": None}}, 80) == ([], [], [])   # inside the 5 points
    assert plan([w(75)], {K: {"resets_at": None}}, 80) == ([], [K], [])  # dropped 5: rearm
    # A reset between two readings that are both above the line.
    nxt = w(82, resets_at=8 * 86400)
    st = {K: {"resets_at": 86400, "alerted_at": 0.0}}
    assert plan([nxt], st, 80, now=3600) == ([nxt], [K], [])
    # A reset time that drifts by half an hour is the same period.
    assert plan([w(86, resets_at=86400 + 1800)], st, 80, now=3600) == ([], [], [])
    assert plan([w(90, window="5 hours")], {}, 80) == ([], [], [])
    assert plan([w(None, error="boom")], {}, 80) == ([], [], [])
    assert plan([w(99)], {}, 0) == ([], [], [])


def test_message_names_login_and_reset():
    title, body = message(w(86, login="work", resets_at=1000 + 3 * 86400 + 7200), now=1000)
    assert title == "manyeyes: codex@work week quota at 86%"
    assert "resets in 3 days 2 h" in body


def _alert_settings(settings: Settings, **extra) -> Settings:
    return settings.with_layer("flag", {"alerts.url": URL, **extra})


def _recorder(status=200):
    got: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        got.append(req)
        return httpx.Response(status)
    return got, httpx.MockTransport(handle)


def test_send_posts_plain_text_with_title(settings):
    got, tr = _recorder()
    s = _alert_settings(settings, **{"server.public_urls": ["https://x.example/me"]})
    assert asyncio.run(send(s, "T", "body", transport=tr))
    assert got[0].headers["Title"] == "T" and got[0].content == b"body"
    assert got[0].headers["Content-Type"].startswith("text/plain")
    assert got[0].headers["Click"] == "https://x.example/me"


def test_failed_send_retries_and_never_logs_the_address(settings, caplog):
    got, tr = _recorder(status=503)
    ok = asyncio.run(send(_alert_settings(settings), "T", "b", transport=tr, retry_gap_s=0))
    assert not ok and len(got) == 3
    assert "HTTP 503" in caplog.text and "topic-abc" not in caplog.text


def test_usage_alerts_once_and_retry_after_a_failed_send(settings, tmp_path):
    store = Store(tmp_path / "a.db")
    s = _alert_settings(settings)
    down, tr_down = _recorder(status=503)
    assert asyncio.run(usage_alerts(store, s, [w(82)], transport=tr_down, retry_gap_s=0)) == 0
    assert len(down) == 3
    got, tr = _recorder()
    new = [w(83), w(97, window="5 hours"), w(81, provider="claude")]
    assert asyncio.run(usage_alerts(store, s, new, transport=tr)) == 2   # the retry + claude
    # A restart, or a reading without the weekly window, sends nothing again.
    assert asyncio.run(usage_alerts(Store(tmp_path / "a.db"), s, [w(85)], transport=tr)) == 0
    assert asyncio.run(usage_alerts(store, s, [w(10, window="5 hours")], transport=tr)) == 0
    assert asyncio.run(usage_alerts(store, s, [w(86)], transport=tr)) == 0
    assert len(got) == 2


def test_httpx_lines_of_an_alert_send_are_dropped(settings, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    got, tr = _recorder()
    s = _alert_settings(settings, **{"alerts.url": "https://ntfy.example/a/../topic-abc?k=v"})
    assert asyncio.run(send(s, "T", "b", transport=tr))
    assert got and "topic-abc" not in caplog.text
    # Lines of other requests stay.
    asyncio.run(_other_request(tr))
    assert "HTTP Request: GET https://other.example/api" in caplog.text


async def _other_request(tr):
    async with httpx.AsyncClient(transport=tr) as c:
        await c.get("https://other.example/api")


def test_bad_address_never_raises_or_logs_it(settings, caplog):
    s = settings.with_layer("flag", {"alerts.url": "https://ntfy.example:abc/topic-abc"})
    assert not asyncio.run(send(s, "T", "b", retry_gap_s=0))
    assert "topic-abc" not in caplog.text


def test_no_address_no_alert(settings, tmp_path):
    got, tr = _recorder()
    assert asyncio.run(usage_alerts(Store(tmp_path / "a.db"), settings, [w(99)],
                                    transport=tr)) == 0
    assert got == []


@pytest.mark.parametrize("bad", [{"alerts.usage_percent": 101}, {"alerts.usage_percent": -1},
                                 {"alerts.url": "ntfy.sh/topic"},
                                 {"alerts.url": "https://ntfy.sh:abc/topic"},
                                 {"alerts.url": "https:///topic"},
                                 {"alerts.url": "https://ntfy.sh:0/topic"},
                                 {"alerts.url": "https://ntfy.sh/to\tpic"}])
def test_bad_alert_settings(settings, bad):
    with pytest.raises(ConfigError) as e:
        validate(settings.with_layer("flag", bad))
    assert "topic" not in str(e.value)


def test_record_without_reset_time_rearms_in_a_new_period():
    now = 1_000_000.0
    st = {K: {"resets_at": None, "alerted_at": now}}
    same = w(84, resets_at=now + 6 * 86400)        # this period began before the alert
    nxt = w(84, resets_at=now + 8 * 86400)         # this period began after the alert
    assert plan([same], st, 80, now=now) == ([], [], [same])   # same period: learn the reset
    assert plan([nxt], st, 80, now=now) == ([nxt], [K], [])


def test_low_threshold_rearms_at_zero():
    assert plan([w(0)], {K: {"resets_at": None}}, 4) == ([], [K], [])


def test_reading_without_reset_time_after_the_stored_reset_rearms():
    st = {K: {"resets_at": 1000.0, "alerted_at": 0.0}}
    assert plan([w(84)], st, 80, now=1000.0 + 3600) == ([], [], [])
    assert plan([w(84)], st, 80, now=1000.0 + 13 * 3600) == ([w(84)], [K], [])


def test_one_alert_per_key_in_one_reading():
    assert plan([w(90), w(91)], {}, 80) == ([w(90)], [], [])


def test_wrong_address_is_not_retried(settings):
    got, tr = _recorder(status=404)
    assert not asyncio.run(send(_alert_settings(settings), "T", "b", transport=tr, retry_gap_s=0))
    assert len(got) == 1


def test_a_failed_record_write_does_not_stop_the_others(settings, tmp_path, monkeypatch):
    store = Store(tmp_path / "a.db")
    monkeypatch.setattr(store, "set_alert", lambda *a: (_ for _ in ()).throw(OSError("x")))
    got, tr = _recorder()
    new = [w(90), w(90, provider="claude")]
    assert asyncio.run(usage_alerts(store, _alert_settings(settings), new, transport=tr)) == 2


def test_every_window_gets_its_tries(settings, tmp_path):
    got, tr = _recorder(status=500)
    new = [w(90), w(90, provider="claude"), w(90, login="seat-2")]
    asyncio.run(usage_alerts(Store(tmp_path / "a.db"), _alert_settings(settings), new,
                             transport=tr, retry_gap_s=0))
    assert len(got) == 9


def test_non_ascii_title_is_encoded(settings):
    got, tr = _recorder()
    s = _alert_settings(settings, **{"server.public_urls": ["https://bücher.example/m"]})
    assert asyncio.run(send(s, "week (Modèle)\nx", "b", transport=tr))
    assert got[0].headers["Title"].startswith("=?UTF-8?B?") and "Click" not in got[0].headers


def test_alert_without_any_reset_time_expires_after_the_window():
    st = {K: {"resets_at": None, "alerted_at": 0.0}}
    assert plan([w(84)], st, 80, now=6 * 86400)[0] == []
    assert plan([w(84)], st, 80, now=8 * 86400)[0] == [w(84)]


def test_reset_time_learned_later_is_saved(settings, tmp_path):
    store = Store(tmp_path / "a.db")
    s = _alert_settings(settings)
    got, tr = _recorder()
    assert asyncio.run(usage_alerts(store, s, [w(82)], transport=tr)) == 1
    later = time.time() + 5 * 86400
    assert asyncio.run(usage_alerts(store, s, [w(84, resets_at=later)], transport=tr)) == 0
    assert store.alert_states()[K]["resets_at"] == later


def test_known_resets_decide_and_alert_age_does_not():
    st = {K: {"resets_at": 9 * 86400, "alerted_at": 0.0}}
    # More than a week after the alert, but the known reset has not come yet.
    assert plan([w(84, resets_at=9 * 86400)], st, 80, now=8 * 86400)[0] == []


def test_a_past_reset_time_counts_as_unknown():
    st = {K: {"resets_at": None, "alerted_at": 1000.0}}
    assert plan([w(84, resets_at=500.0)], st, 80, now=2000.0) == ([], [], [])
    # After the full window, the age fallback rearms in spite of the stale reset.
    assert plan([w(84, resets_at=500.0)], st, 80, now=1000.0 + 8 * 86400)[0] == [w(84, resets_at=500.0)]


def test_a_past_reset_is_stored_as_unknown_and_alerts_once(settings, tmp_path):
    store = Store(tmp_path / "a.db")
    s = _alert_settings(settings)
    got, tr = _recorder()
    stale = w(84, resets_at=time.time() - 13 * 3600)
    assert asyncio.run(usage_alerts(store, s, [stale], transport=tr)) == 1
    assert store.alert_states()[K]["resets_at"] is None
    assert asyncio.run(usage_alerts(store, s, [stale], transport=tr)) == 0
    assert "resets in" not in message(stale)[1]
