"""Configuration: one registry of keys, five layers.

Precedence, lowest first:
  default < config file (TOML) < environment < web UI (database) < CLI flags

Every key lives in KEYS. The registry drives the TOML loader, the environment
loader, the web UI's editable defaults, and the configuration reference doc,
so a key exists in exactly one place.

Secrets never come from the config file. They come from the environment, from
`<NAME>_FILE` (a path whose content is the secret), or from the optional env
file `<data_dir>/secrets.env` (lowest-priority environment).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__


@dataclass(frozen=True)
class Key:
    name: str
    type: type
    default: Any
    help: str
    env: str = ""
    secret: bool = False
    editable: bool = False  # can be changed from the web UI
    choices: tuple[str, ...] = ()

    @property
    def env_name(self) -> str:
        return self.env or "MANYEYES_" + self.name.upper().replace(".", "_")


EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

KEYS: tuple[Key, ...] = (
    Key("data_dir", Path, "~/.manyeyes", "Database, runs, secrets file and auth homes. Keep it outside /tmp: Codex refuses to run its sandbox from there."),
    Key("auth_dir", Path, "", "Reviewer login homes (`codex/`, `claude/`). Default `<data_dir>/auth`."),
    Key("roster", str, "codex:8,claude:2",
        "Reviewers for a full run (the default mode).", editable=True),
    Key("roster_quick", str, "codex:4,claude:1",
        "Reviewers for a quick run (`mode=quick`, `--quick`): fewer and faster, for iterating.",
        editable=True),
    Key("roster_deep", str, "codex:8,claude:3",
        "Reviewers for a deep run (`mode=deep`, `--deep`); deep runs always use the thorough "
        "prompt.", editable=True),
    Key("review.depth", str, "standard",
        "Prompt for full runs: `standard` or `thorough` (reads every changed file in full, "
        "traces callers and dependencies; about 3x slower and 5x the tokens). Quick runs are "
        "always standard, deep runs always thorough.", editable=True,
        choices=("standard", "thorough")),
    Key("reviewers.codex.model", str, "gpt-6.1-sol", "Codex model.", editable=True),
    Key("reviewers.codex.effort", str, "medium", "Codex reasoning effort.", editable=True,
        choices=EFFORTS),
    Key("reviewers.codex.sandbox", str, "read-only",
        "Codex's own sandbox inside the jail: `read-only` or `danger-full-access`.",
        choices=("read-only", "danger-full-access")),
    Key("reviewers.codex.bin", str, "codex", "Codex CLI command."),
    Key("reviewers.claude.model", str, "sonnet", "Claude model.", editable=True),
    Key("reviewers.claude.effort", str, "high", "Claude effort.", editable=True,
        choices=("low", "medium", "high", "xhigh", "max")),
    Key("reviewers.claude.bin", str, "claude", "Claude Code CLI command."),
    Key("limits.reviewer_timeout_s", int, 600, "Hard limit per reviewer, seconds.", editable=True),
    Key("limits.quorum_fraction", float, 0.8,
        "Share of reviewers that must succeed before the straggler grace starts.", editable=True),
    Key("limits.straggler_grace_s", int, 120,
        "Seconds the remaining reviewers get after quorum.", editable=True),
    Key("limits.clone_timeout_s", int, 300, "Limit for cloning a repository, seconds."),
    Key("limits.synth_timeout_s", int, 300, "Limit for the synthesis call, seconds."),
    Key("limits.job_timeout_s", int, 1200, "Limit for a whole job, seconds."),
    Key("synth.enabled", bool, True, "Run the synthesis step.", editable=True),
    Key("synth.model", str, "gpt-6.1-sol", "OpenAI model for the synthesis.", editable=True),
    Key("synth.api_key", str, "", "OpenAI API key, used only for the synthesis.",
        env="MANYEYES_SYNTH_OPENAI_API_KEY", secret=True),
    Key("jail.enabled", bool, True, "Run every reviewer in a bubblewrap jail."),
    Key("container.memory", str, "",
        "Memory limit for the CLI's container, Docker syntax (for example `6g`). Empty: no limit."),
    Key("container.cpus", str, "", "CPU limit for the CLI's container (for example `8`). Empty: no limit."),
    Key("image", str, "ghcr.io/masboes/manyeyes:"
        + ("main" if "dev" in __version__ else __version__),
        "Docker image the local CLI starts (a development version uses the `main` image)."),
    Key("server.host", str, "127.0.0.1", "Address the server listens on."),
    Key("server.port", int, 8789, "Port the server listens on."),
    Key("server.root_path", str, "", "Fixed URL prefix, when the proxy sends no X-Forwarded-Prefix."),
    Key("server.public_urls", list, [], "External base URLs, for example `https://example.org/manyeyes`."),
    Key("server.trusted_proxies", list, ["127.0.0.1"],
        "Proxy addresses whose X-Forwarded-* headers are honored. `*` trusts all."),
    Key("server.max_concurrent_jobs", int, 2, "Jobs that run at the same time."),
    Key("server.max_concurrent_reviewers", int, 15,
        "Reviewers that run at the same time, across all jobs. Extra reviewers wait for a slot."),
    Key("github.token", str, "", "Token for cloning private repositories and reading PRs.",
        env="MANYEYES_GITHUB_TOKEN", secret=True),
    Key("auth.github_client_id", str, "", "GitHub OAuth App client ID.",
        env="MANYEYES_GITHUB_CLIENT_ID"),
    Key("auth.github_client_secret", str, "", "GitHub OAuth App client secret.",
        env="MANYEYES_GITHUB_CLIENT_SECRET", secret=True),
    Key("auth.allowlist_file", Path, "", "GitHub usernames allowed in. Default `<data_dir>/allowlist.txt`."),
    Key("auth.session_secret", str, "", "Signs sessions. Generated into `<data_dir>` when empty.",
        env="MANYEYES_SESSION_SECRET", secret=True),
    Key("auth.bootstrap_token", str, "", "Optional static API token.",
        env="MANYEYES_BOOTSTRAP_TOKEN", secret=True),
    Key("auth.bootstrap_login", str, "", "GitHub login the bootstrap token acts as.",
        env="MANYEYES_BOOTSTRAP_LOGIN"),
    Key("auth.mcp_oauth", bool, True, "Offer GitHub OAuth on the MCP endpoint."),
    Key("auth.admins", list, [],
        "GitHub logins of the admins: they change settings, cancel and delete anyone's jobs, "
        "see the full audit log and everyone's usage, and are exempt from the per-person "
        "limits. Empty: everyone may change settings, and nobody is exempt."),
    Key("limits.user_jobs_per_hour", int, 0, "Reviews and spars one person may start per hour "
        "(0: no limit).", editable=True),
    Key("limits.user_jobs_per_day", int, 0, "Reviews and spars one person may start per day "
        "(0: no limit).", editable=True),
    Key("limits.user_deep_jobs_per_day", int, 0, "Deep reviews one person may start per day "
        "(0: no limit).", editable=True),
    Key("limits.user_reviewer_runs_per_day", int, 0, "Reviewer runs one person may use per day, "
        "summed over their jobs' rosters (0: no limit). Weighs big jobs fairly.", editable=True),
    Key("limits.user_concurrent_jobs", int, 0, "Jobs of one person that may run at the same time; "
        "others wait while other people's jobs go first (0: no limit).", editable=True),
    Key("update.check", bool, True, "Check GitHub for a newer release (server: twice a day)."),
    Key("update.repo", str, "Masboes/manyeyes", "GitHub repository whose releases are checked."),
    Key("usage.interval_min", int, 30, "Server: read the Codex and Claude plan usage this "
        "often, in minutes (no tokens are spent). 0: off."),
    Key("retention.days", int, 0, "Delete jobs older than this. 0 keeps everything."),
    Key("retention.keep_telemetry", bool, True, "Keep reviewer telemetry when deleting jobs."),
)
KEY_BY_NAME = {k.name: k for k in KEYS}
EDITABLE = tuple(k.name for k in KEYS if k.editable)


class ConfigError(ValueError):
    pass


def reference_markdown() -> str:
    """The configuration reference table (docs/configuration.md is generated from it)."""
    rows = ["| Key | Environment | Default | Web UI | Meaning |", "|---|---|---|---|---|"]
    for k in KEYS:
        default = k.default
        if isinstance(default, list):
            default = ",".join(default)
        shown = "—" if default in ("", None) else f"`{default}`"
        env = f"`{k.env_name}`" + (" (or `_FILE`)" if k.secret else "")
        flags = "yes" if k.editable else ""
        help_ = k.help + (f" One of: {', '.join(k.choices)}." if k.choices else "")
        rows.append(f"| `{k.name}` | {env} | {shown} | {flags} | {help_} |")
    return "\n".join(rows)


def coerce(key: Key, raw: Any) -> Any:
    """Convert a raw value (from TOML, env or the UI) to the key's type."""
    try:
        if key.type is bool:
            if isinstance(raw, bool):
                return raw
            s = str(raw).strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off", ""):
                return False
            raise ValueError(raw)
        if key.type is list:
            if isinstance(raw, list):
                return [str(x).strip() for x in raw if str(x).strip()]
            return [x.strip() for x in str(raw).split(",") if x.strip()]
        if key.type is Path:
            return Path(str(raw)).expanduser() if str(raw) else ""
        if key.type is int:
            return int(raw)
        if key.type is float:
            return float(raw)
        value = str(raw).strip()
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{key.name}: cannot read {raw!r} as {key.type.__name__}") from e
    if key.choices and value not in key.choices:
        raise ConfigError(f"{key.name}: {value!r} is not one of {', '.join(key.choices)}")
    return value


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file. Blank lines and # comments are skipped."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, name + "."))
        else:
            out[name] = v
    return out


