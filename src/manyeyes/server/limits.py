"""Per-user limits, so one person cannot use up the shared provider quotas.

Two notions of "admin":
- managing settings: everyone may, while `auth.admins` is empty (a small trusted
  team keeps working as before); once it is set, only those logins may;
- being an admin: only the logins NAMED in `auth.admins`. Admins are exempt from
  the limits, may cancel anyone's job, and see the full audit log and everyone's
  usage. An empty list therefore never switches the limits off.

Usage is charged at admission into an append-only ledger (job, mode, reviewer
runs), so deleting a job does not give quota back, and a later change to the
rosters does not change what was charged.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..engine.roster import build_specs

if TYPE_CHECKING:
    from ..config import Settings
    from ..store.db import Store

HOUR, DAY = 3600, 86400
# Check-and-insert must be atomic, or parallel submissions all see free quota.
ADMIT_LOCK = threading.Lock()
_last_audit: dict[str, float] = {}


def is_admin(settings: Settings, login: str | None) -> bool:
    """Named in auth.admins (an empty list names nobody)."""
    return bool(login) and login.lower() in {a.lower() for a in settings["auth.admins"]}


def can_manage(settings: Settings, login: str | None) -> bool:
    """May change settings: everyone while no admins are named, else admins only."""
    return not settings["auth.admins"] or is_admin(settings, login)


@dataclass
class LimitHit(Exception):
    message: str
    retry_after_s: int | None   # None: waiting will not help

    def __str__(self) -> str:
        return self.message


def roster_size(roster: str, settings: Settings) -> int:
    try:
        return len(build_specs(roster, settings))
    except ValueError:
        return 0


def check(settings: Settings, store: Store, login: str, roster: str, mode: str) -> int:
    """Raise LimitHit when `login` may not start this job now; else return its cost."""
    size = roster_size(roster, settings)
    if is_admin(settings, login):
        return size
    now = time.time()
    day = store.ledger_since(login, now - DAY)            # oldest first
    hour = [e for e in day if e["ts"] >= now - HOUR]

    def wait_for(entries: list[dict], window: int, need_free: int, runs: bool = False) -> int:
        # When will enough of these entries (oldest first) have left the window?
        freed = 0
        for e in entries:
            freed += e["reviewer_runs"] if runs else 1
            if freed >= need_free:
                return max(60, math.ceil(e["ts"] + window - now))
        return 60

    def hit(text: str, wait: int | None) -> LimitHit:
        if wait is None:
            tail = ""
        elif wait > 90 * 60:
            tail = f" The next one is possible in about {round(wait / 3600)} h."
        else:
            tail = f" The next one is possible in about {max(1, wait // 60)} min."
        return LimitHit(f"limit reached: {text}.{tail} Ask an admin if you need more.", wait)

    # Collect every limit this job breaks; report the one that blocks longest.
    hits: list[tuple[str, int | None]] = []
    per_hour = int(settings["limits.user_jobs_per_hour"])
    if per_hour and len(hour) >= per_hour:
        hits.append((f"{per_hour} reviews per hour",
                     wait_for(hour, HOUR, len(hour) - per_hour + 1)))
    per_day = int(settings["limits.user_jobs_per_day"])
    if per_day and len(day) >= per_day:
        hits.append((f"{per_day} reviews per day", wait_for(day, DAY, len(day) - per_day + 1)))
    deep_day = int(settings["limits.user_deep_jobs_per_day"])
    if mode == "deep" and deep_day:
        deep = [e for e in day if e["mode"] == "deep"]
        if len(deep) >= deep_day:
            hits.append((f"{deep_day} deep reviews per day",
                         wait_for(deep, DAY, len(deep) - deep_day + 1)))
    runs_day = int(settings["limits.user_reviewer_runs_per_day"])
    if runs_day:
        used = sum(e["reviewer_runs"] for e in day)
        if size > runs_day:
            hits.append((f"this job needs {size} reviewer runs, more than the daily limit of "
                         f"{runs_day}; use a smaller roster or quick mode", None))
        elif used + size > runs_day:
            hits.append((f"{runs_day} reviewer runs per day ({used} used, this job needs "
                         f"{size})", wait_for(day, DAY, used + size - runs_day, runs=True)))
    if hits:
        never = [h for h in hits if h[1] is None]
        raise hit(*(never[0] if never else max(hits, key=lambda h: h[1])))
    return size


class NotSignedIn(Exception):
    """The caller has no login, so their usage cannot be accounted for."""


def _audit_once(store: Store, login: str, message: str) -> None:
    # One "limit_hit" entry per person per 10 minutes; keep the memo small.
    now = time.time()
    if len(_last_audit) > 1000:
        for k in [k for k, t in _last_audit.items() if now - t > 600]:
            del _last_audit[k]
    if now - _last_audit.get(login.lower(), 0) > 600:
        _last_audit[login.lower()] = now
        store.audit(login, "limit_hit", {"message": message})


def admit(settings: Settings, store: Store, login: str | None, roster: str, mode: str,
          fields: dict) -> dict:
    """Check the limits, then create the job and charge the ledger in one transaction."""
    if not login:
        raise NotSignedIn("no signed-in user: cannot account for this job")
    with ADMIT_LOCK:
        try:
            size = check(settings, store, login, roster, mode)
        except LimitHit as e:
            _audit_once(store, login, e.message)
            raise
        return store.create_job(charge=(login, mode, size), **fields)


def warn_if_unenforced(settings: Settings, log) -> None:
    """Limits without named admins: anyone may change them in Settings."""
    keys = ("user_jobs_per_hour", "user_jobs_per_day", "user_deep_jobs_per_day",
            "user_reviewer_runs_per_day", "user_concurrent_jobs")
    if any(int(settings[f"limits.{k}"]) for k in keys) and not settings["auth.admins"]:
        log.warning("per-person limits are set, but auth.admins is empty: every user can "
                    "change the limits in Settings. Name the admins to enforce them.")
