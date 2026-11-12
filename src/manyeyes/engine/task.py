"""What the swarm works on: a review of a git change, or a free-form spar."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewTask:
    label: str                  # human name, e.g. "owner/repo#12" or a directory name
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    dirty: bool = False         # working tree has uncommitted changes (local runs)
    instructions: str = ""      # optional extra focus from the caller
    # Related repositories for cross-repo checks: (owner/name, ref, sha, path) each.
    context: tuple[tuple[str, str, str, str], ...] = ()

    kind = "review"

    def prompt(self, depth: str = "standard") -> str:
        lines = [
            "You are one of several independent code reviewers. Each reviewer works alone; "
            "a later step merges the reviews. Do not modify any files.",
            "",
            f"Review this change in {self.label}:",
            f"- base: {self.base_ref} ({self.base_sha[:12]})",
            f"- head: {self.head_ref} ({self.head_sha[:12]})",
            f"- see it with: git diff {self.base_sha}...{self.head_sha}",
        ]
        if self.dirty:
            lines.append("- the working tree also has uncommitted changes; include them "
                         "(`git status`, `git diff HEAD`)")
        if depth == "thorough":
            lines += [
                "",
                "This is a review that matters; give it a thorough assessment. Read every "
                "changed file in full, not only the diff hunks. Trace the callers and callees of "
                "every changed function. Check error paths, timeouts and cancellation, "
                "concurrency and ordering, input validation, data migrations, and whether the "
                "tests really exercise the new behavior. Take the time you need: depth matters "
                "more than speed here.",
            ]
        lines += [
            "",
            "Read the surrounding code as needed. Then answer:",
            "- What is the scope? One clear scope, or several?",
            "- Should this change be approved? Is it ready to go live?",
            "- Which concerns or blockers exist?",
            "",
            "Rules: cite `path:line` for every finding; describe the concrete failure "
            "(input or state, and the wrong result); give each finding a severity "
            "(high, medium, low). Report real defects before style. If nothing serious "
            "is wrong, say so plainly.",
        ]
        if self.context:
            lines += ["", "Related repositories are available READ-ONLY, as reference "
                          "(a detached checkout of the commit named; `git log` works, there "
                          "are no other branches):"]
            lines += [f"- {name} at {ref} ({sha[:12]}): {path}"
                      for name, ref, sha, path in self.context]
            lines.append(
                "Use them ONLY to check whether this change breaks something they depend on "
                "or define: a caller, a shared API contract, an event or message shape, a "
                "config key or a database schema. Do not review those repositories "
                "themselves. Every finding must be anchored to the change under review.")
        if self.instructions.strip():
            lines += ["", "Extra focus from the requester:", self.instructions.strip()]
        return "\n".join(lines)


@dataclass(frozen=True)
class SparTask:
    question: str
    label: str = "spar"

    kind = "spar"

    def prompt(self, depth: str = "standard") -> str:
        return self.question.strip()


Task = ReviewTask | SparTask
