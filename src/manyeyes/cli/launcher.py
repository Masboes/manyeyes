"""Start the same `manyeyes` command inside the Docker image.

Paths inside the container equal paths on the host (repository, data dir), so
git worktree links, printed result paths and the database all line up.
The container runs as the host user, so files it writes stay yours.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Settings

IN_CONTAINER = "MANYEYES_IN_CONTAINER"


def in_container() -> bool:
    return os.environ.get(IN_CONTAINER) == "1"


def _git_paths(cwd: Path) -> list[Path]:
    """Repository root and, for a linked worktree, the shared git dir."""
    paths: list[Path] = []
    for args in (["rev-parse", "--show-toplevel"],
                 ["rev-parse", "--path-format=absolute", "--git-common-dir"]):
        try:
            out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                                 text=True, check=True).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        if out:
            paths.append(Path(out))
    root = paths[0] if paths else None
    out_paths = [p for p in paths if root is None or p == root or root not in p.parents]
    return out_paths


def docker_argv(settings: Settings, args: list[str], *, interactive: bool,
                repo_path: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """The docker argv, and the environment to run it with.

    Secrets travel by name (`-e NAME`), with the value in the docker client's
    environment, so they never appear in the process list. File-based secrets
    are resolved here, on the host, where their files exist.
    """
    from ..config import KEYS
    docker = shutil.which("docker")
    if not docker:
        raise SystemExit("manyeyes: Docker is not installed. Install it, or pass "
                         "--no-container to run with host-installed codex/claude.")
    cwd = Path.cwd()
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    mounts: list[tuple[Path, str]] = [(data_dir, "rw")]
    auth = settings.auth_dir
    if data_dir not in auth.parents and auth != data_dir:
        mounts.append((auth, "rw"))
    if settings.config_file and data_dir not in settings.config_file.parents:
        mounts.append((settings.config_file, "ro"))
    for p in _git_paths((repo_path or cwd).resolve()):
        mounts.append((p, "ro"))
    workdir = cwd if any(cwd == p or p in cwd.parents for p, _ in mounts) else data_dir

    argv = [docker, "run", "--rm", "-i"]
    if interactive and sys.stdin.isatty():
        argv.append("-t")
    argv += [
        "--user", f"{os.getuid()}:{os.getgid()}",
        # bubblewrap (the reviewer jail, and codex's own sandbox) creates user
        # namespaces and mounts; the default seccomp and AppArmor profiles block that.
        "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined",
        # A fresh /proc inside the jail needs the unmasked /proc of the container.
        "--security-opt", "systempaths=unconfined",
        "-e", f"{IN_CONTAINER}=1", "-e", f"MANYEYES_DATA_DIR={data_dir}",
        "-e", "HOME=/tmp",
        "-w", str(workdir),
    ]
    # One limit covers everything: the engine and every reviewer share the
    # container's cgroup. Swap equals memory, so the limit is a real ceiling.
    if settings["container.memory"]:
        argv += ["--memory", settings["container.memory"],
                 "--memory-swap", settings["container.memory"]]
    if settings["container.cpus"]:
        argv += ["--cpus", settings["container.cpus"]]
    if repo_path is not None:
        argv += ["-e", f"MANYEYES_REVIEW_PATH={repo_path.resolve()}"]
    env = dict(os.environ)
    secret_names = {k.env_name for k in KEYS if k.secret}
    for k in os.environ:
        if (k.startswith("MANYEYES_") and k not in (IN_CONTAINER, "MANYEYES_DATA_DIR")
                and not k.endswith("_FILE") and k not in secret_names):
            argv += ["-e", k]
    for key in KEYS:
        if key.secret and settings[key.name]:
            env[key.env_name] = settings[key.name]
            argv += ["-e", key.env_name]
    if settings.config_file:
        argv += ["-e", f"MANYEYES_CONFIG={settings.config_file}"]
    seen: set[Path] = set()
    for path, mode in mounts:
        if path in seen:
            continue
        seen.add(path)
        argv += ["-v", f"{path}:{path}:{mode}"]
    argv += [settings["image"], "manyeyes", *args]
    return argv, env


def run_in_container(settings: Settings, args: list[str], *, interactive: bool = False,
                     repo_path: Path | None = None) -> int:
    argv, env = docker_argv(settings, args, interactive=interactive, repo_path=repo_path)
    try:
        return subprocess.call(argv, env=env)
    except KeyboardInterrupt:
        return 130
