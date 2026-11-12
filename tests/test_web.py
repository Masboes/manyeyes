import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).parent.parent / "src" / "manyeyes" / "server" / "web" / "app.js"


def test_app_js_has_no_duplicate_top_level_functions():
    # The browser loads app.js as a module: a duplicate declaration is a SyntaxError
    # that stops the whole UI, while a plain-script parse accepts it.
    names = re.findall(r"^(?:async )?function (\w+)\(", APP.read_text(), re.M)
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate top-level functions: {sorted(dupes)}"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_app_js_parses_as_a_module(tmp_path):
    mod = tmp_path / "app.mjs"
    mod.write_text(APP.read_text())
    r = subprocess.run(["node", "--check", str(mod)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
