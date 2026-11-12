"""A Codex run whose commands all failed is not a review."""
import json
from pathlib import Path

from manyeyes.engine.models import ReviewerSpec
from manyeyes.engine.reviewers.codex import CodexReviewer

BROKEN = "bwrap: execvp codex-linux-sandbox: No such file or directory"


def make(settings, tmp_path: Path) -> CodexReviewer:
    return CodexReviewer(ReviewerSpec("codex", 1, "m", "medium"), settings, tmp_path)


def cmd(output, code):
    return {"type": "item.completed", "item": {"type": "command_execution",
                                               "aggregated_output": output, "exit_code": code}}


def say(text):
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def run(settings, tmp_path, *evs):
    return make(settings, tmp_path).parse("\n".join(json.dumps(e) for e in evs), "")


def test_blind_codex_run_has_no_review(settings, tmp_path):
    parsed = run(settings, tmp_path, cmd(BROKEN, 1), cmd(BROKEN, 1),
                 say("I couldn't inspect the change."))
    assert parsed.review == "" and "keep data_dir outside /tmp" in parsed.error_text


def test_other_sandbox_failures_are_blind_too(settings, tmp_path):
    parsed = run(settings, tmp_path,
                 cmd("bwrap: No permissions to create new namespace, likely because", 1),
                 say("I couldn't read the file."))
    assert parsed.review == "" and "No permissions to create new namespace" in parsed.error_text
    assert "/tmp" not in parsed.error_text


def test_marker_in_the_code_under_review_is_harmless(settings, tmp_path):
    # A diff that contains the marker text (like this fix itself) must not hide a review.
    parsed = run(settings, tmp_path, cmd("+SANDBOX = '" + BROKEN + "'", 0),
                 say("The check for " + BROKEN + " looks fine."))
    assert parsed.review.startswith("The check for") and not parsed.error_text


def test_one_failed_command_among_good_ones_keeps_the_review(settings, tmp_path):
    parsed = run(settings, tmp_path, cmd(BROKEN, 1), cmd("diff --git a/x b/x", 0), say("LGTM"))
    assert parsed.review == "LGTM"


def test_runner_reports_a_blind_codex_as_an_error(settings, tmp_path):
    from manyeyes.engine.classify import classify_failure
    from manyeyes.engine.models import Outcome
    parsed = run(settings, tmp_path, cmd(BROKEN, 1), say("I couldn't inspect the change."))
    outcome, reason = classify_failure(parsed.error_text, "", "", 0)
    assert outcome == Outcome.ERROR and "its sandbox failed" in reason


def test_a_nonzero_command_that_ran_keeps_the_review(settings, tmp_path):
    parsed = run(settings, tmp_path, cmd("diff --git a/x b/x\n+bug", 1), cmd(BROKEN, 1),
                 say("Found a bug in x."))
    assert parsed.review == "Found a bug in x."


def test_no_commands_at_all_keeps_the_review(settings, tmp_path):
    assert run(settings, tmp_path, say("Answer from the prompt alone.")).review
