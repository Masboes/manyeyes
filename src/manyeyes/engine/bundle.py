"""Render a SwarmResult as markdown (people) and a dict (machines)."""
from __future__ import annotations

from typing import Any

from .models import SwarmResult


def fmt_seconds(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m{sec:02d}s"


def fmt_tokens(n: int | None) -> str:
    if n is None:
        return "–"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def header_lines(title: str, result: SwarmResult) -> list[str]:
    ok = len(result.successes)
    counts = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in
                       sorted(result.outcome_counts.items()) if k != "success")
    line = f"**Reviews:** {ok} of {len(result.runs)} succeeded" + (f" ({counts})" if counts else "")
    return [f"# {title}", "", line]


def synthesis_section(result: SwarmResult) -> list[str]:
    s = result.synth
    if s and s.outcome == "success":
        return ["## Synthesis", "", f"_{s.model}, {fmt_seconds(s.duration_s)}_", "", s.text]
    reason = s.failure_detail if s else "not run"
    return ["## Synthesis", "", f"_Synthesis skipped: {reason}_"]


def render_markdown(title: str, result: SwarmResult, *, include_reviews: bool = True) -> str:
    lines = header_lines(title, result) + [""] + synthesis_section(result)
    if include_reviews:
        lines += ["", "## Reviews", ""]
        for r in result.runs:
            u = r.usage
            lines.append(f"### {r.spec.name} — {r.spec.model}/{r.spec.effort} — "
                         f"{fmt_seconds(r.duration_s)} — in {fmt_tokens(u.input_tokens)}, "
                         f"out {fmt_tokens(u.output_tokens)}, "
                         f"reasoning {fmt_tokens(u.reasoning_tokens)}")
            lines.append("")
            if r.ok:
                lines.append(r.review)
            else:
                lines.append(f"**{r.outcome}** — {r.failure_reason}")
            lines.append("")
    elif not (result.synth and result.synth.outcome == "success") and result.successes:
        # No synthesis: the reviews are the only result, so include them.
        return render_markdown(title, result, include_reviews=True)
    return "\n".join(lines).rstrip() + "\n"


def result_dict(result: SwarmResult) -> dict[str, Any]:
    s = result.synth
    return {
        "status": result.status,
        "partial": result.partial,
        "outcomes": result.outcome_counts,
        "synthesis": s.text if s and s.outcome == "success" else None,
        "synthesis_status": s.outcome if s else None,
        "synthesis_note": None if (s and s.outcome == "success") else (s.failure_detail if s else None),
        "reviews": [r.to_dict() for r in result.runs],
    }
