"""Reviewer logins: more than one per provider, and which one a reviewer gets.

The first login of a provider lives in `<auth_dir>/<kind>` and is called
"default". Another login lives in `<auth_dir>/<kind>@<name>`. Each folder is a
complete login home; a reviewer gets read-write access to its own login only.

A reviewer gets the login with the most quota left (from the newest usage
reading) and the fewest reviewers on it now. A login that hits a rate limit
rests until its plan window resets; an admin can switch a login off.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings
    from .store.db import Store

DEFAULT = "default"
KINDS = ("codex", "claude")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")   # use fullmatch: no trailing newline
FULL_PCT = 98.0         # a plan window this full blocks the login
# The file that proves a login home is logged in.
LOGIN_FILES = {"codex": "auth.json", "claude": ".credentials.json"}
UNKNOWN_USED = 50.0     # score of a login without a recent usage reading
INFLIGHT_WEIGHT = 10.0  # one running reviewer counts like 10 percentage points used


class LoginError(ValueError):
    pass


def check_name(name: str) -> str:
    if name != DEFAULT and not NAME_RE.fullmatch(name or ""):
        raise LoginError("a login name is 1-32 characters: a-z, 0-9 and '-', "
                         "starting with a letter or digit")
    return name


def login_home(auth_dir: Path, kind: str, name: str) -> Path:
    if kind not in KINDS:
        raise LoginError(f"unknown reviewer kind: {kind!r}")
    check_name(name)
    return auth_dir / (kind if name == DEFAULT else f"{kind}@{name}")


@dataclass(frozen=True)
class Login:
    kind: str
    name: str
    home: Path

    @property
    def logged_in(self) -> bool:
        return (self.home / LOGIN_FILES[self.kind]).is_file()

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.name


def list_logins(auth_dir: Path, kind: str | None = None) -> list[Login]:
    """The login folders that exist, "default" first, then by name."""
    out: list[Login] = []
    for k in KINDS if kind is None else (kind,):
        named = []
        if auth_dir.is_dir():
            for p in auth_dir.iterdir():
                if p.is_dir() and p.name.startswith(f"{k}@"):
                    name = p.name[len(k) + 1:]
                    # "default" is auth/<kind> itself, never auth/<kind>@default.
                    if name != DEFAULT and NAME_RE.fullmatch(name):
                        named.append(Login(k, name, p))
        home = auth_dir / k
        if home.is_dir():
            out.append(Login(k, DEFAULT, home))
        out += sorted(named, key=lambda x: x.name)
    return out


class LoginPool:
    """Chooses a login for each reviewer. One pool per process (server or CLI run)."""

    def __init__(self, settings: Settings, store: Store | None) -> None:
        self.settings = settings
        self.store = store
        self.inflight: Counter[tuple[str, str]] = Counter()
        self._rest: dict[tuple[str, str], float] = {}   # without a store

    # -- state ------------------------------------------------------------

    def _states(self) -> dict[tuple[str, str], dict[str, Any]]:
        return self.store.login_states() if self.store else {}

    def resting_until(self, login: Login, states: dict | None = None) -> float | None:
        st = (states if states is not None else self._states()).get(login.key) or {}
        until = st.get("resting_until") or self._rest.get(login.key)
        return until if until and until > time.time() else None

    def _used(self, login: Login, settings: Settings) -> float:
        """The fullest plan window of the newest recent reading, in percent."""
        if not self.store:
            return UNKNOWN_USED
        interval = int(settings["usage.interval_min"]) or 30
        rows = self.store.latest_usage(login.kind, login.name,
                                       time.time() - max(3 * interval * 60, 3600))
        used = [r["used_percent"] for r in rows if r["used_percent"] is not None]
        return max(used) if used else UNKNOWN_USED

    # -- choice -----------------------------------------------------------

    def pick(self, kind: str, exclude: tuple[str, ...] = (),
             settings: Settings | None = None) -> Login | None:
        """The login for one reviewer, or None when no login of `kind` can run.

        With every usable login resting, the one whose rest ends first: the rest
        is an estimate, and a reviewer that hits the limit again fails fast.
        """
        s = settings or self.settings
        states = self._states()
        usable = [x for x in list_logins(s.auth_dir, kind)
                  if x.logged_in and x.name not in exclude
                  and (states.get(x.key) or {}).get("enabled", 1)]
        awake = [x for x in usable if self.resting_until(x, states) is None]
        if awake:
            return min(awake, key=lambda x: (self._used(x, s) + INFLIGHT_WEIGHT * self.inflight[x.key],
                                             x.name != DEFAULT, x.name))
        if usable and not exclude:
            return min(usable, key=lambda x: self.resting_until(x, states) or 0)
        return None

    def why_none(self, kind: str, settings: Settings | None = None) -> str:
        s = settings or self.settings
        logins = list_logins(s.auth_dir, kind)
        if not any(x.logged_in for x in logins):
            return f"auth: no {kind} login in {s.auth_dir}"
        return f"auth: every {kind} login is switched off (see the usage panel)"

    def acquire(self, login: Login) -> None:
        self.inflight[login.key] += 1

    def release(self, login: Login) -> None:
        self.inflight[login.key] -= 1
        if self.inflight[login.key] <= 0:
            del self.inflight[login.key]

    def rest(self, login: Login, settings: Settings | None = None) -> float:
        """Rest a login after a plan limit. Until the full window (FULL_PCT or
        more) resets, when a reading shows one; else for limits.login_rest_s. A
        rest never gets shorter. Returns the end time."""
        s = settings or self.settings
        now = time.time()
        until = now + float(s["limits.login_rest_s"])
        if self.store:
            rows = self.store.latest_usage(login.kind, login.name, now - 6 * 3600)
            full = [r for r in rows if (r["used_percent"] or 0) >= FULL_PCT
                    and r["resets_at"] and r["resets_at"] > now]
            if full:
                until = max(r["resets_at"] for r in full)
            until = max(until, self.resting_until(login) or 0)
            self.store.set_login(login.kind, login.name, resting_until=until,
                                 by="manyeyes (rate limit)")
        else:
            until = max(until, self._rest.get(login.key, 0))
            self._rest[login.key] = until
        return until

    def has_other(self, kind: str, tried: tuple[str, ...],
                  settings: Settings | None = None) -> bool:
        s = settings or self.settings
        states = self._states()
        return any(x.logged_in and x.name not in tried
                   and (states.get(x.key) or {}).get("enabled", 1)
                   and self.resting_until(x, states) is None
                   for x in list_logins(s.auth_dir, kind))
