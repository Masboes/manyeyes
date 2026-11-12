"""One OpenAI API call that merges the reviews into a synthesis.

This is the only place that uses the OpenAI API key, and the key comes from
settings (`synth.api_key`), never from OPENAI_API_KEY. A failed synthesis is
never fatal: the reviews are the product, the synthesis is a convenience.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from .models import ReviewerRun, SynthRun, Usage

if TYPE_CHECKING:
    from ..config import Settings

FAMILIES = {"codex": "OpenAI", "claude": "Anthropic"}

_COMMON = """You merge several independent code reviews of the same change into one \
synthesis. Each reviewer worked alone. The synthesis is usually the only part anyone reads.

Reviewers are named `kind#instance`, for example `codex#3` or `claude#1`.
{grouping}

Write every finding in exactly this shape:

### <one-line title> — `path/to/file.py:123` — high
**[codex#1, codex#4]** · {roster_line_example}
<Two or three sentences: what is wrong, the concrete failure, what to do.>

Rules:
- The location is a real `path:line` from the source reviews. If none gave one, write \
`(no location)`. Never invent a path or a line number.
- Severity is high, medium or low. High: incorrect behavior, data loss, or a security \
exposure. Medium: a correctness or maintainability risk that is not visible to users yet. \
Low: style and nits. Severity does not depend on how many reviewers agreed.
- Merge findings that describe the same defect, even when worded differently. List every \
reviewer that raised it.
- Sort findings in each section by severity, high first.
- Keep every distinct issue. That includes one raised by a single reviewer, a risk a \
reviewer could not verify (say what to confirm), and a problem the change leaves \
unfixed elsewhere; never drop one. Place it by agreement like any finding (one reviewer: \
Singleton findings) and keep the severity its reviewers gave. \
Concise means short wording, not fewer findings.
- Every finding must come from at least one review. Do not add your own findings and do \
not upgrade a suggestion into a defect.

`## Disagreements`: reviewers took opposite positions on the same question; state both \
positions and who held them. `## Verdict`: one short paragraph — should this ship, and the \
top one to three things to fix first.

Omit empty sections. Be concise. No preamble."""

_MULTI = """Reviewers come from different provider families: {families}. Group findings by \
HOW MANY FAMILIES agreed, not by how many reviewers agreed: two families agreeing is a \
stronger signal than many instances of one family. Use these sections, in this order:

## Cross-family consensus
## Single-family consensus
## Singleton findings
## Disagreements
## Verdict"""

_SINGLE = """All reviewers come from one model family. Group findings by HOW MANY \
REVIEWERS agreed. Use these sections, in this order:

## Strong agreement (3 or more reviewers)
## Some agreement (2 reviewers)
## Singleton findings
## Disagreements
## Verdict"""


SPAR = """You merge several independent answers to the same question into one synthesis. \
Each respondent worked alone. The synthesis is usually the only part anyone reads.

Respondents are named `kind#instance`, for example `codex#3` or `claude#1`.

Use these sections, in this order, and omit empty ones:

## Consensus
Points most respondents made. Name who made each point, for example **[codex#1, codex#4]**.
## Distinct positions
Points only one or two respondents made that are worth weighing. Name who.
## Disagreements
Questions where respondents took opposite positions: state both and who held them.
## Bottom line
One short paragraph: the answer the evidence supports best, and its main uncertainty.

Every point must come from at least one answer. Do not add your own. Be concise. No preamble."""


def system_prompt(runs: list[ReviewerRun], kind: str = "review") -> str:
    if kind == "spar":
        return SPAR
    kinds = sorted({r.spec.kind for r in runs})
    if len(kinds) > 1:
        grouping = _MULTI.format(families=", ".join(
            f"{k} ({FAMILIES.get(k, k)})" for k in kinds))
        example = "2 families · 3 of 10 reviewers"
    else:
        grouping = _SINGLE
        example = "2 of 10 reviewers"
    return _COMMON.format(grouping=grouping, roster_line_example=example)


def user_prompt(prompt: str, runs: list[ReviewerRun]) -> str:
    ok = [r for r in runs if r.ok]
    parts = [f"Task given to every reviewer:\n---\n{prompt}\n---\n",
             f"Reviews from {len(ok)} of {len(runs)} reviewers:\n---"]
    for r in runs:
        if r.ok:
            parts.append(f"## {r.spec.name} ({r.spec.model}, {r.spec.effort})\n{r.review}")
        else:
            parts.append(f"## {r.spec.name} (no review: {r.outcome})")
    parts.append("---")
    return "\n\n".join(parts)


async def synthesize(prompt: str, runs: list[ReviewerRun], settings: Settings,
                     kind: str = "review") -> SynthRun:
    model = settings["synth.model"]
    ok = [r for r in runs if r.ok]
    if not settings["synth.enabled"]:
        return SynthRun(model, "skipped", failure_detail="synthesis disabled")
    if len(ok) < 2:
        return SynthRun(model, "skipped",
                        failure_detail=f"{len(ok)} successful review(s); synthesis needs 2")
    key = settings["synth.api_key"]
    if not key:
        return SynthRun(model, "skipped",
                        failure_detail="no synthesis key (set MANYEYES_SYNTH_OPENAI_API_KEY)")
    try:
        from openai import AsyncOpenAI
    except ImportError as e:  # pragma: no cover
        return SynthRun(model, "error", failure_detail=f"openai package missing: {e}")

    timeout = float(settings["limits.synth_timeout_s"])
    client = AsyncOpenAI(api_key=key, timeout=timeout, max_retries=1)
    start = time.monotonic()
    try:
        resp = await asyncio.wait_for(client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt(runs, kind)},
                      {"role": "user", "content": user_prompt(prompt, runs)}],
        ), timeout=timeout)
    except TimeoutError:
        return SynthRun(model, "timeout", duration_s=round(time.monotonic() - start, 1),
                        failure_detail=f"synthesis timed out after {int(timeout)}s")
    except Exception as e:  # noqa: BLE001 - never fatal
        # Never echo request details: the SDK error can carry headers.
        return SynthRun(model, "error", duration_s=round(time.monotonic() - start, 1),
                        failure_detail=f"OpenAI API error: {type(e).__name__}: "
                                       f"{str(e)[:300]}")
    finally:
        await client.close()
    elapsed = round(time.monotonic() - start, 1)
    text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    u = getattr(resp, "usage", None)
    usage = Usage()
    if u is not None:
        details = getattr(u, "completion_tokens_details", None)
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None)
        usage = Usage(input_tokens=u.prompt_tokens, cached_input_tokens=cached,
                      output_tokens=u.completion_tokens,
                      reasoning_tokens=getattr(details, "reasoning_tokens", None))
    if not text:
        return SynthRun(model, "error", duration_s=elapsed, usage=usage,
                        failure_detail="the model returned an empty synthesis")
    return SynthRun(model, "success", text=text, duration_s=elapsed, usage=usage)
