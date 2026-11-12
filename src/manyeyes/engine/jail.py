"""A bubblewrap jail around each reviewer process.

The reviewer's own sandbox (codex read-only, claude plan mode) limits what the
model asks to do. The jail limits what the process can see at all: a fresh PID
namespace, a clean environment, read-only system directories, the code under
review, and its own login home. Nothing else from the data directory (the
database, secrets, other jobs) exists inside it.

Paths inside the jail equal paths outside, so git worktree links and symlinks
keep working without translation.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ETC_FILES = (
    "/etc/ssl", "/etc/ca-certificates", "/etc/pki", "/etc/passwd", "/etc/group",
    "/etc/hosts", "/etc/resolv.conf", "/etc/nsswitch.conf", "/etc/gitconfig",
    "/etc/alternatives", "/etc/ld.so.cache", "/etc/localtime", "/etc/host.conf",
)
TOP_LEVEL = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")


class JailError(RuntimeError):
    pass


@dataclass
class JailSpec:
    workdir: Path
    env: dict[str, str]
    ro: list[Path] = field(default_factory=list)       # extra read-only paths
    rw: list[Path] = field(default_factory=list)       # read-write paths
    protect: list[Path] = field(default_factory=list)  # must stay invisible (the data dir)
    unshare_pid: bool = True                           # tests turn this off on purpose


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def _exists(p: str | Path) -> bool:
    return os.path.lexists(p)


def exposes(path: Path, secret_dir: Path) -> bool:
    """True when binding `path` would make `secret_dir` visible."""
    path, secret_dir = path.resolve(), secret_dir.resolve()
    return path == secret_dir or path in secret_dir.parents


def build_argv(spec: JailSpec) -> list[str]:
    """bwrap argv that runs a command in the jail. Append the command."""
    bwrap = bwrap_path()
    if not bwrap:
        raise JailError("bubblewrap (bwrap) is not installed")
    a = [bwrap, "--die-with-parent", "--new-session", "--unshare-ipc", "--unshare-uts",
         "--clearenv"]
    if spec.unshare_pid:
        a.append("--unshare-pid")
    a += ["--ro-bind", "/usr", "/usr"]
    for top in TOP_LEVEL:
        if os.path.islink(top):
            a += ["--symlink", os.readlink(top), top]
        elif os.path.isdir(top):
            a += ["--ro-bind", top, top]
    if os.path.isdir("/opt"):
        a += ["--ro-bind", "/opt", "/opt"]
    for f in ETC_FILES:
        if _exists(f):
            # Bind the resolved target at the original path, so a symlink
            # into /run (resolv.conf) still works.
            a += ["--ro-bind", os.path.realpath(f), f]
    a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for p in spec.ro:
        if _exists(p):
            a += ["--ro-bind", str(p), str(p)]
    for p in spec.rw:
        if _exists(p):
            a += ["--bind", str(p), str(p)]
    a += ["--ro-bind", str(spec.workdir), str(spec.workdir)]
    # Enforce the invariant here, where the mounts are built: no source of any
    # bind (implicit system dirs included) may contain a protected directory.
    sources = [a[i + 1] for i, x in enumerate(a) if x in ("--ro-bind", "--bind")]
    for src in sources:
        for secret in spec.protect:
            if exposes(Path(src), secret):
                raise JailError(f"refusing to mount {src}: it contains {secret}")
    for k, v in spec.env.items():
        a += ["--setenv", k, v]
    a += ["--chdir", str(spec.workdir)]
    return a


def tool_dirs(binary: str) -> list[Path]:
    """Directories a CLI needs that are not under /usr or /opt.

    An npm global prefix outside /usr (for example ~/.npm-global) holds both
    the launcher and the package; bind the whole prefix read-only.
    """
    found = shutil.which(binary)
    if not found:
        return []
    out: list[Path] = []
    for p in {Path(found), Path(os.path.realpath(found))}:
        if str(p).startswith(("/usr/", "/opt/", "/bin/", "/sbin/")):
            continue
        prefix = p.parent
        for parent in p.parents:
            if parent.name == "node_modules" and parent.parent.name == "lib":
                prefix = parent.parent.parent
                break
        out.append(prefix)
    return sorted(set(out))


# --- self-test -------------------------------------------------------------

PROBE = r"""
found=""
for f in /proc/[0-9]*/environ; do
  if grep -qa "$SENTINEL_NEEDLE" "$f" 2>/dev/null; then found="$found env:$f"; fi
done
for p in "$@"; do
  if [ -e "$p" ] && grep -rqa "$SENTINEL_NEEDLE" "$p" 2>/dev/null; then found="$found file:$p"; fi
done
echo "FOUND:$found"
"""


async def self_test(data_dir: Path, extra_secret_paths: list[Path] | None = None,
                    unshare_pid: bool = True, bind_data_dir: bool = False,
                    no_jail: bool = False) -> list[str]:
    """Prove the jail hides the server's secrets. Returns what leaked (empty = OK).

    A decoy process outside the jail carries a random sentinel in its
    environment, and a sentinel file sits in the data directory. A probe inside
    a jail built with production rules searches every visible process
    environment and the data directory for it. The `unshare_pid`,
    `bind_data_dir` and `no_jail` switches exist so tests can weaken the jail
    and check that the probe then finds the sentinel.
    """
    sentinel = "manyeyes-sentinel-" + secrets.token_hex(12)
    data_dir.mkdir(parents=True, exist_ok=True)
    marker = data_dir / f".jail-sentinel-{os.getpid()}"
    marker.write_text(sentinel)
    decoy = subprocess.Popen(["sleep", "30"], env={"PATH": "/usr/bin:/bin",
                                                   "DECOY": sentinel})
    # An empty workdir of its own: binding a directory that contains the data
    # dir (for example /tmp) would expose it.
    scratch = data_dir / "run" / f"selftest-{os.getpid()}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        # The probe needs the needle without carrying it in its own
        # environment verbatim (it would find itself): pass it split.
        half = len(sentinel) // 2
        spec = JailSpec(workdir=scratch, env={"PATH": "/usr/bin:/bin"},
                        rw=[data_dir] if bind_data_dir else [], unshare_pid=unshare_pid,
                        protect=[] if bind_data_dir else [data_dir])
        argv = ([] if no_jail else build_argv(spec)) + [
            "sh", "-c", f'SENTINEL_NEEDLE="$0$1"; shift 1; {PROBE}',
            sentinel[:half], sentinel[half:],
            str(data_dir), *[str(p) for p in (extra_secret_paths or [])],
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        text = out.decode(errors="replace")
        if "FOUND:" not in text:
            raise JailError(f"jail probe did not run: {err.decode(errors='replace')[-300:]}")
        found = text.split("FOUND:", 1)[1].split()
        # The probe's own argv holds the halves, never the joined value; its
        # environ holds SENTINEL_NEEDLE only as a shell variable, not exported.
        return found
    finally:
        decoy.kill()
        decoy.wait()
        marker.unlink(missing_ok=True)
        shutil.rmtree(scratch, ignore_errors=True)