@dataclass
class Settings:
    values: dict[str, Any]
    sources: dict[str, str]
    config_file: Path | None = None
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    secret_files: list[Path] = field(default_factory=list)   # *_FILE paths: never mounted

    def __getitem__(self, name: str) -> Any:
        return self.values[name]

    # Derived paths -------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return Path(self.values["data_dir"]).expanduser()

    @property
    def auth_dir(self) -> Path:
        v = self.values["auth_dir"]
        return Path(v).expanduser() if v else self.data_dir / "auth"

    @property
    def allowlist_file(self) -> Path:
        v = self.values["auth.allowlist_file"]
        return Path(v).expanduser() if v else self.data_dir / "allowlist.txt"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "manyeyes.db"

    # Layers --------------------------------------------------------------
    def with_layer(self, layer: str, overrides: dict[str, Any],
                   allow_secrets: bool = True) -> Settings:
        """A copy with `overrides` applied on top, recorded as `layer`."""
        values = dict(self.values)
        sources = dict(self.sources)
        layers = dict(self.layers)
        applied: dict[str, Any] = {}
        for name, raw in overrides.items():
            key = KEY_BY_NAME.get(name)
            if key is None:
                raise ConfigError(f"unknown setting: {name}")
            if key.secret and not allow_secrets:
                raise ConfigError(f"{name} is a secret; set {key.env_name} instead")
            if raw is None:
                continue
            values[name] = applied[name] = coerce(key, raw)
            sources[name] = layer
        layers[layer] = applied
        return Settings(values, sources, self.config_file, layers, list(self.secret_files))

    def public(self) -> list[dict[str, Any]]:
        """All keys for display: secrets shown only as set / not set."""
        rows = []
        for k in KEYS:
            v = self.values[k.name]
            if k.secret:
                v = bool(v)
            elif isinstance(v, Path):
                v = str(v)
            rows.append({"name": k.name, "value": v, "source": self.sources[k.name],
                         "default": k.default, "editable": k.editable, "secret": k.secret,
                         "help": k.help, "choices": list(k.choices), "type": k.type.__name__})
        return rows


