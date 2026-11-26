"""Alerts to a person: a plan window of a reviewer login is nearly full.

The server reads the plan usage every `usage.interval_min`. When a window of a
day or longer reaches `alerts.usage_percent`, one alert goes to `alerts.url`:
plain text with a `Title` header, the form that ntfy takes.

**One alert per window period.** The table `alert_state` records each window
that has had its alert. The record goes when the window drops to 5 points below
the line, or when a new period begins: the reset time moves on by more than 12
hours, or (for a record without a reset time) the alert came before the start
of the period that the reading shows, or (no reset time on either side) the
alert is older than the window.
So a restart, a reading that lacks the window, or a reading that fails sends
nothing again. A send that fails records nothing: the next reading tries again.
A send whose answer is lost (a timeout after the address took it, or a crash
before the record is written) can repeat: delivery is at least once.

Windows shorter than a day do not alert: they reset soon, and a login that hits
one rests by itself (logins.py).

**The address is a secret** (anyone with an ntfy topic can read it). No log line
of this module names it, and a filter drops httpx's log lines of an alert send.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
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


_SENDING: contextvars.ContextVar[bool] = contextvars.ContextVar("alert_send", default=False)


class _QuietWhileSending(logging.Filter):
    """Drops every httpx and httpcore line of an alert send: they show its URL.
    httpx logs in the task that sends, so the context variable marks the lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _SENDING.get()


# A filter works on the records of its own logger only, so each one gets it.
for _name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
              "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(_name).addFilter(_QuietWhileSending())


def window_minutes(name: str) -> int | None:
    """"week" and "week (Fable)": 10080; "36 hours": 2160; unknown: None."""
    if name.startswith("week"):
        return 10080
    m = _DURATION.match(name)
    return int(m.group(1)) * _MINUTES[m.group(2)] if m else None


def long_window(name: str) -> bool:
    return (window_minutes(name) or 0) >= 1440


def plan(windows: list[Window], states: dict[tuple[str, str, str], dict],
         threshold: float, now: float | None = None,
         ) -> tuple[list[Window], list[tuple[str, str, str]], list[Window]]:
    """(windows to alert, records to clear, records to give a reset time) for one reading."""
    if threshold <= 0:
        return [], [], []
    now = time.time() if now is None else now
    send, clear, backfill, seen = [], [], [], set()
    for w in windows:
        if w.error or w.used_percent is None or not long_window(w.window):
            continue
        key = (w.provider, w.login, w.window)
        st = states.get(key)
        if st is not None:
            alerted = now if st.get("alerted_at") is None else st["alerted_at"]
            length = 60 * (window_minutes(w.window) or 0)
            if st.get("resets_at") and w.resets_at:
                new_period = w.resets_at > st["resets_at"] + NEW_PERIOD_S
            elif st.get("resets_at"):   # the reading has no reset time
                new_period = now > st["resets_at"] + NEW_PERIOD_S
            elif w.resets_at:   # the alert has no reset time: did it come before this period?
                new_period = alerted < w.resets_at - length
            else:   # no reset time at all: an alert older than the window is from before
                new_period = now > alerted + length
            if w.used_percent <= max(0.0, threshold - REARM_POINTS) or new_period:
                clear.append(key)
                st = None
            elif not st.get("resets_at") and w.resets_at and w.resets_at > alerted:
                backfill.append(w)
        if st is None and w.used_percent >= threshold and key not in seen:
            send.append(w)
            seen.add(key)
    return send, clear, backfill


def _header(value: str) -> str:
    """HTTP headers are ASCII. ntfy decodes RFC 2047 for anything else."""
    value = "".join(c if c.isprintable() else " " for c in value)
    if value.isascii():
        return value
    return "=?UTF-8?B?" + base64.b64encode(value.encode()).decode() + "?="


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


async def _post(url: str, body: str, headers: dict[str, str],
                transport: httpx.AsyncBaseTransport | None) -> int:
    """The status only: the body is never read, so a slow body cannot hold a send."""
    token = _SENDING.set(True)
    try:
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S, transport=transport) as c:
            async with c.stream("POST", url, content=body.encode(), headers=headers) as r:
                return r.status_code
    finally:
        _SENDING.reset(token)


async def send(settings: Settings, title: str, body: str, *,
               transport: httpx.AsyncBaseTransport | None = None,
               retry_gap_s: float = RETRY_GAP_S) -> bool:
    """POST one alert to `alerts.url`. True when the address took it. Never raises."""
    url = settings["alerts.url"]
    if not url:
        return False
    headers = {"Title": _header(title), "Tags": "warning",
               "Content-Type": "text/plain; charset=utf-8"}
    public = settings["server.public_urls"]
    if public and isinstance(public[0], str) and public[0].isascii() and public[0].isprintable():
        headers["Click"] = public[0]
    for attempt in range(1, SEND_TRIES + 1):
        try:
            status = await asyncio.wait_for(_post(url, body, headers, transport), SEND_TIMEOUT_S)
            if 200 <= status < 300:
                return True
            why = f"HTTP {status}"
            if status != 429 and status < 500:   # a wrong address: a retry gives the same
                log.warning("alert not sent: %s (a redirect is not followed: use the final "
                            "address)" if 300 <= status < 400 else "alert not sent: %s", why)
                return False
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
    to_send, to_clear, to_backfill = plan(windows, store.alert_states(), threshold)
    for w in to_backfill:
        try:
            store.backfill_alert_reset(w.provider, w.login, w.window, w.resets_at)
        except Exception as e:  # noqa: BLE001
            log.warning("alert record not updated: %s", type(e).__name__)
    for key in to_clear:
        try:
            store.clear_alert(*key)
        except Exception as e:  # noqa: BLE001 - the other windows still go
            log.warning("alert record not cleared: %s", type(e).__name__)
    async def one(w: Window) -> bool:
        if not await send(settings, *message(w), transport=transport, retry_gap_s=retry_gap_s):
            return False
        try:
            store.set_alert(w.provider, w.login, w.window, w.resets_at)
        except Exception as e:  # noqa: BLE001 - the others still finish
            log.warning("alert sent, record not written: %s", type(e).__name__)
        return True
    # At the same time: a slow address never leaves a later window without a try.
    return sum(await asyncio.gather(*(one(w) for w in to_send)))
