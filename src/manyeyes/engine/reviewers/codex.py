"""Codex (`codex exec --json`), authenticated by a ChatGPT login only.

Each instance gets a private CODEX_HOME with a symlink to the shared
`auth.json`. Without that, N parallel codex processes race on startup (shared
temp extraction dir, shared auth read) and some die with SIGBUS or ENOENT.
A small staggered start covers the auth-file read.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..models import Usage
from .base import Parsed, Reviewer


class CodexReviewer(Reviewer):
    kind = "codex"

    @property
    def home(self) -> Path:
        return self.run_dir / f"codex-home-{self.spec.instance}"

    @property
    def start_delay(self) -> float:
        return 0.2 * (self.spec.instance - 1)

    def is_logged_in(self) -> bool:
        return (self.auth_home / "auth.json").is_file()

    def prepare(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        link = self.home / "auth.json"
        if not link.is_symlink():
            link.symlink_to(self.auth_home / "auth.json")

    # The prompt goes on stdin ("-"): no argv length limit, and it does not
    # show up in the process list.
    stdin_prompt = True

    def argv(self, prompt: str) -> list[str]:
        return [
            self.binary() or self.binary_name, "exec",
            "--json", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", self.settings["reviewers.codex.sandbox"],
            "--model", self.spec.model,
            "-c", f'model_reasoning_effort="{self.spec.effort}"',
            "-",
        ]

    def env(self) -> dict[str, str]:
        env = self.base_env(home=self.home)
        env["CODEX_HOME"] = str(self.home)
        return env

    def rw_paths(self) -> list[Path]:
        # The instance home (temp files) and the shared auth dir (token refresh
        # rewrites auth.json through the symlink).
        return [self.home, self.auth_home]

    def parse(self, stdout: str, stderr: str) -> Parsed:
        out = Parsed()
        errors: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type", "")
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" or etype == "agent_message":
                text = item.get("text") or ev.get("text")
                if text:
                    out.review = text
            usage = ev.get("usage")
            if etype == "turn.completed" and isinstance(usage, dict):
                out.usage = out.usage.add(Usage(
                    input_tokens=usage.get("input_tokens"),
                    cached_input_tokens=usage.get("cached_input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    reasoning_tokens=usage.get("reasoning_output_tokens"),
                ))
            if etype in ("error", "turn.failed") or item.get("type") == "error":
                err = ev.get("error") or item
                msg = (err.get("message") if isinstance(err, dict) else None) \
                    or ev.get("message") or json.dumps(ev)[:500]
                errors.append(str(msg))
        out.error_text = "\n".join(errors)
        return out
