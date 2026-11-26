"""Alerts to a person: a plan window of a reviewer login is nearly full.

The server reads the plan usage every `usage.interval_min`. When a window of a
day or longer reaches `alerts.usage_percent`, one alert goes to `alerts.url`:
plain text with a `Title` header, the form that ntfy takes.

**One alert per window period.** The table `alert_state` records each window
that has had its alert. The record goes when the window drops 5 points below
the line, or when its reset time moves on by more than 12 hours (a new period).
So a restart, a reading that lacks the window, or a reading that fails sends
nothing again. A send that fails records nothing: the next reading tries again.
A send that arrives, followed by a crash before the record is written, can
repeat once.

Windows shorter than a day do not alert: they reset soon, and a login that hits
one rests by itself (logins.py).

**The address is a secret** (anyone with an ntfy topic can read it). No log line
of this module names it, and a filter keeps httpx's request log from naming it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .config import Settings
    from .store.db import Store
    from .usage import Window

log = logging.getLogger("manyeyes.alerts")

SEND_TIMEOUT_S = 10
SEND_TRIES = 3
RETRY_GAP_S = 5.0
REARM_POINTS = 5.0           # a window must drop this far below the line to alert again
NEW_PERIOD_S = 12 * 3600     # a reset time that moves on this much starts a new period
_DURATION = re.compile(r"^(\d+) (min|hours|days)\b")
_MINUTES = {"min": 1, "hours": 60, "days": 1440}


class _HideSecretUrls(logging.Filter):
    """Drops httpx's "HTTP Request: POST <url>" line for an alert address."""
    urls: set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(u in msg for u in self.urls)


logging.getLogger("httpx").addFilter(_HideSecretUrls())


def _hide(url: str) -> None:
    try:
        bare = str(httpx.URL(url).copy_with(query=None, fragment=None))
    except Exception:  # noqa: BLE001 - a bad URL is never logged by httpx either
        bare = url
    _HideSecretUrls.urls.update({bare, url.split("?", 1)[0]})


def window_minutes(name: str) -> int | None:
    """"week" and "week (Fable)": 10080; "36 hours": 2160; unknown: None."""
    if name.startswith("week"):
        return 10080
    m = _DURATION.match(name)
    return int(m.group(1)) * _MINUTES[m.group(2)] if m else None


def long_window(name: str) -> bool:
    return (window_minutes(name) or 0) >= 1440


def plan(windows: list[Window], states: dict[tuple[str, str, str], dict],
         threshold: float) -> tuple[list[Window], list[tuple[str, str, str]]]:
    """(windows to alert, records to clear) for one reading."""
    if threshold <= 0:
        return [], []
    send, clear = [], []
    for w in windows:
        if w.error or w.used_percent is None or not long_window(w.window):
            continue
        key = (w.provider, w.login, w.window)
        st = states.get(key)
        if st is not None:
            new_period = (w.resets_at and st.get("resets_at")
                          and w.resets_at > st["resets_at"] + NEW_PERIOD_S)
            if w.used_percent < threshold - REARM_POINTS or new_period:
                clear.append(key)
                st = None
        if st is None and w.used_percent >= threshold:
            send.append(w)
    return send, clear


def _left(seconds: float) -> str:
    hours = max(0, round(seconds / 3600))
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} days {hours % 24} h"


def message(w: Window, now: float | None = None) -> tuple[str, str]:
    """(title, body) of the alert for one window."""
    now = time.time() if now is None else now
    who = w.provider if w.login == "default" else f"{w.provider}@{w.login}"
    title = f"manyeyes: {who} {w.window} quota at {w.used_percent:.0f}%"
    body = f"The {w.window} window of the {who} login is {w.used_percent:.0f}% used."
    if w.resets_at:
        body += f" It resets in {_left(w.resets_at - now)}."
    return title, body


async def send(settings: Settings, title: str, body: str, *,
               transport: httpx.AsyncBaseTransport | None = None,
               retry_gap_s: float = RETRY_GAP_S) -> bool:
    """POST one alert to `alerts.url`. True when the address took it. Never raises."""
    url = settings["alerts.url"]
    if not url:
        return False
    _hide(url)
    headers = {"Title": title, "Tags": "warning"}
    public = settings["server.public_urls"]
    if public:
        headers["Click"] = public[0]
    for attempt in range(1, SEND_TRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S, transport=transport) as c:
                r = await c.post(url, content=body.encode(), headers=headers)
            if r.is_success:
                return True
            why = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001 - the message can contain the address
            why = type(e).__name__
        log.warning("alert not sent (try %d of %d): %s", attempt, SEND_TRIES, why)
        if attempt < SEND_TRIES:
            await asyncio.sleep(retry_gap_s)
    return False


async def usage_alerts(store: Store, settings: Settings, windows: list[Window], *,
                       transport: httpx.AsyncBaseTransport | None = None,
                       retry_gap_s: float = RETRY_GAP_S) -> int:
    """Send the alerts that one usage reading calls for. Returns the alerts sent."""
    threshold = float(settings["alerts.usage_percent"])
    if not settings["alerts.url"] or threshold <= 0:
        return 0
    to_send, to_clear = plan(windows, store.alert_states(), threshold)
    for key in to_clear:
        store.clear_alert(*key)
    sent = 0
    for w in to_send:
        if await send(settings, *message(w), transport=transport, retry_gap_s=retry_gap_s):
            store.set_alert(w.provider, w.login, w.window, w.resets_at)
            sent += 1
    return sent
