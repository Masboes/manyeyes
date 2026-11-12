"""manyeyes command line.

    manyeyes review [PATH] [--base REF]          review the current branch
    manyeyes review --repo OWNER/NAME --pr N      review a GitHub pull request
    manyeyes spar "question"                      ask the swarm a free-form question
    manyeyes login codex|claude                   log a reviewer in (mounted auth dir)
    manyeyes doctor [--probe]                     check logins, jail, tools
    manyeyes serve                                run the server (API, MCP, web UI)
    manyeyes jobs [ls|show ID|delete ID]          past runs
    manyeyes config                               effective settings and their source

Exit codes for review and spar: 0 at least one review, 1 failed, 2 every
reviewer was rate-limited, 130 interrupted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import __version__
from ..config import ConfigError, Settings, load
from ..engine.bundle import fmt_seconds, render_markdown, result_dict
from ..engine.models import Outcome
from ..engine.roster import RosterError, build_specs
from ..engine.runner import SwarmError
from ..engine.task import SparTask
from ..sources import SourceError, clone_review, local_review
from . import launcher

CONTAINER_COMMANDS = ("review", "spar", "login", "doctor", "usage")


def err(msg: str) -> None:
    print(f"manyeyes: {msg}", file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="manyeyes", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"manyeyes {__version__}")
    p.add_argument("--config", help="config file (default <data_dir>/config.toml)")
    p.add_argument("--no-container", action="store_true",
                   help="run here with host-installed codex/claude instead of the Docker image")
    p.add_argument("--no-jail", action="store_true",
                   help="run reviewers without the bubblewrap jail (local only; not with "
                        "codex sandbox danger-full-access)")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override one setting for this run, e.g. --set synth.enabled=false")
    sub = p.add_subparsers(dest="cmd", required=True)

    def swarm_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--roster", help="reviewers, e.g. codex:10 or codex:8,claude:2")
        depth = sp.add_mutually_exclusive_group()
        depth.add_argument("--quick", action="store_true",
                           help="quick mode: the roster_quick reviewers, standard prompt")
        depth.add_argument("--deep", action="store_true",
                           help="deep mode: roster_deep reviewers, thorough prompt (several "
                                "times the tokens; for risky changes)")
        sp.add_argument("--no-synth", action="store_true", help="skip the synthesis")
        sp.add_argument("--full", action="store_true", help="print every review, not only "
                                                            "the synthesis")
        sp.add_argument("--json", action="store_true", help="print the result as JSON")

    r = sub.add_parser("review", help="review a change")
    r.add_argument("path", nargs="?", default=".", help="repository path (default: .)")
    r.add_argument("--base", help="base ref (default: origin/HEAD, main or master)")
    r.add_argument("--repo", help="GitHub owner/name: clone and review instead of a local path")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--pr", type=int, help="pull request number (with --repo)")
    g.add_argument("--branch", help="head branch (with --repo)")
    r.add_argument("--focus", default="", help="extra instructions for the reviewers")
    r.add_argument("--context", action="append", default=[], metavar="OWNER/NAME[@BRANCH]",
                   help="a related GitHub repository the reviewers get read-only, to check "
                        "cross-repo breakage (repeat, at most 5)")
    swarm_opts(r)

    s = sub.add_parser("spar", help="ask the swarm a free-form question")
    s.add_argument("question")
    swarm_opts(s)

    lg = sub.add_parser("login", help="log a reviewer in")
    lg.add_argument("kind", choices=("codex", "claude"))

    d = sub.add_parser("doctor", help="check logins, tools and the jail")
    d.add_argument("--probe", action="store_true", help="also send a one-line prompt to "
                                                        "each logged-in reviewer kind")

    u = sub.add_parser("usage", help="how much of the Codex and Claude plans is used "
                                     "(spends no tokens)")
    u.add_argument("--json", action="store_true", help="print JSON")

    sub.add_parser("serve", help="run the server")

    v = sub.add_parser("version", help="show the version; --check looks for a newer release")
    v.add_argument("--check", action="store_true", help="ask GitHub for the latest release")

    j = sub.add_parser("jobs", help="past runs")
    j.add_argument("action", nargs="?", default="ls", choices=("ls", "show", "delete"))
    j.add_argument("id", nargs="?")
    j.add_argument("--limit", type=int, default=20)
    j.add_argument("--yes", action="store_true", help="delete without asking")

    u = sub.add_parser("users", help="user data (server)")
    u.add_argument("action", choices=("forget",))
    u.add_argument("login")
    u.add_argument("--yes", action="store_true", help="erase without asking")

    cf = sub.add_parser("config", help="show effective settings")
    cf.add_argument("--reference", action="store_true",
                    help="print the configuration reference table (markdown)")
    return p


def settings_from(args: argparse.Namespace) -> Settings:
    """Settings for this run. Precedence for the roster: --roster, then --quick
    (the roster_quick value, after any --set override), then roster."""
    s = load(args.config)
    flags: dict[str, object] = {}
    for item in args.set:
        if "=" not in item:
            raise ConfigError(f"--set needs KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        flags[k.strip()] = v
    from .. import modes
    mode = "quick" if getattr(args, "quick", False) else (
        "deep" if getattr(args, "deep", False) else "full")
    if mode != "full" and not getattr(args, "roster", None):
        key = modes.ROSTER_KEY[mode]
        flags["roster"] = flags.get(key) or s[key]
    if mode != "full":
        flags["review.depth"] = "thorough" if mode == "deep" else "standard"
    if mode == "deep":
        flags.update(modes.deep_limits(s.with_layer("flag", {k: v for k, v in flags.items()
                                                             if k.startswith("limits.")},
                                                    allow_secrets=False)))
    if getattr(args, "roster", None):
        flags["roster"] = args.roster
    if getattr(args, "no_synth", False):
        flags["synth.enabled"] = False
    if args.no_jail:
        flags["jail.enabled"] = False
    if flags:
        s = s.with_layer("flag", flags, allow_secrets=False)
        from ..config import validate
        validate(s)
    return s


# --- review / spar -------------------------------------------------------

def _progress(total: int):
    state = {"done": 0, "t0": time.monotonic()}

    def show(kind: str, p: dict) -> None:
        if kind == "swarm_started":
            err(f"{total} reviewers started; quorum {p['quorum']}")
        elif kind == "reviewer_finished":
            state["done"] += 1
            mark = "ok" if p["outcome"] == "success" else p["outcome"]
            extra = f" — {p['reason']}" if p.get("reason") else ""
            err(f"[{state['done']}/{total}] {p['reviewer']:10s} {mark:16s} "
                f"{fmt_seconds(p['duration_s'])}{extra}")
        elif kind == "quorum_reached":
            err(f"quorum reached; the rest get {int(p['grace_s'])}s")
        elif kind == "synth_started":
            err("synthesis…")
        elif kind == "synth_finished":
            if p["outcome"] == "success":
                err(f"synthesis done in {fmt_seconds(p['duration_s'])}")
            else:
                err(f"synthesis skipped: {p.get('note')}")
    return show


async def _swarm(args: argparse.Namespace, s: Settings) -> int:
    from ..jobs import run_job
    from ..store.db import Store

    store = Store(s.db_path)
    roster = s["roster"]
    specs = build_specs(roster, s)
    fields: dict[str, object] = {"kind": args.cmd, "requested_by": "local", "source": "cli",
                                 "roster": roster}
    work = s.data_dir / "work" / f"cli-{os.getpid()}-{int(time.time() * 1000)}"
    # One deadline for the whole run, preparation included.
    deadline = time.monotonic() + float(s["limits.job_timeout_s"])
    try:
        if args.cmd == "spar":
            task = SparTask(args.question)
            work.mkdir(parents=True)
            fields["prompt"] = args.question
        elif args.repo:
            err(f"cloning {args.repo}…")
            task = await clone_review(args.repo, work, token=s["github.token"],
                                      timeout=s["limits.clone_timeout_s"], pr=args.pr,
                                      head=args.branch, base=args.base, instructions=args.focus)
            fields.update(repo=args.repo, pr=args.pr, prompt=args.focus or None)
        else:
            # Inside the container the launcher passes the host's absolute path.
            target = os.environ.get("MANYEYES_REVIEW_PATH") or args.path
            src = local_review(Path(target).resolve(), work, args.base, args.focus,
                               exclude=[s.data_dir, s.auth_dir])
            task = src.task
            fields.update(repo=src.name, prompt=args.focus or None)

        extra_ro: list[Path] = []
        if args.cmd == "review" and args.context:
            # Before the job row exists: a failure here leaves no stale "running" job.
            import dataclasses

            from ..sources import context_cache_dir, prepare_context
            err(f"preparing {len(args.context)} related repo(s)…")
            left = deadline - time.monotonic()
            try:
                ctx = await asyncio.wait_for(prepare_context(
                    args.context, s.data_dir / "context-cache", Path(str(work) + "-context"),
                    token=s["github.token"],
                    timeout=min(float(s["limits.clone_timeout_s"]), left)), timeout=max(1.0, left))
            except TimeoutError as e:
                raise SourceError("preparing the related repos ran out of time") from e
            task = dataclasses.replace(task, context=tuple(ctx))
            extra_ro = [Path(str(work) + "-context")] + [
                context_cache_dir(s.data_dir / "context-cache", c[0]) for c in ctx]
        # Created as running: a server sharing this data dir must never claim it.
        job = store.create_job(status="running", started_at=time.time(), **fields)
        err(f"job {job['id']}: {task.label} — {len(specs)} reviewers ({roster})")
        result = await run_job(store, s, job, task, work, progress=_progress(len(specs)),
                               deadline=deadline, extra_ro=extra_ro)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(Path(str(work) + "-context"), ignore_errors=True)

    from ..jobs import job_title
    full_md = render_markdown(job_title(store.get_job(job["id"]) or job), result)
    path = _write_private(s.data_dir / "results", f"{job['id']}.md", full_md)
    if args.json:
        print(json.dumps({"id": job["id"], **result_dict(result)}, indent=2, default=str))
    else:
        print(full_md if args.full else render_markdown(
            job_title(job), result, include_reviews=False))
    err(f"full result: {path}")
    if result.successes:
        return 0
    if result.runs and all(r.outcome == Outcome.RATE_LIMITED for r in result.runs):
        return 2
    return 1


def _confirm(args: argparse.Namespace, question: str) -> bool:
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        err("refusing without --yes (no terminal to ask)")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _write_private(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


# --- login / doctor --------------------------------------------------------

def cmd_login(args: argparse.Namespace, s: Settings) -> int:
    home = s.auth_dir / args.kind
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY",
                                                           "ANTHROPIC_API_KEY")}
    if args.kind == "codex":
        env["CODEX_HOME"] = str(home)
        argv = [s["reviewers.codex.bin"], "login", "--device-auth"]
        err(f"Codex login into {home}. Open the URL it prints, sign in with your ChatGPT "
            "account, and enter the code.")
    else:
        env["CLAUDE_CONFIG_DIR"] = str(home)
        argv = [s["reviewers.claude.bin"]]
        err(f"Claude login into {home}. In Claude Code, type /login, choose your Claude "
            "account, finish in the browser, then type /exit.")
    try:
        return subprocess.call(argv, env=env)
    except FileNotFoundError:
        err(f"{argv[0]} is not installed")
        return 1


async def _usage(args: argparse.Namespace, s: Settings) -> int:
    import time as _time

    from ..usage import probe
    windows = await probe(s)
    if args.json:
        print(json.dumps([w.to_dict() for w in windows], indent=2))
        return 0 if windows and not all(w.error for w in windows) else 1
    if not windows:
        err("no reviewer is logged in (manyeyes login codex)")
        return 1
    for w in windows:
        if w.error:
            print(f"{w.provider:7s} FAIL  {w.error}")
            continue
        left = ""
        if w.resets_at:
            mins = max(0, int(w.resets_at - _time.time()) // 60)
            left = f"resets in {mins // 60} h {mins % 60:02d} min" if mins >= 60 else \
                f"resets in {mins} min"
        print(f"{w.provider:7s} {w.window:16s} {w.used_percent:5.0f}% used  {left}")
    return 1 if all(w.error for w in windows) else 0


async def _doctor(args: argparse.Namespace, s: Settings) -> int:
    from ..engine import jail
    from ..engine.reviewers import ADAPTERS

    problems = 0

    def line(ok: bool | None, label: str, detail: str = "") -> None:
        nonlocal problems
        mark = {True: "ok  ", False: "FAIL", None: "--  "}[ok]
        problems += ok is False
        print(f"{mark} {label}" + (f" — {detail}" if detail else ""))

    print(f"manyeyes {__version__}; data dir {s.data_dir}; "
          f"{'container' if launcher.in_container() else 'host'}")
    roster_kinds = {sp.kind for sp in build_specs(s["roster"], s)}
    for kind, cls in ADAPTERS.items():
        rev = cls(build_specs(f"{kind}:1", s)[0], s, s.data_dir / "run" / "doctor")
        needed = kind in roster_kinds
        b = rev.binary()
        version = ""
        if b:
            try:
                version = subprocess.run([b, "--version"], capture_output=True, text=True,
                                         timeout=20).stdout.strip().splitlines()[0]
            except Exception:  # noqa: BLE001
                version = "?"
        line(bool(b) if needed else (True if b else None), f"{kind} CLI", version or "missing")
        line(rev.is_logged_in() if needed else (True if rev.is_logged_in() else None),
             f"{kind} login", str(rev.auth_home))
    line(bool(s["synth.api_key"]) or None, "synthesis key",
         "set" if s["synth.api_key"] else "not set; synthesis will be skipped")
    if s["jail.enabled"]:
        if not jail.bwrap_path():
            line(False, "jail", "bwrap missing")
        else:
            try:
                leaks = await jail.self_test(s.data_dir)
                line(not leaks, "jail self-test", "nothing leaked" if not leaks
                     else f"LEAKED: {' '.join(leaks)}")
            except Exception as e:  # noqa: BLE001
                line(False, "jail self-test", str(e))
    else:
        line(None, "jail", "disabled (--no-jail)")

    if args.probe:
        from ..engine.runner import Runner
        for kind in sorted(roster_kinds):
            specs = build_specs(f"{kind}:1", s)
            r = Runner(s.with_layer("flag", {"synth.enabled": False}))
            probe_dir = s.data_dir / "run" / "probe"
            probe_dir.mkdir(parents=True, exist_ok=True)
            res = await r.run(SparTask("Reply with the single word: ok"), specs, probe_dir)
            run = res.runs[0]
            u = run.usage
            line(run.ok, f"{kind} probe ({run.spec.model}/{run.spec.effort})",
                 f"{fmt_seconds(run.duration_s)}, in {u.input_tokens} out {u.output_tokens}"
                 if run.ok else run.failure_reason)
    return 1 if problems else 0


# --- jobs, users, config ------------------------------------------------------

def cmd_jobs(args: argparse.Namespace, s: Settings) -> int:
    from ..store.db import Store
    store = Store(s.db_path)
    if args.action == "ls":
        for j in store.list_jobs(args.limit):
            started = time.strftime("%Y-%m-%d %H:%M", time.localtime(j["created_at"]))
            target = j.get("repo") or (j.get("prompt") or "")[:40]
            print(f"{j['id']}  {started}  {j['kind']:6s} {j['status']:9s} {target}")
        return 0
    if not args.id or not re.fullmatch(r"[rs]-[0-9a-f]{8}", args.id):
        err("give a job id, for example r-1a2b3c4d")
        return 1
    if args.action == "delete":
        if not _confirm(args, f"Delete job {args.id} and its result?"):
            return 1
        ok = store.delete_job(args.id, keep_telemetry=s["retention.keep_telemetry"])
        (s.data_dir / "results" / f"{args.id}.md").unlink(missing_ok=True)
        err("deleted" if ok else "no such job")
        return 0 if ok else 1
    job = store.get_job(args.id)
    if not job:
        err("no such job")
        return 1
    print(job.get("result_md") or f"{job['id']}: {job['status']} {job.get('error') or ''}")
    return 0


async def _version(args: argparse.Namespace, s: Settings) -> int:
    print(f"manyeyes {__version__} (image {s['image']})")
    if not args.check:
        return 0
    from ..updates import check
    info = await check(s)
    if info.update_available:
        print(f"manyeyes {info.latest} is available: {info.url}")
        print(f"update: uv tool install --force git+https://github.com/{s['update.repo']}@v{info.latest}")
        return 0
    print(f"up to date ({info.error})" if info.error else "up to date")
    return 0


def cmd_config(args: argparse.Namespace, s: Settings) -> int:
    if args.reference:
        from ..config import reference_markdown
        print(reference_markdown())
        return 0
    print(f"config file: {s.config_file or '(none)'}")
    for row in s.public():
        v = ("set" if row["value"] else "not set") if row["secret"] else row["value"]
        print(f"{row['name']:32s} {str(v):40s} [{row['source']}]")
    return 0


def _without_config_flag(raw: list[str]) -> list[str]:
    out, skip = [], False
    for a in raw:
        if skip:
            skip = False
            continue
        if a == "--config":
            skip = True
            continue
        if a.startswith("--config="):
            continue
        out.append(a)
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        s = settings_from(args)
    except (ConfigError, RosterError) as e:
        err(str(e))
        return 2
    raw = list(sys.argv[1:] if argv is None else argv)

    if (args.cmd in CONTAINER_COMMANDS and not args.no_container
            and not launcher.in_container()):
        repo = Path(args.path) if args.cmd == "review" and not args.repo else None
        # The container gets the config as an absolute MANYEYES_CONFIG; drop
        # the (possibly relative) --config argument.
        return launcher.run_in_container(s, _without_config_flag(raw),
                                         interactive=args.cmd == "login", repo_path=repo)

    try:
        if args.cmd in ("review", "spar"):
            return asyncio.run(_swarm(args, s))
        if args.cmd == "login":
            return cmd_login(args, s)
        if args.cmd == "doctor":
            return asyncio.run(_doctor(args, s))
        if args.cmd == "usage":
            return asyncio.run(_usage(args, s))
        if args.cmd == "serve":
            from ..server.app import serve
            return serve(s)
        if args.cmd == "jobs":
            return cmd_jobs(args, s)
        if args.cmd == "users":
            from ..store.db import Store
            if not _confirm(args, f"Erase {args.login}'s jobs, tokens and audit events?"):
                return 1
            Store(s.db_path).forget_user(args.login)
            err(f"forgot {args.login}")
            return 0
        if args.cmd == "config":
            return cmd_config(args, s)
        if args.cmd == "version":
            return asyncio.run(_version(args, s))
    except (SourceError, ConfigError, RosterError, SwarmError) as e:
        err(str(e))
        return 1
    except TimeoutError:
        err(f"the job ran past limits.job_timeout_s ({s['limits.job_timeout_s']}s)")
        return 1
    except KeyboardInterrupt:
        err("interrupted")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
