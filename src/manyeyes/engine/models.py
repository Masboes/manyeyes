"""Plain data types the engine produces. No I/O here."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    STRAGGLER_KILLED = "straggler_killed"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ReviewerSpec:
    kind: str          # "codex" | "claude"
    instance: int      # 1-based within the kind
    model: str
    effort: str

    @property
    def name(self) -> str:
        return f"{self.kind}#{self.instance}"


@dataclass
class Usage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None

    def add(self, other: Usage) -> Usage:
        def s(a: int | None, b: int | None) -> int | None:
            return None if a is None and b is None else (a or 0) + (b or 0)
        return Usage(s(self.input_tokens, other.input_tokens),
                     s(self.cached_input_tokens, other.cached_input_tokens),
                     s(self.output_tokens, other.output_tokens),
                     s(self.reasoning_tokens, other.reasoning_tokens))


@dataclass
class ReviewerRun:
    spec: ReviewerSpec
    outcome: Outcome
    review: str = ""
    failure_reason: str = ""     # one short line, for humans
    failure_detail: str = ""     # stderr/stdout tail, for debugging
    exit_code: int | None = None
    model_resolved: str = ""     # the model that actually answered, when the CLI says
    started_at: float = 0.0      # epoch seconds
    finished_at: float = 0.0
    usage: Usage = field(default_factory=Usage)

    @property
    def duration_s(self) -> float:
        return round(max(0.0, self.finished_at - self.started_at), 1)

    @property
    def ok(self) -> bool:
        return self.outcome == Outcome.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reviewer"] = self.spec.name
        d["outcome"] = str(self.outcome)
        d["duration_s"] = self.duration_s
        return d


@dataclass
class SynthRun:
    model: str
    outcome: str                 # success | skipped | error | timeout
    text: str = ""
    failure_detail: str = ""
    duration_s: float = 0.0
    usage: Usage = field(default_factory=Usage)


@dataclass
class SwarmResult:
    runs: list[ReviewerRun]
    synth: SynthRun | None
    prompt: str

    @property
    def successes(self) -> list[ReviewerRun]:
        return [r for r in self.runs if r.ok]

    @property
    def status(self) -> str:
        if any(r.outcome == Outcome.CANCELLED for r in self.runs) and not self.successes:
            return "cancelled"
        return "done" if self.successes else "failed"

    @property
    def partial(self) -> bool:
        return any(not r.ok for r in self.runs)

    @property
    def outcome_counts(self) -> dict[str, int]:
        return dict(Counter(str(r.outcome) for r in self.runs))

    def failure_summary(self) -> str:
        """One line naming the dominant failure, for a job with no success."""
        if self.successes or not self.runs:
            return ""
        counts = Counter(r.outcome for r in self.runs)
        outcome, n = counts.most_common(1)[0]
        sample = next(r.failure_reason for r in self.runs if r.outcome == outcome)
        label = str(outcome).replace("_", " ")
        return f"all {len(self.runs)} reviewers failed ({n} {label}): {sample}"
