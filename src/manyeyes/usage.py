"""How much of each provider's subscription quota is used.

Both probes cost no tokens:
- Codex: the app server answers `account/rateLimits/read` from the ChatGPT login.
- Claude: `claude -p /usage` is a local command; it prints the plan windows.

The probes run like reviewers: in the jail, with the reviewer environment (no keys).
They must: a reviewer can write to the login folders, so a probe outside the jail
would run whatever a reviewer planted there.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings

PROBE_TIMEOUT_S = 60
log = logging.getLogger("manyeyes.usage")


@dataclass
class Window:
    provider: str             # "codex" or "claude"
    window: str               # "5 hours", "week", "week (Fable)", …
    used_percent: float
    resets_at: float | None   # epoch seconds, when known
    plan: str | None = None
    error: str | None = None  # set when the probe failed; the other fields are empty

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _window_name(minutes: int | None) -> str:
    if not minutes:
        return "window"
    if minutes == 10080:
        return "week"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} days"
    if minutes % 60 == 0:
        return f"{minutes // 60} hours"
    return f"{minutes} min"


def parse_codex(result: dict[str, Any]) -> list[Window]:
    """Parse the `account/rateLimits/read` result."""
    out: list[Window] = []
    by_id = result.get("rateLimitsByLimitId") or {"codex": result.get("rateLimits") or {}}
    for limit_id, rl in by_id.items():
        if not rl:
            continue
        for key in ("primary", "secondary"):
            w = rl.get(key)
            if not w or w.get("usedPercent") is None:
                continue
            name = _window_name(w.get("windowDurationMins"))
            if limit_id != "codex":
                name += f" ({rl.get('limitName') or limit_id})"
            out.append(Window("codex", name, float(w["usedPercent"]), w.get("resetsAt"),
                              rl.get("planType")))
    return out


_CLAUDE_LINE = re.compile(r"^Current (session|week[^:]*):\s*(\d+(?:\.\d+)?)%\s*used"
                          r"(?:\s*·\s*resets\s+(.+?))?\s*$", re.M)
_RESET = re.compile(r"^(?:(?P<mon>[A-Z][a-z]{2}) (?P<day>\d{1,2}),(?: (?P<year>\d{4}),)? )?"
                    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?(?P<ampm>am|pm)"
                    r"(?: \((?P<tz>[^)]+)\))?$")


def _reset_epoch(text: str, now: float) -> float | None:
    """Epoch of a reset such as "Nov 12, 4pm (Europe/Amsterdam)"; None if unclear."""
    m = _RESET.match(text.strip())
    if not m:
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(m["tz"]) if m["tz"] else None
        hour = int(m["h"]) % 12 + (12 if m["ampm"] == "pm" else 0)
        base = datetime.fromtimestamp(now, tz)
        dt = base.replace(hour=hour, minute=int(m["m"] or 0), second=0, microsecond=0)
        if m["mon"]:
            month = datetime.strptime(m["mon"], "%b").month
            dt = dt.replace(year=int(m["year"] or base.year), month=month, day=int(m["day"]))
            if not m["year"] and dt.timestamp() < now - 180 * 86400:   # "Jan 2" in December
                dt = dt.replace(year=dt.year + 1)
        elif dt.timestamp() < now:
            dt += timedelta(days=1)    # wall-clock arithmetic: keeps the local time over DST
        return dt.timestamp()
    except (ValueError, KeyError, OSError, ArithmeticError):   # unknown zone, Feb 29, …
        return None


def parse_claude(text: str, now: float | None = None) -> list[Window]:
    """Parse the text of `claude -p /usage`."""
    now = time.time() if now is None else now
    out = []
    for kind, pct, reset in _CLAUDE_LINE.findall(text):
        name = "5 hours" if kind == "session" else kind.replace("week (all models)", "week")
        out.append(Window("claude", name, float(pct), _reset_epoch(reset, now) if reset else None))
    return out


class ProbeError(Exception):
    """A probe failed; the message is safe to show to every signed-in user."""


async def _run(settings: Settings, kind: str, args: list[str], stdin: bytes,
               until: Callable[[bytes], Any] | None) -> tuple[bytes, Any] | None:
    """Run the `kind` CLI with `args`, jailed and with the reviewer's environment.

    With `until`, stdout is read line by line until `until(line)` returns a value;
    else to the end. The whole process group is killed afterwards, also on timeout.
    None: the CLI is not installed or not logged in.
    """
    from .engine.models import ReviewerSpec
    from .engine.reviewers import ADAPTERS
    from .engine.runner import jailed_command

    run_dir = settings.data_dir / "run" / f"usage-{kind}-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True)
    try:
        rev = ADAPTERS[kind](ReviewerSpec(kind=kind, instance=1, model="default",
                                          effort="low"), settings, run_dir)
        binary = rev.binary()
        if not binary or not rev.is_logged_in():
            return None
        rev.prepare()
        workdir = run_dir / "work"
        workdir.mkdir()
        argv, env = jailed_command(settings, rev, [binary, *args], workdir)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, cwd=str(workdir),
            start_new_session=True)
        try:
            assert proc.stdin and proc.stdout
            proc.stdin.write(stdin)
            await proc.stdin.drain()
            if until is None:
                proc.stdin.close()

            async def read() -> tuple[bytes, Any]:
                if until is None:
                    buf = b""
                    while chunk := await proc.stdout.read(65536):   # to EOF, bounded
                        buf += chunk
                        if len(buf) > 4 << 20:
                            raise ProbeError(f"{kind} printed too much output")
                    return buf, None
                while line := await proc.stdout.readline():
                    got = until(line)
                    if got is not None:
                        return line, got
                raise ProbeError(f"{kind} closed without an answer")

            try:
                return await asyncio.wait_for(read(), PROBE_TIMEOUT_S)
            except TimeoutError:
                raise ProbeError(f"{kind} did not answer within {PROBE_TIMEOUT_S} s") from None
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                pass
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _codex_answer(line: bytes) -> dict[str, Any] | None:
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    # A response to our request 2, not a server request that happens to use id 2.
    if not isinstance(msg, dict) or msg.get("id") != 2 or "method" in msg:
        return None
    if "error" in msg:
        log.warning("codex usage request failed: %s", msg["error"])
        raise ProbeError("codex refused the usage request; see the server log")
    return msg.get("result") or {}


async def _codex(settings: Settings) -> list[Window]:
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "manyeyes", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"}]
    got = await _run(settings, "codex", ["app-server"],
                     "".join(json.dumps(m) + "\n" for m in msgs).encode(), _codex_answer)
    if got is None:
        return []                       # not installed or not logged in
    windows = parse_codex(got[1])
    if not windows:
        raise ProbeError("codex returned no usage windows")
    return windows


async def _claude(settings: Settings) -> list[Window]:
    # /usage is a local command: no model call. Only our own settings, no MCP servers.
    got = await _run(settings, "claude",
                     ["-p", "/usage", "--output-format", "json", "--no-session-persistence",
                      "--strict-mcp-config", "--setting-sources", "user"], b"", None)
    if got is None:
        return []                       # not installed or not logged in
    out = got[0]
    if not out.strip():
        raise ProbeError("claude /usage printed nothing")
    try:
        data = json.loads(out.decode(errors="replace"))
    except ValueError:
        raise ProbeError("claude /usage printed no JSON") from None
    if not isinstance(data, dict) or data.get("is_error"):
        raise ProbeError("claude /usage failed")
    windows = parse_claude(str(data.get("result", "")))
    if not windows:
        raise ProbeError("claude /usage showed no plan windows (is the login a subscription?)")
    return windows


async def probe(settings: Settings) -> list[Window]:
    """Probe every logged-in provider. A failed probe gives one Window with `error`."""
    out: list[Window] = []
    for name, fn in (("codex", _codex), ("claude", _claude)):
        try:
            out += await fn(settings)
        except ProbeError as e:
            out.append(Window(name, "", 0.0, None, error=str(e)[:200]))
        except Exception:  # noqa: BLE001 - details go to the log, not to the API
            log.exception("%s usage probe failed", name)
            out.append(Window(name, "", 0.0, None, error="probe failed; see the server log"))
    return out
