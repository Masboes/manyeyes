"""Roster syntax.

    codex:10                     ten Codex reviewers with the configured model
    codex:8,claude:2             mixed
    codex:4@gpt-6-astra/high     model and effort override for that entry
    claude                       count defaults to 1
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .models import ReviewerSpec

if TYPE_CHECKING:
    from ..config import Settings

KINDS = ("codex", "claude")
MAX_REVIEWERS = 32

_ENTRY = re.compile(
    r"^(?P<kind>[a-z]+)(?::(?P<count>\d+))?(?:@(?P<model>[A-Za-z0-9._\-]+))?"
    r"(?:/(?P<effort>[a-z]+))?$"
)


class RosterError(ValueError):
    pass


def parse_roster(spec: str) -> list[tuple[str, int, str | None, str | None]]:
    """Parse a roster string into (kind, count, model, effort) entries."""
    entries = []
    total = 0
    for raw in (spec or "").split(","):
        piece = raw.strip()
        if not piece:
            continue
        m = _ENTRY.match(piece)
        if not m:
            raise RosterError(f"cannot read roster entry {piece!r} (try 'codex:10')")
        kind = m["kind"]
        if kind not in KINDS:
            raise RosterError(f"unknown reviewer {kind!r}; choose from {', '.join(KINDS)}")
        count = int(m["count"] or 1)
        if count < 1:
            raise RosterError(f"count in {piece!r} must be at least 1")
        total += count
        entries.append((kind, count, m["model"], m["effort"]))
    if not entries:
        raise RosterError("the roster is empty")
    if total > MAX_REVIEWERS:
        raise RosterError(f"the roster has {total} reviewers; the maximum is {MAX_REVIEWERS}")
    return entries


def build_specs(spec: str, settings: Settings) -> list[ReviewerSpec]:
    """Expand a roster into one ReviewerSpec per reviewer, numbered per kind."""
    out: list[ReviewerSpec] = []
    seen: dict[str, int] = {}
    for kind, count, model, effort in parse_roster(spec):
        for _ in range(count):
            seen[kind] = seen.get(kind, 0) + 1
            out.append(ReviewerSpec(
                kind=kind,
                instance=seen[kind],
                model=model or settings[f"reviewers.{kind}.model"],
                effort=effort or settings[f"reviewers.{kind}.effort"],
            ))
    return out


def families(specs: list[ReviewerSpec]) -> list[str]:
    return sorted({s.kind for s in specs})
