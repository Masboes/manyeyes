"""Reviewer adapter interface.

An adapter knows how to call one CLI headless: argv, environment, which paths
it needs inside the jail, and how to read its output. Running, timing and
killing the process is the runner's job, so every CLI gets the same lifecycle.
"""
from __future__ import annotations

import os
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import ReviewerSpec, Usage

if TYPE_CHECKING:
    from ...config import Settings

SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,79}$")

# Environment a reviewer may receive. Everything else (API keys, tokens, the
# server's own settings) is dropped, jail or no jail.
PASS_THROUGH = ("LANG", "LC_ALL", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
                "NODE_EXTRA_CA_CERTS")


@dataclass
class Parsed:
    review: str = ""
    usage: Usage = field(default_factory=Usage)
    error_text: str = ""   # error messages the CLI reported in its own output
    model: str = ""        # the model that actually answered, when the CLI reports it


class Reviewer(ABC):
    kind: str = ""
    stdin_prompt = False   # True: the runner writes the prompt to stdin
    start_delay = 0.0

    def __init__(self, spec: ReviewerSpec, settings: Settings, run_dir: Path) -> None:
        if not SAFE_VALUE.match(spec.model) or not SAFE_VALUE.match(spec.effort):
            raise ValueError(f"unsafe model or effort value: {spec.model!r} {spec.effort!r}")
        self.spec = spec
        self.settings = settings
        self.run_dir = run_dir      # per-job scratch, private to the engine

    @property
    def binary_name(self) -> str:
        return self.settings[f"reviewers.{self.kind}.bin"]

    def binary(self) -> str | None:
        return shutil.which(self.binary_name)

    @property
    def auth_home(self) -> Path:
        return self.settings.auth_dir / self.kind

    def base_env(self, home: Path) -> dict[str, str]:
        b = self.binary()
        path_dirs = [str(Path(b).parent)] if b else []
        node = shutil.which("node")
        if node:
            path_dirs.append(str(Path(node).parent))
        path_dirs += ["/usr/local/bin", "/usr/bin", "/bin"]
        env = {k: os.environ[k] for k in PASS_THROUGH if k in os.environ}
        env.update({
            "PATH": ":".join(dict.fromkeys(path_dirs)),
            "HOME": str(home),
            "TERM": "dumb",
            "NO_COLOR": "1",
            # Never prompt for credentials, never fetch missing objects: inside
            # the jail git has no token, so fail fast instead of hanging.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "LANG": env.get("LANG", "C.UTF-8"),
        })
        return env

    @abstractmethod
    def prepare(self) -> None:
        """Create per-instance state (for example a private CLI home)."""

    @abstractmethod
    def argv(self, prompt: str) -> list[str]: ...

    @abstractmethod
    def env(self) -> dict[str, str]: ...

    @abstractmethod
    def rw_paths(self) -> list[Path]:
        """Paths the reviewer writes to (its login home)."""

    @abstractmethod
    def parse(self, stdout: str, stderr: str) -> Parsed: ...

    def is_logged_in(self) -> bool:
        return self.auth_home.is_dir() and any(self.auth_home.iterdir())
