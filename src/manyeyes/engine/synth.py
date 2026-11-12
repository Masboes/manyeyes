"""One OpenAI API call that merges the reviews into a synthesis.

This is the only place that uses the OpenAI API key, and the key comes from
settings (`synth.api_key`), never from OPENAI_API_KEY. A failed synthesis is
never fatal: the reviews are the product, the synthesis is a convenience.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import TYPE_CHECKING

from .models import ReviewerRun, SynthRun, Usage

if TYPE_CHECKING:
    from ..config import Settings

FAMILIES = {"codex": "OpenAI", "claude": "Anthropic"}
SEVERITIES = ("high", "medium", "low")

# Reviews: the model sees anonymous labels (R1, R2, …) in random order and no model
# names, so it cannot favor one vendor's reviewers. It only merges and rates the
# findings, as JSON; the code then places them by agreement (render_review).
REVIEW = """You merge several independent code reviews of the same change. Each reviewer \
worked alone. The merged result is usually the only part anyone reads.

Reviewers are anonymous: R1, R2, and so on. The labels are in random order and say \
nothing about the model behind a review. Judge every review on its content only.

Return one JSON object with exactly these keys:
{"findings": [{"title": "<one line>", "location": "<path/to/file.py:123 or empty>", \
"severity": "high|medium|low", "reviewers": ["R2", "R7"], \
"text": "<two or three sentences: what is wrong, the concrete failure, what to do>"}], \
"disagreements": ["<reviewers took opposite positions on the same question: both \
positions and their labels>"], \
"verdict": "<one short paragraph: should this ship, and the top one to three things to fix first>"}

