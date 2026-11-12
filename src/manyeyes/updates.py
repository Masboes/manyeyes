"""Is a newer manyeyes release out? Asks GitHub's "latest release" endpoint.

Used by the server (checked twice a day, shown in the web UI) and by
`manyeyes version --check`. Never raises: a failed check just reports no update.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import httpx

from . import __version__

if TYPE_CHECKING:
    from .config import Settings

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[.-]?(dev|a|b|rc)(\d*))?$")


def parse(version: str) -> tuple | None:
    """Sortable key for 1.2.3, 1.2.3rc1, 0.1.0.dev0. Pre-releases sort before the release."""
    m = _VERSION.match(version.strip())
    if not m:
        return None
    major, minor, patch, pre, num = m.groups()
    rank = {"dev": 0, "a": 1, "b": 2, "rc": 3}.get(pre or "", 4)
    return (int(major), int(minor), int(patch), rank, int(num or 0))


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse(candidate), parse(current)
    return bool(a and b and a > b)


@dataclass
class UpdateInfo:
    current: str = __version__
    latest: str | None = None
    update_available: bool = False
    url: str | None = None
    notes: str | None = None
    published_at: str | None = None
    checked_at: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def check(settings: Settings) -> UpdateInfo:
    info = UpdateInfo(checked_at=time.time())
    if not settings["update.check"]:
        info.error = "update checks are off"
        return info
    repo = settings["update.repo"]
    headers = {"Accept": "application/vnd.github+json"}
    if settings["github.token"]:
        headers["Authorization"] = f"Bearer {settings['github.token']}"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"https://api.github.com/repos/{repo}/releases/latest", headers=headers)
    except httpx.HTTPError as e:
        info.error = f"{type(e).__name__}"
        return info
    if r.status_code == 404:
        info.error = "no release published yet"
        return info
    if r.status_code != 200:
        info.error = f"GitHub answered {r.status_code}"
        return info
    data = r.json()
    tag = str(data.get("tag_name") or "")
    info.latest = tag.lstrip("v") or None
    info.url = data.get("html_url")
    info.notes = (data.get("body") or "")[:4000] or None
    info.published_at = data.get("published_at")
    info.update_available = bool(info.latest and is_newer(info.latest))
    return info
