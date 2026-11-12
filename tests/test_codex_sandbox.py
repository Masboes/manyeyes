"""A Codex run whose commands all failed is not a review."""
import json
from pathlib import Path

from manyeyes.engine.models import ReviewerSpec
from manyeyes.engine.reviewers.codex import CodexReviewer


def make(settings, tmp_path: Path) -> CodexReviewer:
    return CodexReviewer(ReviewerSpec("codex", 1, "m", "medium"), settings, tmp_path)


def events(*evs):
    return "\n".join(json.dumps(e) for e in evs)


def test_blind_codex_run_has_no_review(settings, tmp_path):
    stdout = events(
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output":
                                             "bwrap: execvp codex-linux-sandbox: No such file or directory"}},
        {"type": "item.completed", "item": {"type": "agent_message",
                                             "text": "I couldn't inspect the change."}})
    parsed = make(settings, tmp_path).parse(stdout, "")
    assert parsed.review == "" and "sandbox helper" in parsed.error_text


def test_normal_codex_run_keeps_its_review(settings, tmp_path):
    stdout = events({"type": "item.completed", "item": {"type": "agent_message", "text": "LGTM"}})
    assert make(settings, tmp_path).parse(stdout, "").review == "LGTM"
