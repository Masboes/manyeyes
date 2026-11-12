"""Claude Code headless (`claude -p --output-format json`), plan mode.

The login lives in CLAUDE_CONFIG_DIR (the mounted auth home). That folder can
also hold the user's MCP servers and settings, so the reviewer starts with
`--strict-mcp-config` (no MCP servers) and without session persistence.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..models import Usage
from .base import Parsed, Reviewer


class ClaudeReviewer(Reviewer):
    kind = "claude"

    @property
    def home(self) -> Path:
        return self.run_dir / f"claude-home-{self.spec.instance}"

    @property
    def start_delay(self) -> float:
        return 0.1 * (self.spec.instance - 1)

    def is_logged_in(self) -> bool:
        return (self.auth_home / ".credentials.json").is_file()

    def prepare(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)

    stdin_prompt = True   # the prompt goes on stdin, so it can never become an option

    def argv(self, prompt: str) -> list[str]:
        return [
            self.binary() or self.binary_name, "-p",
            "--model", self.spec.model,
            "--effort", self.spec.effort,
            "--output-format", "json",
            "--permission-mode", "plan",
            "--strict-mcp-config",
            "--no-session-persistence",
            # Only our own user settings: a repository's .claude/settings.json
            # must not be able to add hooks that run commands.
            "--setting-sources", "user",
        ]

    def env(self) -> dict[str, str]:
        env = self.base_env(home=self.home)
        env["CLAUDE_CONFIG_DIR"] = str(self.auth_home)
        env["DISABLE_AUTOUPDATER"] = "1"
        return env

    def rw_paths(self) -> list[Path]:
        return [self.home, self.auth_home]

    def parse(self, stdout: str, stderr: str) -> Parsed:
        out = Parsed()
        obj = None
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                obj = parsed
                break
        if obj is None:
            return out
        text = obj.get("result")
        if obj.get("is_error"):
            out.error_text = str(text or obj.get("subtype") or "claude reported an error")
        elif isinstance(text, str) and text.strip():
            out.review = text
        used = list((obj.get("modelUsage") or {}).keys())
        if used:
            # The main model first (the CLI may also call a small helper model).
            out.model = max(used, key=lambda m: (obj["modelUsage"][m] or {}).get("outputTokens", 0))
        u = obj.get("usage") or {}
        if u:
            cached = u.get("cache_read_input_tokens") or 0
            created = u.get("cache_creation_input_tokens") or 0
            details = u.get("output_tokens_details") or {}
            out.usage = Usage(
                input_tokens=(u.get("input_tokens") or 0) + cached + created,
                cached_input_tokens=cached,
                output_tokens=u.get("output_tokens"),
                reasoning_tokens=details.get("thinking_tokens"),
            )
        return out
