"""Alerts to a person: a plan window of a reviewer login is nearly full.

The server reads the plan usage every `usage.interval_min`. When a window of a
day or longer passes `alerts.usage_percent`, one alert goes to `alerts.url`:
plain text with a `Title` header, the form that ntfy takes.

A window alerts once per crossing: the previous reading of the same window must
be below the line. The readings are in the database, so a restart sends nothing
again. Windows shorter than a day do not alert: they reset soon, and a login
that hits one rests by itself (logins.py).

Sending never raises and never blocks a reading for long. The address is a
secret (anyone with an ntfy topic can read it), so no log line names it.
"""
from __future__ import annotations

import asyncio
import logging
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
LOOKBACK_S = 2 * 86400   # an older previous reading counts as none


def long_window(name: str) -> bool:
    """True for "week", "week (Fable)", "7 days"; False for "5 hours"."""
    return name.startswith("week") or name.endswith(" days")


def crossings(windows: list[Window], previous: dict[tuple[str, str, str], float],
              threshold: float) -> list[Window]:
    """The windows of a new reading that reached `threshold` since the previous one.

    `previous` maps (provider, login, window) to the used percent of the
    previous successful reading. A window without a previous reading alerts
    when it is at or above the line.
    """
    if threshold <= 0:
        return []
    out = []
    for w in windows:
        if w.error or w.used_percent is None or not long_window(w.window):
            continue
        before = previous.get((w.provider, w.login, w.window))
        if w.used_percent >= threshold and (before is None or before < threshold):
            out.append(w)
    return out


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
    """POST one alert to `alerts.url`. True when the address took it."""
    url = settings["alerts.url"]
    if not url:
        return False
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
        except httpx.HTTPError as e:
            why = type(e).__name__
        log.warning("alert not sent (try %d of %d): %s", attempt, SEND_TRIES, why)
        if attempt < SEND_TRIES:
            await asyncio.sleep(retry_gap_s)
    return False


def previous_readings(store: Store, windows: list[Window],
                      now: float | None = None) -> dict[tuple[str, str, str], float]:
    """The newest successful reading before this one, per login and window."""
    since = (time.time() if now is None else now) - LOOKBACK_S
    out: dict[tuple[str, str, str], float] = {}
    for provider, login in {(w.provider, w.login) for w in windows}:
        for row in store.latest_usage(provider, login, since):
            if row["used_percent"] is not None:
                out[(provider, login, row["window"])] = row["used_percent"]
    return out


async def usage_alerts(store: Store, settings: Settings, windows: list[Window], *,
                       transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Call before the new reading goes into the store. Returns the alerts sent."""
    if not settings["alerts.url"] or settings["alerts.usage_percent"] <= 0:
        return 0
    hits = crossings(windows, previous_readings(store, windows),
                     float(settings["alerts.usage_percent"]))
    sent = 0
    for w in hits:
        if await send(settings, *message(w), transport=transport):
            sent += 1
    return sent