def load(
    config_file: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> Settings:
    """Build Settings from defaults, the TOML file and the environment."""
    env = dict(os.environ if env is None else env)
    values = {k.name: k.default for k in KEYS}
    sources = {k.name: "default" for k in KEYS}
    secret_files: list[Path] = []

    # The data dir decides where the config file and env file live, so read it
    # from the environment first.
    data_dir = Path(env.get("MANYEYES_DATA_DIR", KEY_BY_NAME["data_dir"].default)).expanduser()

    # Env file: lowest-priority environment. Real environment wins.
    env_file = Path(env.get("MANYEYES_ENV_FILE", data_dir / "secrets.env")).expanduser()
    env = {**read_env_file(env_file), **env}

    path = Path(config_file or env.get("MANYEYES_CONFIG",
                                       data_dir / "config.toml")).expanduser().resolve()
    file_values: dict[str, Any] = {}
    if path.is_file():
        with path.open("rb") as fh:
            file_values = _flatten(tomllib.load(fh))
        for name, raw in file_values.items():
            key = KEY_BY_NAME.get(name)
            if key is None:
                raise ConfigError(f"{path}: unknown setting {name!r}")
            if key.secret:
                raise ConfigError(
                    f"{path}: {name} is a secret; set {key.env_name} or {key.env_name}_FILE")
            values[name] = coerce(key, raw)
            sources[name] = "file"
    elif config_file:
        raise ConfigError(f"config file not found: {path}")

    for key in KEYS:
        raw = env.get(key.env_name)
        # A direct value wins over a _FILE reference (the launcher passes the
        # resolved value into the container, where the host path may not exist).
        file_ref = env.get(key.env_name + "_FILE") if key.secret and raw is None else None
        if file_ref:
            secret_files.append(Path(file_ref).expanduser().resolve())
            try:
                raw = Path(file_ref).expanduser().read_text().strip()
            except OSError as e:
                raise ConfigError(f"{key.env_name}_FILE: cannot read {file_ref}: "
                                  f"{e.strerror}") from e
        if raw is None:
            continue
        values[key.name] = coerce(key, raw)
        sources[key.name] = "env"

    values["data_dir"] = Path(str(values["data_dir"])).expanduser().resolve()
    for k in KEYS:
        if k.type is Path and k.name != "data_dir" and values[k.name]:
            values[k.name] = Path(values[k.name]).expanduser().resolve()
    settings = Settings(values, sources, path if path.is_file() else None,
                        secret_files=secret_files)
    validate(settings)
    return settings


def validate(s: Settings) -> None:
    q = s["limits.quorum_fraction"]
    if not 0 < q <= 1:
        raise ConfigError("limits.quorum_fraction must be in (0, 1]")
    for name in ("limits.reviewer_timeout_s", "limits.straggler_grace_s",
                 "limits.job_timeout_s", "server.max_concurrent_jobs",
                 "server.max_concurrent_reviewers"):
        if s[name] < 1:
            raise ConfigError(f"{name} must be at least 1")
    for name in ("limits.user_jobs_per_hour", "limits.user_jobs_per_day",
                 "limits.user_deep_jobs_per_day", "limits.user_reviewer_runs_per_day",
                 "limits.user_concurrent_jobs", "usage.interval_min"):
        if s[name] < 0:
            raise ConfigError(f"{name} must be 0 (off or no limit) or more")
    if s["limits.clone_timeout_s"] >= s["limits.job_timeout_s"]:
        raise ConfigError("limits.clone_timeout_s must be below limits.job_timeout_s")
    if bool(s["auth.bootstrap_token"]) != bool(s["auth.bootstrap_login"]):
        raise ConfigError("set auth.bootstrap_token and auth.bootstrap_login together")
    if (s["reviewers.codex.sandbox"] == "danger-full-access" and not s["jail.enabled"]):
        raise ConfigError("reviewers.codex.sandbox=danger-full-access needs the jail "
                          "(jail.enabled = true)")
    from .engine.roster import parse_roster  # local import: avoid a cycle

    parse_roster(s["roster"])
    parse_roster(s["roster_quick"])
    parse_roster(s["roster_deep"])
