# Configuration reference

Every setting has one key. Set it in the config file (TOML, `<data_dir>/config.toml` or `$MANYEYES_CONFIG`), in the environment, for one run with `--set key=value`, or — for the keys marked "Web UI" — on the server's Settings page.

Precedence, lowest first: default < config file < environment < web UI < `--set` flags.

Secrets (keys whose environment name allows `_FILE`) never come from the config file. Set them in the environment, point `<NAME>_FILE` at a file that holds the value, or put them in `<data_dir>/secrets.env` (`NAME=value` lines, mode 600). `--set` refuses secrets, so they never land in shell history.

Example `config.toml`:

```toml
roster = "codex:8,claude:2"

[reviewers.codex]
model = "gpt-6.1-sol"
effort = "medium"

[limits]
straggler_grace_s = 90
```

`manyeyes config` prints the effective value and source of every key.

<!-- generated: manyeyes config --reference -->

| Key | Environment | Default | Web UI | Meaning |
|---|---|---|---|---|
| `data_dir` | `MANYEYES_DATA_DIR` | `~/.manyeyes` |  | Database, runs, secrets file and auth homes. Keep it outside /tmp: Codex refuses to run its sandbox from there. |
| `auth_dir` | `MANYEYES_AUTH_DIR` | — |  | Reviewer login homes (`codex/`, `claude/`). Default `<data_dir>/auth`. |
| `roster` | `MANYEYES_ROSTER` | `codex:8,claude:2` | yes | Reviewers for a full run (the default mode). |
| `roster_quick` | `MANYEYES_ROSTER_QUICK` | `codex:4,claude:1` | yes | Reviewers for a quick run (`mode=quick`, `--quick`): fewer and faster, for iterating. |
| `roster_deep` | `MANYEYES_ROSTER_DEEP` | `codex:8,claude:3` | yes | Reviewers for a deep run (`mode=deep`, `--deep`); deep runs always use the thorough prompt. |
| `review.depth` | `MANYEYES_REVIEW_DEPTH` | `standard` | yes | Prompt for full runs: `standard` or `thorough` (reads every changed file in full, traces callers and dependencies; about 3x slower and 5x the tokens). Quick runs are always standard, deep runs always thorough. One of: standard, thorough. |
| `reviewers.codex.model` | `MANYEYES_REVIEWERS_CODEX_MODEL` | `gpt-6.1-sol` | yes | Codex model. |
| `reviewers.codex.effort` | `MANYEYES_REVIEWERS_CODEX_EFFORT` | `medium` | yes | Codex reasoning effort. One of: minimal, low, medium, high, xhigh. |
| `reviewers.codex.sandbox` | `MANYEYES_REVIEWERS_CODEX_SANDBOX` | `read-only` |  | Codex's own sandbox inside the jail: `read-only` or `danger-full-access`. One of: read-only, danger-full-access. |
| `reviewers.codex.bin` | `MANYEYES_REVIEWERS_CODEX_BIN` | `codex` |  | Codex CLI command. |
| `reviewers.claude.model` | `MANYEYES_REVIEWERS_CLAUDE_MODEL` | `sonnet` | yes | Claude model. |
| `reviewers.claude.effort` | `MANYEYES_REVIEWERS_CLAUDE_EFFORT` | `high` | yes | Claude effort. One of: low, medium, high, xhigh, max. |
| `reviewers.claude.bin` | `MANYEYES_REVIEWERS_CLAUDE_BIN` | `claude` |  | Claude Code CLI command. |
| `limits.reviewer_timeout_s` | `MANYEYES_LIMITS_REVIEWER_TIMEOUT_S` | `600` | yes | Hard limit per reviewer, seconds. |
| `limits.quorum_fraction` | `MANYEYES_LIMITS_QUORUM_FRACTION` | `0.8` | yes | Share of reviewers that must succeed before the straggler grace starts. |
| `limits.straggler_grace_s` | `MANYEYES_LIMITS_STRAGGLER_GRACE_S` | `120` | yes | Seconds the remaining reviewers get after quorum. |
| `limits.clone_timeout_s` | `MANYEYES_LIMITS_CLONE_TIMEOUT_S` | `300` |  | Limit for cloning a repository, seconds. |
| `limits.synth_timeout_s` | `MANYEYES_LIMITS_SYNTH_TIMEOUT_S` | `300` |  | Limit for the synthesis call, seconds. |
| `limits.job_timeout_s` | `MANYEYES_LIMITS_JOB_TIMEOUT_S` | `1200` |  | Limit for a whole job, seconds. |
| `synth.enabled` | `MANYEYES_SYNTH_ENABLED` | `True` | yes | Run the synthesis step. |
| `synth.model` | `MANYEYES_SYNTH_MODEL` | `gpt-6.1-sol` | yes | OpenAI model for the synthesis. |
| `synth.api_key` | `MANYEYES_SYNTH_OPENAI_API_KEY` (or `_FILE`) | — |  | OpenAI API key, used only for the synthesis. |
| `jail.enabled` | `MANYEYES_JAIL_ENABLED` | `True` |  | Run every reviewer in a bubblewrap jail. |
| `container.memory` | `MANYEYES_CONTAINER_MEMORY` | — |  | Memory limit for the CLI's container, Docker syntax (for example `6g`). Empty: no limit. |
| `container.cpus` | `MANYEYES_CONTAINER_CPUS` | — |  | CPU limit for the CLI's container (for example `8`). Empty: no limit. |
| `image` | `MANYEYES_IMAGE` | `ghcr.io/masboes/manyeyes:1.2.0` |  | Docker image the local CLI starts (a development version uses the `main` image). |
| `server.host` | `MANYEYES_SERVER_HOST` | `127.0.0.1` |  | Address the server listens on. |
| `server.port` | `MANYEYES_SERVER_PORT` | `8789` |  | Port the server listens on. |
| `server.root_path` | `MANYEYES_SERVER_ROOT_PATH` | — |  | Fixed URL prefix, when the proxy sends no X-Forwarded-Prefix. |
| `server.public_urls` | `MANYEYES_SERVER_PUBLIC_URLS` | — |  | External base URLs, for example `https://example.org/manyeyes`. |
| `server.trusted_proxies` | `MANYEYES_SERVER_TRUSTED_PROXIES` | `127.0.0.1` |  | Proxy addresses whose X-Forwarded-* headers are honored. `*` trusts all. |
| `server.max_concurrent_jobs` | `MANYEYES_SERVER_MAX_CONCURRENT_JOBS` | `2` |  | Jobs that run at the same time. |
| `server.max_concurrent_reviewers` | `MANYEYES_SERVER_MAX_CONCURRENT_REVIEWERS` | `15` |  | Reviewers that run at the same time, across all jobs. Extra reviewers wait for a slot. |
| `github.token` | `MANYEYES_GITHUB_TOKEN` (or `_FILE`) | — |  | Token for cloning private repositories and reading PRs. |
| `auth.github_client_id` | `MANYEYES_GITHUB_CLIENT_ID` | — |  | GitHub OAuth App client ID. |
| `auth.github_client_secret` | `MANYEYES_GITHUB_CLIENT_SECRET` (or `_FILE`) | — |  | GitHub OAuth App client secret. |
| `auth.allowlist_file` | `MANYEYES_AUTH_ALLOWLIST_FILE` | — |  | GitHub usernames allowed in. Default `<data_dir>/allowlist.txt`. |
| `auth.session_secret` | `MANYEYES_SESSION_SECRET` (or `_FILE`) | — |  | Signs sessions. Generated into `<data_dir>` when empty. |
| `auth.bootstrap_token` | `MANYEYES_BOOTSTRAP_TOKEN` (or `_FILE`) | — |  | Optional static API token. |
| `auth.bootstrap_login` | `MANYEYES_BOOTSTRAP_LOGIN` | — |  | GitHub login the bootstrap token acts as. |
| `auth.mcp_oauth` | `MANYEYES_AUTH_MCP_OAUTH` | `True` |  | Offer GitHub OAuth on the MCP endpoint. |
| `auth.admins` | `MANYEYES_AUTH_ADMINS` | — |  | GitHub logins of the admins: they change settings, cancel and delete anyone's jobs, see the full audit log and everyone's usage, and are exempt from the per-person limits. Empty: everyone may change settings, and nobody is exempt. |
| `limits.user_jobs_per_hour` | `MANYEYES_LIMITS_USER_JOBS_PER_HOUR` | `0` | yes | Reviews and spars one person may start per hour (0: no limit). |
| `limits.user_jobs_per_day` | `MANYEYES_LIMITS_USER_JOBS_PER_DAY` | `0` | yes | Reviews and spars one person may start per day (0: no limit). |
| `limits.user_deep_jobs_per_day` | `MANYEYES_LIMITS_USER_DEEP_JOBS_PER_DAY` | `0` | yes | Deep reviews one person may start per day (0: no limit). |
| `limits.user_reviewer_runs_per_day` | `MANYEYES_LIMITS_USER_REVIEWER_RUNS_PER_DAY` | `0` | yes | Reviewer runs one person may use per day, summed over their jobs' rosters (0: no limit). Weighs big jobs fairly. |
| `limits.user_concurrent_jobs` | `MANYEYES_LIMITS_USER_CONCURRENT_JOBS` | `0` | yes | Jobs of one person that may run at the same time; others wait while other people's jobs go first (0: no limit). |
| `update.check` | `MANYEYES_UPDATE_CHECK` | `True` |  | Check GitHub for a newer release (server: twice a day). |
| `update.repo` | `MANYEYES_UPDATE_REPO` | `Masboes/manyeyes` |  | GitHub repository whose releases are checked. |
| `usage.interval_min` | `MANYEYES_USAGE_INTERVAL_MIN` | `30` |  | Server: read the Codex and Claude plan usage this often, in minutes (no tokens are spent). 0: off. |
| `retention.days` | `MANYEYES_RETENTION_DAYS` | `0` |  | Delete jobs older than this. 0 keeps everything. |
| `retention.keep_telemetry` | `MANYEYES_RETENTION_KEEP_TELEMETRY` | `True` |  | Keep reviewer telemetry when deleting jobs. |
