"""Aggregate telemetry for the stats views. Pure functions over rows."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any


def _pct(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, math.ceil(p * len(sorted_vals)) - 1))
    return round(sorted_vals[k], 1)


def summary(values: list[float]) -> dict[str, Any]:
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return {"count": 0, "avg": None, "p50": None, "p90": None, "max": None}
    return {"count": len(vs), "avg": round(sum(vs) / len(vs), 1), "p50": _pct(vs, 0.5),
            "p90": _pct(vs, 0.9), "max": round(vs[-1], 1)}


def reviewer_stats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per kind+model: runs, outcomes, duration of successful runs, average tokens."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["kind"], r["model"])].append(r)
    out = []
    for (kind, model), rs in sorted(groups.items()):
        ok = [r for r in rs if r["outcome"] == "success"]
        reasons = Counter(r["failure_reason"].split(":")[0] for r in rs
                          if r["outcome"] != "success" and r.get("failure_reason"))

        def avg(field: str, ok: list = ok) -> float | None:
            vals = [r[field] for r in ok if r.get(field) is not None]
            return round(sum(vals) / len(vals)) if vals else None

        out.append({
            "kind": kind, "model": model, "runs": len(rs),
            "success_rate": round(len(ok) / len(rs), 3) if rs else None,
            "outcomes": dict(Counter(r["outcome"] for r in rs)),
            "top_failures": reasons.most_common(3),
            "duration_s": summary([r["duration_s"] for r in ok]),
            "avg_input_tokens": avg("input_tokens"),
            "avg_output_tokens": avg("output_tokens"),
            "avg_reasoning_tokens": avg("reasoning_tokens"),
        })
    return out


def job_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [r["finished_at"] - r["started_at"] for r in rows
                 if r.get("finished_at") and r.get("started_at")]
    waits = [r["started_at"] - r["created_at"] for r in rows if r.get("started_at")]
    graded = [r for r in rows if r["status"] in ("done", "failed")]
    done = [r for r in graded if r["status"] == "done"]
    return {
        "total": len(rows),
        "by_status": dict(Counter(r["status"] for r in rows)),
        "by_kind": dict(Counter(r["kind"] for r in rows)),
        "success_rate": round(len(done) / len(graded), 3) if graded else None,
        "partial": sum(1 for r in done if r.get("partial")),
        "run_s": summary(durations),
        "queue_s": summary(waits),
    }
