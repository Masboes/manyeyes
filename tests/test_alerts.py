from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from manyeyes.alerts import crossings, long_window, message, send, usage_alerts
from manyeyes.config import ConfigError, Settings, validate
from manyeyes.store.db import Store
from manyeyes.usage import Window

URL = "https://ntfy.example/topic-abc"


def w(pct, window="week", provider="codex", login="default", error=None, resets_at=None):
    return Window(provider, window, pct, resets_at, login=login, error=error)


def test_long_windows_only():
    assert long_window("week") and long_window("week (Fable)") and long_window("7 days")
    assert not long_window("5 hours") and not long_window("window")


def test_alert_once_per_crossing():
    key = ("codex", "default", "week")
    assert crossings([w(81)], {key: 79}, 80) == [w(81)]
    assert crossings([w(85)], {key: 81}, 80) == []          # already above
    assert crossings([w(3)], {key: 86}, 80) == []           # the window reset
    assert crossings([w(80)], {}, 80) == [w(80)]            # no earlier reading
    assert crossings([w(90, window="5 hours")], {}, 80) == []
    assert crossings([w(None, error="boom")], {}, 80) == []
    assert crossings([w(99)], {}, 0) == []                  # off


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
    assert got[0].headers["Click"] == "https://x.example/me"


def test_failed_send_retries_and_never_logs_the_address(settings, caplog):
    got, tr = _recorder(status=500)
    ok = asyncio.run(send(_alert_settings(settings), "T", "b", transport=tr, retry_gap_s=0))
    assert not ok and len(got) == 3
    assert "HTTP 500" in caplog.text and "topic-abc" not in caplog.text


def test_usage_alerts_compare_with_the_stored_reading(settings, tmp_path):
    store = Store(tmp_path / "a.db")
    s = _alert_settings(settings)
    store.add_usage([w(79).to_dict(), w(95, window="5 hours").to_dict()], ts=time.time() - 60)
    got, tr = _recorder()
    new = [w(82), w(97, window="5 hours"), w(81, provider="claude")]
    assert asyncio.run(usage_alerts(store, s, new, transport=tr)) == 2
    store.add_usage([x.to_dict() for x in new])
    assert asyncio.run(usage_alerts(store, s, [w(84)], transport=tr)) == 0
    assert len(got) == 2


def test_no_address_no_alert(settings, tmp_path):
    got, tr = _recorder()
    assert asyncio.run(usage_alerts(Store(tmp_path / "a.db"), settings, [w(99)],
                                    transport=tr)) == 0
    assert got == []


@pytest.mark.parametrize("bad", [{"alerts.usage_percent": 101}, {"alerts.usage_percent": -1},
                                 {"alerts.url": "ntfy.sh/topic"}])
def test_bad_alert_settings(settings, bad):
    with pytest.raises(ConfigError):
        validate(settings.with_layer("flag", bad))
