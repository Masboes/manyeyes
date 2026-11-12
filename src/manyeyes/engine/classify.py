"""Turn a finished reviewer process into one outcome and one readable reason.

Structured error text from the CLI's own event stream is checked first; the
raw stderr/stdout tail is the fallback.
"""
from __future__ import annotations

import re

from .models import Outcome

RATE_LIMIT_RE = re.compile(
    r"(rate.?limit|too many requests|usage limit|\b429\b|quota|"
    r"weekly.*limit|daily.*limit|limit.*reached|reached.*limit|"
    r"exceeded.*limit|exhausted.*capacity|"
    r"no capacity available|capacity exhausted|out of capacity|overloaded)",
    re.IGNORECASE,
)

AUTH_RE = re.compile(
    r"(not authenticated|not logged in|please (log ?in|sign ?in|run /login)|"
    r"please run .*login|run .* login|login required|"
    r"missing.*(api.?key|credentials|auth)|invalid.*(api.?key|credentials)|"
    r"token (has )?expired|refresh token|unauthori[sz]ed|\b401\b|authentication_error)",
    re.IGNORECASE,
)

TAIL_CHARS = 4000


def first_line(text: str, pattern: re.Pattern[str] | None = None, limit: int = 200) -> str:
    """The first non-empty line, or the first line matching `pattern`."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if pattern is None or pattern.search(line):
            return line[:limit]
    return ""


def tail(text: str, n: int = TAIL_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= n else "…" + text[-n:]


def classify_failure(error_text: str, stderr: str, stdout: str,
                     exit_code: int | None) -> tuple[Outcome, str]:
    """Classify a reviewer that did not produce a review."""
    for source in (error_text, stderr, stdout):
        if not source:
            continue
        if line := first_line(source, AUTH_RE):
            return Outcome.AUTH_FAILED, f"auth: {line}"
        if line := first_line(source, RATE_LIMIT_RE):
            return Outcome.RATE_LIMITED, f"rate limit: {line}"
    detail = first_line(error_text) or first_line(stderr) or first_line(stdout)
    if exit_code not in (None, 0):
        return Outcome.ERROR, f"exit {exit_code}" + (f": {detail}" if detail else "")
    return Outcome.ERROR, detail or "no review in the output"
