"""The review synthesis: anonymous labels in, findings placed by agreement out."""
import asyncio
import json
import random
import types

import pytest

from manyeyes.engine import synth
from manyeyes.engine.models import Outcome, ReviewerRun, ReviewerSpec


def run(kind, i, ok=True, text=None):
    return ReviewerRun(ReviewerSpec(kind, i, f"{kind}-model-x", "high"),
                       Outcome.SUCCESS if ok else Outcome.ERROR, text or f"review by {kind}#{i}")


RUNS = [run("codex", 1), run("codex", 2), run("codex", 3), run("claude", 1),
        run("claude", 2, ok=False)]


def test_labels_hide_the_reviewer_and_model():
    labels = synth.anonymize(RUNS, random.Random(1))
    assert sorted(labels) == ["R1", "R2", "R3", "R4"]          # the failed run gets no label
    prompt = synth.review_user_prompt("review it", RUNS, labels)
    for word in ("codex-model-x", "claude-model-x", "## codex", "## claude", "(high)"):
        assert word not in prompt
    assert "1 more returned no review" in prompt
    assert "never drop one" in synth.system_prompt(RUNS, "review")


def labels_by_name():
    labels = synth.anonymize(RUNS, random.Random(7))
    return labels, {r.spec.name: lab for lab, r in labels.items()}


def test_findings_are_placed_by_agreement():
    labels, lab = labels_by_name()
    data = {"findings": [
        {"title": "Lost update", "location": "a.py:3", "severity": "high",
         "reviewers": [lab["codex#2"], lab["claude#1"]], "text": f"{lab['claude#1']} shows a race."},
        {"title": "Wrong default", "location": "b.py:9", "severity": "medium",
         "reviewers": [lab["codex#1"], lab["codex#3"]], "text": "x"},
        {"title": "Unchecked None", "location": "", "severity": "medium",
         "reviewers": [lab["claude#1"]], "text": "y"},
        {"title": "Typo in log", "location": "c.py:1", "severity": "low",
         "reviewers": [lab["codex#3"]], "text": "z"},
        {"title": "Invented", "location": "d.py:1", "severity": "high", "reviewers": ["R99"]},
    ], "disagreements": [f"{lab['codex#1']} says ship, {lab['claude#1']} says wait."],
        "verdict": f"Fix the race first ({lab['codex#2']})."}
    md = synth.render_review(data, labels)
    sections = [line for line in md.splitlines() if line.startswith("## ")]
    assert sections == ["## Cross-family consensus", "## Single-family consensus",
                        "## Singleton findings", "## Minor notes", "## Disagreements",
                        "## Verdict"]
    assert "### Lost update — `a.py:3` — high\n**[claude#1, codex#2]** · 2 families · 2 of 4" in md
    assert "claude#1 shows a race." in md
    assert "### Unchecked None — (no location) — medium" in md
    assert "- Typo in log — `c.py:1` **[codex#3]**" in md
    assert "Invented" not in md and "R99" not in md
    assert "codex#1 says ship, claude#1 says wait." in md and "(codex#2)" in md


def test_single_family_sections_and_order():
    runs = [run("codex", i) for i in range(1, 5)]
    labels = synth.anonymize(runs, random.Random(3))
    lab = {r.spec.name: k for k, r in labels.items()}
    def who(*ns):
        return [lab[f"codex#{n}"] for n in ns]
    data = {"findings": [
        {"title": "Medium three", "severity": "medium", "reviewers": who(1, 2, 3)},
        {"title": "High three", "severity": "high", "reviewers": who(1, 2, 4)},
        {"title": "Pair", "severity": "weird", "reviewers": who(1, 2)},
    ], "verdict": "ok"}
    md = synth.render_review(data, labels)
    assert md.index("## Strong agreement") < md.index("High three") < md.index("Medium three")
    assert "## Some agreement (2 reviewers)" in md and "Pair — (no location) — medium" in md
    assert "families" not in md


@pytest.mark.parametrize("data", [[], {"findings": "x"}, {"findings": [], "verdict": ""}])
def test_unusable_model_output_is_rejected(data):
    labels = synth.anonymize(RUNS, random.Random(1))
    with pytest.raises(ValueError):
        synth.render_review(data, labels)


def test_synthesize_end_to_end(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

        async def create(self, **req):
            seen.update(req)
            user = req["messages"][1]["content"]
            first = user.split("## ")[1].split("\n")[0]       # some label from the prompt
            body = {"findings": [{"title": "Bug", "location": "x.py:1", "severity": "high",
                                  "reviewers": [first], "text": "t"}], "verdict": "fix it"}
            msg = types.SimpleNamespace(content=json.dumps(body))
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=None)

        async def close(self):
            pass

    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)
    settings = {"synth.model": "m", "synth.enabled": True, "synth.api_key": "k",
                "limits.synth_timeout_s": 30}
    out = asyncio.run(synth.synthesize("review it", RUNS, settings))
    assert out.outcome == "success", out.failure_detail
    assert seen["response_format"] == {"type": "json_object"}
    assert "## Singleton findings" in out.text and "### Bug — `x.py:1` — high" in out.text
    assert "R1" not in out.text and "R2" not in out.text
