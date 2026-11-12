"""Review modes: how much of the swarm a job gets.

    quick  roster_quick, standard prompt   — while iterating
    full   roster, review.depth            — the normal review (default)
    deep   roster_deep, thorough prompt    — risky changes; several times the tokens
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

MODES = ("quick", "full", "deep")
ROSTER_KEY = {"quick": "roster_quick", "full": "roster", "deep": "roster_deep"}


def check(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    return mode


def roster_for(mode: str, settings: Settings) -> str:
    return settings[ROSTER_KEY[check(mode)]]


def deep_limits(settings: Settings) -> dict[str, int]:
    """Deep reviewers read more: twice the configured time limits."""
    return {"limits.reviewer_timeout_s": 2 * settings["limits.reviewer_timeout_s"],
            "limits.job_timeout_s": 2 * settings["limits.job_timeout_s"]}


def depth_for(mode: str, settings: Settings) -> str:
    return "thorough" if mode == "deep" else ("standard" if mode == "quick"
                                               else settings["review.depth"])
