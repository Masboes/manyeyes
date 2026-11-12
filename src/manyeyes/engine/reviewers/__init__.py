from .base import Reviewer
from .claude import ClaudeReviewer
from .codex import CodexReviewer

ADAPTERS: dict[str, type[Reviewer]] = {
    "codex": CodexReviewer,
    "claude": ClaudeReviewer,
}

__all__ = ["ADAPTERS", "ClaudeReviewer", "CodexReviewer", "Reviewer"]
