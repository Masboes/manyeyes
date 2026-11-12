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

# How a command fails when Codex's own sandbox cannot start, for example
# "bwrap: execvp codex-linux-sandbox: No such file or directory" (Codex 0.167+ does not
# create that helper when its home is under /tmp) or "bwrap: No permissions to create
# new namespace".
SANDBOX_BROKEN = "bwrap: "


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
        cmd_ok, blind_output = 0, ""
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
            if etype == "item.completed" and item.get("type") == "command_execution":
                output = str(item.get("aggregated_output") or "").lstrip()
                if item.get("exit_code") not in (0, None) and output.startswith(SANDBOX_BROKEN):
                    blind_output = blind_output or output.splitlines()[0][:200]
                else:          # it ran, whatever its exit code (git diff --exit-code: 1)
                    cmd_ok += 1
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
        if blind_output and not cmd_ok:
            # Not one command ran, so Codex never saw the code; its "review" only
            # says so. Never count that as a review.
            out.review = ""
            hint = (" Its home is probably under /tmp: keep data_dir outside /tmp."
                    if "codex-linux-sandbox" in blind_output else "")
            errors.insert(0, f"codex could not run any command, its sandbox failed: "
                             f"{blind_output}.{hint}")
        out.error_text = "\n".join(errors)
        return out