Rules:
- The location is a real path:line from the source reviews. Give it whenever any \
reviewer of that finding named one; leave it empty only when none did. Never invent a \
path or a line number.
- Severity: high is incorrect behavior, data loss, or a security exposure. Medium is a \
correctness or maintainability risk that is not visible to users yet. Low is style and \
nits. Severity does not depend on how many reviewers agreed.
- Merge findings that describe the same defect, even when worded differently. List every \
reviewer that raised it.
- Keep every distinct issue. That includes one raised by a single reviewer, a risk a \
reviewer could not verify (say what to confirm), and a problem the change leaves \
unfixed elsewhere; never drop one. Keep the severity its reviewers gave. Concise means \
short wording, not fewer findings.
- Every finding must come from at least one review. Do not add your own findings and do \
not upgrade a suggestion into a defect.
- Refer to reviewers only by their labels, one by one (R2, R3, R4), never as a range \
such as R2–R4."""

_LABEL = re.compile(r"\bR(\d{1,3})\b")


def anonymize(runs: list[ReviewerRun], rng: random.Random | None = None) -> dict[str, ReviewerRun]:
    """Label the successful reviews R1..Rn in random order."""
    ok = [r for r in runs if r.ok]
    (rng or random.SystemRandom()).shuffle(ok)
    return {f"R{i}": r for i, r in enumerate(ok, 1)}


def review_user_prompt(prompt: str, runs: list[ReviewerRun], labels: dict[str, ReviewerRun]) -> str:
    failed = len(runs) - len(labels)
    parts = [f"Task given to every reviewer:\n---\n{prompt}\n---\n",
             f"Reviews from {len(labels)} reviewers"
             + (f" ({failed} more returned no review)" if failed else "") + ":\n---"]
    parts += [f"## {label}\n{r.review}" for label, r in labels.items()]
    parts.append("---")
    return "\n\n".join(parts)


def _names(text: str, labels: dict[str, ReviewerRun]) -> str:
    return _LABEL.sub(lambda m: labels[m[0]].spec.name if m[0] in labels else m[0], text)


def render_review(data: dict, labels: dict[str, ReviewerRun]) -> str:
    """Place every finding by agreement, from the model's JSON. Raises ValueError."""
    if not isinstance(data, dict) or not isinstance(data.get("findings", []), list):
        raise ValueError("no findings list")
    total = len(labels)
    kinds = {r.spec.kind for r in labels.values()}
    multi = len(kinds) > 1
    order = ([("Cross-family consensus", lambda f, n: f >= 2),
              ("Single-family consensus", lambda f, n: f == 1 and n >= 2)] if multi else
             [("Strong agreement (3 or more reviewers)", lambda f, n: n >= 3),
              ("Some agreement (2 reviewers)", lambda f, n: n == 2)])
    order.append(("Singleton findings", lambda f, n: n == 1))
    sections: dict[str, list] = {name: [] for name, _ in order}
    minor: list[str] = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        who = []
        for label in item.get("reviewers") or []:
            label = str(label).strip()
            if label in labels and labels[label] not in who:
                who.append(labels[label])
        if not who:
            continue                       # not traceable to a review: never shown
        sev = str(item.get("severity", "")).lower().strip()
        sev = sev if sev in SEVERITIES else "medium"
        title = " ".join(_names(str(item.get("title") or "Untitled finding"), labels).split())
        loc = str(item.get("location") or "").strip().strip("`") or "(no location)"
        loc_md = loc if loc == "(no location)" else f"`{loc}`"
        who.sort(key=lambda r: (r.spec.kind, r.spec.instance))
        cited = ", ".join(r.spec.name for r in who)
        fams = len({r.spec.kind for r in who})
        if len(who) == 1 and sev == "low":
            minor.append(f"- {title} — {loc_md} **[{cited}]**")
            continue
        count = (f"{fams} famil{'ies' if fams > 1 else 'y'} · " if multi else "") \
            + f"{len(who)} of {total} reviewers"
        body = _names(str(item.get("text") or "").strip(), labels)
        block = f"### {title} — {loc_md} — {sev}\n**[{cited}]** · {count}\n{body}".rstrip()
        name = next(n for n, test in order if test(fams, len(who)))
        sections[name].append((SEVERITIES.index(sev), -len(who), block))
    out = []
    for name, _ in order:
        if sections[name]:
            out.append(f"## {name}\n\n" + "\n\n".join(b for *_, b in sorted(
                sections[name], key=lambda x: x[:2])))
    if minor:
        out.append("## Minor notes\n\nLow-severity points that one reviewer raised.\n\n"
                   + "\n".join(minor))
    dis = [_names(str(d).strip(), labels) for d in data.get("disagreements") or [] if str(d).strip()]
    if dis:
        out.append("## Disagreements\n\n" + "\n".join(f"- {d}" for d in dis))
    verdict = _names(str(data.get("verdict") or "").strip(), labels)
    if verdict:
        out.append(f"## Verdict\n\n{verdict}")
    if not out:
        raise ValueError("empty synthesis")
    return "\n\n".join(out)


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
    return SPAR if kind == "spar" else REVIEW


def user_prompt(prompt: str, runs: list[ReviewerRun]) -> str:
    """The spar prompt: respondents keep their names (no ranking by family there)."""
    ok = [r for r in runs if r.ok]
    parts = [f"Task given to every reviewer:\n---\n{prompt}\n---\n",
             f"Answers from {len(ok)} of {len(runs)} respondents:\n---"]
    for r in runs:
        if r.ok:
            parts.append(f"## {r.spec.name}\n{r.review}")
        else:
            parts.append(f"## {r.spec.name} (no answer: {r.outcome})")
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
    labels = anonymize(runs)
    client = AsyncOpenAI(api_key=key, timeout=timeout, max_retries=1)
    start = time.monotonic()
    try:
        if kind == "spar":
            request = {"messages": [{"role": "system", "content": SPAR},
                                    {"role": "user", "content": user_prompt(prompt, runs)}]}
        else:
            request = {"messages": [{"role": "system", "content": REVIEW},
                                    {"role": "user",
                                     "content": review_user_prompt(prompt, runs, labels)}],
                       "response_format": {"type": "json_object"}}
        resp = await asyncio.wait_for(client.chat.completions.create(
            model=model, **request), timeout=timeout)
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
    if kind != "spar":
        try:
            text = render_review(json.loads(text), labels)
        except ValueError as e:     # json.JSONDecodeError is a ValueError
            return SynthRun(model, "error", duration_s=elapsed, usage=usage,
                            failure_detail=f"the model returned an unusable synthesis: {e}"[:300])
    return SynthRun(model, "success", text=text, duration_s=elapsed, usage=usage)
