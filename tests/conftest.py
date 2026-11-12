from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from manyeyes.config import Settings, load

FAKES = Path(__file__).parent / "fakes"


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """A directory with an executable `codex` that runs the fake."""
    d = tmp_path / "bin"
    d.mkdir()
    exe = d / "codex"
    # System Python when present: the jail hides virtualenvs outside /usr.
    py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    body = (FAKES / "fake_codex.py").read_text().split("\n", 1)[1]
    exe.write_text(f"#!{py}\n{body}")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return d


@pytest.fixture
def settings(tmp_path: Path, fake_bin: Path) -> Settings:
    data = tmp_path / "data"
    (data / "auth" / "codex").mkdir(parents=True)
    (data / "auth" / "codex" / "auth.json").write_text("{}")
    s = load(env={"MANYEYES_DATA_DIR": str(data), "PATH": os.environ["PATH"]})
    return s.with_layer("flag", {
        "reviewers.codex.bin": str(fake_bin / "codex"),
        "jail.enabled": False,
        "synth.enabled": False,
        "update.check": False,
        "usage.interval_min": 0,      # no network in tests
        "limits.reviewer_timeout_s": 20,
        "limits.straggler_grace_s": 1,
    })


def bwrap_works() -> bool:
    if not shutil.which("bwrap"):
        return False
    import subprocess
    r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-pid", "true"],
                       capture_output=True)
    return r.returncode == 0


needs_bwrap = pytest.mark.skipif(not bwrap_works(), reason="bubblewrap cannot run here")
