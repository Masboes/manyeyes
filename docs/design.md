# manyeyes design

Status: accepted. Name **manyeyes** (repository, Python package and module `manyeyes`, command `manyeyes`).

> "Given enough eyeballs, all bugs are shallow." — Linus's law

This is the original design. Where it differs from the code, the code wins: see [architecture.md](architecture.md) for the current structure and [configuration.md](configuration.md) for every setting and default.

## 1. Goal

One package that runs a swarm of independent AI reviewers against code, and merges their findings into one synthesis. It has one review engine and two entry points:

- **Local CLI.** Anyone runs it against their own repository.
- **Self-hosted server.** An MCP server, a job queue, a REST API and a web UI in one process.

It replaces two earlier tools of the same author:

- **agent-swarm**: the review engine and CLI. A bash wrapper started one Docker container per run.
- **agent-swarm-cloud**: a FastAPI daemon with a SQLite queue, an MCP server and a web UI. It shelled out to the agent-swarm wrapper per job and parsed its markdown output.

The main structural problem today: the daemon talks to the engine through a shell wrapper, Docker, a results directory and a markdown parser. Each hop loses data (tokens, failure reasons) and adds a failure mode (bundle mix-ups at concurrency > 1). manyeyes calls the engine in-process.

## 2. Requirements

| Area | Requirement |
|---|---|
| Reviewers | Codex and Claude. Claude is optional: without a Claude login, use Codex-only rosters. Cursor and Gemini are removed. |
| Default line-up | Measured mixed rosters per mode (section 13); every roster is a setting. |
| Codex auth | ChatGPT login only, from a mounted `CODEX_HOME`. Codex never receives an API key. |
| OpenAI API key | Used only for the synthesis step. |
| Agent auth | Mounted directories, default `<data_dir>/auth/{claude,codex}`. No secrets in the repo. |
| Packaging | One Docker image for both modes. Local install with `uv tool install` or `pipx`. |
| Config | Environment variables plus one config file. No hardcoded hosts, users or paths. |
| Hosted access | GitHub OAuth plus an allowlist file of GitHub usernames. Fully self-hostable. |
| Results | Back to the calling agent (MCP) and to the web UI. |
| Telemetry | Fresh database. Per reviewer run: model, effort, duration, input / output / reasoning tokens, outcome, clear failure reason. |

**Families.** Cross-family agreement (Codex and Claude both find an issue) is a strong signal, and each family finds bugs the other misses. The default rosters therefore mix both; a Codex-only roster is faster and needs one login.

## 3. Architecture

```
                 ┌──────────────── one package: manyeyes ────────────────┐
                 │                                                        │
 local user ──►  │  cli ──────────┐                                       │
                 │                ▼                                       │
                 │            engine  ──►  reviewers (codex ×N, claude ×M) │
                 │   (roster, runner, timeouts, telemetry, synthesis)     │
                 │                ▲                                       │
 MCP client ──►  │  server ───────┘                                       │
 browser    ──►  │  (FastAPI: /mcp, /api, / web UI; SQLite queue)         │
                 └────────────────────────────────────────────────────────┘
                       store: SQLite (jobs, reviewer_runs, synth_runs, users, tokens)
```

### 3.1 Engine (`manyeyes.engine`)

The engine is a library. The CLI and the server call the same function:

```python
result = await run_swarm(task, roster, workdir, limits, store, on_event)
```

| Module | Job |
|---|---|
| `task.py` | `ReviewTask` (repo path, base ref, head ref) and `SparTask` (prompt). Builds the reviewer prompt. |
| `roster.py` | Parses `codex:10,claude:2` and per-reviewer model/effort overrides. |
| `reviewers/base.py` | `Reviewer` interface: `run(prompt, cwd, deadline) -> ReviewerRun`. Process group spawn and kill. |
| `reviewers/codex.py` | `codex exec --json`. Per-instance `CODEX_HOME` with the shared `auth.json` (prevents the known startup races). Reads token usage from the JSON event stream. |
| `reviewers/claude.py` | `claude -p --output-format json`. Reads usage from the result object. |
| `jail.py` | Starts each reviewer inside a bubblewrap jail (section 3.3). |
| `runner.py` | Runs all reviewers in parallel. Hard per-reviewer timeout, straggler policy, cancel. Emits events (`reviewer_started`, `reviewer_finished`, `synth_started`, …). |
| `classify.py` | Maps an exit to one outcome (section 6). Structured error events first, regex fallback second. |
| `synth.py` | One OpenAI API call that merges the anonymous reviews into JSON findings; the code then groups them by agreement. Never load-bearing: a failed synthesis still returns every review. |
| `bundle.py` | Renders markdown for humans and a JSON result for machines. |

Rules for the engine:

- It never reads `OPENAI_API_KEY` and never passes any OpenAI key to a reviewer. The synthesis key has its own name (`MANYEYES_SYNTH_OPENAI_API_KEY`). The reviewer environment is built from an allowlist, not copied from `os.environ`.
- It writes telemetry through a `Store` interface. The CLI and the server use the same SQLite store, so local runs get the same stats.
- It does not clone, does not talk to GitHub, and does not know about HTTP. Callers prepare the working directory.

### 3.2 Where reviewers run

The reviewer CLIs (codex, claude) and their sandboxes need a known environment. The Docker image is that environment.

| Mode | Engine runs in | Reviewers run in |
|---|---|---|
| Local, default | The image, started by the host CLI (`docker run`, repo mounted read-only) | Jails in the same container |
| Local, `--no-container` | The host Python process (Linux only) | Host-installed `codex` / `claude` in jails; needs `bwrap` |
| Server | The image (`manyeyes serve`) | Jails in the same container. No Docker socket, no Docker-in-Docker. |

The host CLI is a thin launcher in Python (it replaces the bash wrapper). It mounts the repo read-only, the auth directories, and the data directory, then runs the same `manyeyes` command inside the image. For a linked git worktree it also mounts the shared git directory read-only (`git rev-parse --git-common-dir`), so `git diff` works inside the container. macOS and Windows use the default container mode.

### 3.3 Reviewer isolation

A malicious repository can prompt-inject a reviewer. In server mode the reviewer shares a container with the database, the synthesis key, the GitHub token and the session secret. The container alone is therefore not a sufficient boundary. Each reviewer process starts inside a bubblewrap jail that the engine builds, independent of the reviewer's own sandbox:

| Jail rule | Effect |
|---|---|
| `--unshare-pid`, fresh `/proc` | The reviewer cannot see the server process or read its environment. |
| `--clearenv` plus an explicit variable list | No secret reaches the reviewer through the environment. |
| Read-only bind of `/usr`, `/opt`, the tool installs, and a fixed list of `/etc` files (CA certificates, `passwd`, `group`, `hosts`, `resolv.conf`, `nsswitch.conf`, `gitconfig`) | The CLIs run. No other `/etc` file is visible. |
| Read-only bind of the job workdir, plus its git common directory when the workdir is a linked worktree (same path as outside) | The reviewer reads the code and its git history, and cannot change either. A test runs `git diff` inside the jail from a linked worktree. |
| Read-write bind of that reviewer's own auth home only | Codex and Claude can refresh their login tokens. |
| Nothing else from `data_dir` | Database, secrets, other jobs and other users' data stay invisible. |
| Per-reviewer tmpfs `/tmp` | No shared scratch space between reviewers. |

Inside the jail, codex still runs `--sandbox read-only` and claude still runs `--permission-mode plan`. The image runs with `seccomp=unconfined` so that bubblewrap can create user namespaces (same as today). Residual risk: this exposes more kernel system calls to code in the container. The jail runs as an unprivileged user, so a kernel bug is the remaining path out. Operators who need more can run the container under gVisor or in a VM.

`reviewers.codex.sandbox = "danger-full-access"` (needed on hosts where codex's nested bwrap fails) is allowed only because the outer jail still holds. If the engine cannot build the outer jail, it refuses to run, in every mode. Local users who accept the risk pass `--no-jail` explicitly; `--no-jail` together with `danger-full-access` is rejected.

Secret files named in `*_file` keys must not lie inside any path the jail binds. The config loader rejects such a setup at start.

**Jail self-test.** At boot (server) and in `manyeyes doctor` (local), the engine places a random sentinel value in its own environment, in a file in `data_dir`, and in a file next to each configured secret file. It then runs a probe process inside a jail with the production rules. The probe searches every `/proc/*/environ` it can see, `data_dir`, and the secret paths for the sentinel. If it finds the sentinel anywhere, the engine refuses to run. The test suite also runs the probe against a deliberately weakened jail and asserts that the probe then finds the sentinel, so a broken probe cannot pass silently.

**Network.** Reviewers need the network to reach their provider, so the jail does not cut it. Residual risk, stated in the security docs: a prompt-injected reviewer can send the code under review and its own login token to any host. It cannot reach the server's secrets. An egress allowlist (provider endpoints only) is a later option for operators who need it.

### 3.4 Server (`manyeyes.server`)

One FastAPI process serves everything under one prefix:

| Path | What |
|---|---|
| `/` | Web UI (static files shipped in the package, no build step) |
| `/api/*` | REST API |
| `/mcp` | MCP, streamable HTTP. Served without a redirect, with or without a trailing slash, so clients never lose the `Authorization` header. |
| `/auth/*` | GitHub OAuth login, callback, logout |
| `/healthz` | Unauthenticated health check |

**Prefix handling.** The server honors `X-Forwarded-Prefix` from trusted proxies (`server.trusted_proxies`), or a fixed `server.root_path`. Every generated URL (OAuth callback, cookies, links in the UI, MCP OAuth metadata) uses the prefix. Nothing in the code names a specific prefix. `server.public_urls` lists every external base URL the server answers on (for example a test route and the production route during a migration). The server builds OAuth callbacks only for URLs on that list.

**Queue.** A SQLite job table. The worker loop calls the engine in-process, so there is no output scraping. `server.max_concurrent_jobs` and `server.max_concurrent_reviewers` cap the load; several jobs share one ChatGPT rate limit.

**Deadlines.** Every phase has its own limit (clone, reviewers, synthesis, cleanup), and the whole job has one too, so one stuck job cannot hold the queue. The defaults are in [configuration.md](configuration.md). Tests cover a hanging clone and a hanging synthesis call.

**Jobs.** `review` (GitHub PR number, or head branch vs base branch) and `spar` (free-form prompt). For a review, the server clones into a per-job directory with the deployment GitHub token, checks the merge base, runs the engine, then deletes the clone. The token never enters the jail; the clone has no credentials in `.git/config`.

**Restart.** On boot, jobs left `running` become `failed` with the reason `server restarted`. Queued jobs stay queued and run.

### 3.5 MCP contract

Tool names stay the same, so existing clients keep working: `swarm_review`, `swarm_spar`, `swarm_get`, `swarm_ls`, `swarm_status`, `swarm_logs`, `swarm_cancel`, `swarm_stats`. Names alone are not a contract, so:

- The tool schemas of the earlier server are captured as a JSON fixture in `tests/fixtures/mcp_v1_tools.json`.
- A test compares the manyeyes tool list against the fixtures. Argument names, types, required fields, and the top-level response keys (`id`, `status`, `waited_seconds`, `synthesis` / `error` / `note`) must match.
- Additive changes are allowed (new optional arguments, new response keys). Anything else is a breaking change and goes in the changelog with a migration note.
- Blocking behavior stays: review and spar block until done, send progress every 10 s, and return the job id with a note when the wait budget runs out.
- When manyeyes replaces an earlier server at the same URL, clients need no reinstall. At most they sign in with GitHub once more, because the new server issues new OAuth tokens.

### 3.6 Authentication and authorization

| Who | How |
|---|---|
| Browser | GitHub OAuth. Session JWT in an httpOnly cookie, path = prefix. |
| MCP client | MCP OAuth through GitHub (FastMCP `GitHubProvider`). The client adds only the URL; `claude mcp add --transport http <name> <url>` opens a GitHub sign-in. Behind a prefix it needs the RFC 9728 / 8414 `/.well-known/…` routes at the origin root; the nginx sample includes them. |
| Scripts, CI, header-only clients | Personal API tokens. A logged-in user creates them in the web UI. Stored hashed, tied to a GitHub login. |
| Bootstrap | Optional `MANYEYES_BOOTSTRAP_TOKEN` with `MANYEYES_BOOTSTRAP_LOGIN` (both required together). It acts as that GitHub login, which must be on the allowlist. Remove the variable and restart to revoke it. At cutover, the old shared token can be set here, so header-only clients keep working. |

One GitHub OAuth App serves both the web UI and MCP. It registers two exact redirect URIs per prefix: `<public_url>/auth/callback` (web UI) and `<public_url>/mcp/auth/callback` (MCP). Wildcard matching stays off. "Expire user access tokens" stays off, because MCP re-verifies the GitHub token after sign-in.

The allowlist is a file of GitHub usernames (`auth.allowlist_file`), one per line. The server reloads it when it changes. Every request re-checks the login, so removing a name revokes the session and all its tokens at once.

There is no anonymous mode. The server refuses to start without GitHub OAuth or at least one bootstrap token.

**Trust model (v1).** One deployment serves one team that trusts each other. Every allowlisted user:

- can review any repository that the deployment GitHub token can read,
- can see every job, its synthesis and its reviews,
- can cancel only their own jobs.

Per-user repository permissions (for example, using each user's own GitHub token) are out of scope for v1. The self-hosting guide states this, and states that the deployment token defines the reach of every allowlisted user. A mixed-trust deployment is not supported in v1.

**Audit.** Sign-ins, token create and revoke, allowlist reloads (with the names added and removed) and job cancels go to an `audit_events` table and the log.

## 4. Kept, dropped, rewritten

| Item | Decision | Why |
|---|---|---|
| Parallel runner, process-group kill, straggler kill | **Rewritten** | Same mechanics, success-based quorum (section 6). |
| Codex worker, per-instance `CODEX_HOME`, spawn jitter | **Kept** | Fixes measured startup races at 5–10 instances. |
| Claude worker | **Kept**, optional | Supported reviewer, off by default. |
| Cursor, Gemini workers | **Dropped** | No working credentials; off by default today. |
| Family-grouped synthesis prompt | **Rewritten** | The model merges anonymous reviews; the code groups by family when the roster has more than one family, by agreement count otherwise. |
| Synthesis via OpenAI API | **Kept** | Now the only use of the API key. |
| Bash host wrapper, `install.sh` | **Dropped** | Replaced by the Python launcher and `uv tool install`. |
| Bundle markdown on disk as the interface | **Dropped** | Results live in the database. Markdown is a rendering. |
| `bundles.py` (parse timings back out of markdown) | **Dropped** | The engine records telemetry directly. |
| Single static `SWARM_TOKEN` | **Rewritten** | Per-user hashed tokens plus an optional bootstrap token. |
| Single allowed GitHub login | **Rewritten** | Allowlist file. |
| Daemon on host, shelling out to Docker | **Rewritten** | Daemon runs inside the image; reviewers run in jails. |
| SQLite job queue, orphan reconcile on boot | **Kept** | Simple and correct for one host. |
| MCP blocking model, nested timeouts | **Kept** | Clients rely on it (section 3.5). |
| `stats.py` aggregation | **Rewritten** | Reads the new `reviewer_runs` table; adds tokens and failure reasons. |
| Siblings workspace (`/siblings`) | **Deferred** | Specific to one deployment today. It can return as a generic `review.context_repos` option. |
| `usage` probe command | **Rewritten** | Becomes `manyeyes doctor`: auth, CLI versions, jail check, a one-line probe per reviewer. |
| Web UI | **Rewritten** | New look (section 8), served by the daemon. |
| nginx-injecting installer | **Dropped** | Self-hosters own their proxy. Docs give a sample block. |

## 5. Repository layout

```
src/manyeyes/
  engine/            task, roster, runner, jail, classify, synth, bundle, reviewers/
  cli/               main (argparse), launcher (docker run), login, doctor
  server/            app, api, mcp, auth, queue, github, web/ (static UI)
  store/             db, migrations/*.sql, stats
  config.py          one Settings object: defaults < file < env < flags
docker/Dockerfile    one image: python + codex + claude + gh + git + rg + bwrap
compose.yaml         server mode sample
deploy/              systemd user unit, nginx sample block
tests/               unit + integration (fake reviewer binaries), MCP contract fixtures
docs/                design, quickstart, self-hosting, configuration, architecture
```

## 6. Reviewer runs, outcomes and telemetry

### Outcomes

| Outcome | Meaning |
|---|---|
| `success` | Reviewer returned a review. |
| `timeout` | Hard per-reviewer deadline hit. |
| `straggler_killed` | Killed by the straggler policy. |
| `rate_limited` | Provider said rate limit, quota, or capacity. |
| `auth_failed` | Not logged in, expired, or rejected credentials. |
| `error` | Anything else; `failure_detail` holds the stderr tail. |
| `cancelled` | User cancelled the job. |

### Straggler policy

- `quorum = ceil(limits.quorum_fraction × N)`, default fraction `0.8`. N is the number of reviewers launched.
- Only `success` counts toward quorum. Failures never arm the straggler timer, so fast failures cannot cut short the reviewers that still work.
- When quorum is reached, the reviewers still running get `limits.straggler_grace_s` (default 120 s), then they are killed with outcome `straggler_killed`.
- When quorum becomes impossible (too many failures), no straggler timer runs. The remaining reviewers run to the hard timeout (`limits.reviewer_timeout_s`, default 1200 s).

### Job outcome

| Successful reviews | Job status | Notes |
|---|---|---|
| ≥ 2 | `done` | Synthesis runs. |
| 1 | `done` | No synthesis; the one review is the result. |
| 0 | `failed` | `error` names the dominant failure reason (for example "all 10 reviewers rate-limited"). |

`partial = true` means at least one reviewer did not succeed, whatever the job status. Every job carries a per-outcome count, in the API, the MCP response and the UI.

### Database (fresh, SQLite, WAL, versioned migrations)

```
jobs            id, kind, status, partial, requested_by, repo, pr, head_ref, base_ref,
                head_sha, base_sha, prompt, roster, created_at, started_at,
                finished_at, error, synthesis_md, result_json
reviewer_runs   id, job_id, reviewer, instance, model, effort, started_at,
                finished_at, duration_s, input_tokens, cached_input_tokens,
                output_tokens, reasoning_tokens, outcome, failure_reason,
                failure_detail, exit_code, review_md
synth_runs      job_id, model, duration_s, input_tokens, output_tokens,
                outcome, failure_detail
users           github_login, first_seen, last_seen
api_tokens      id, github_login, name, token_hash, created_at, last_used_at, revoked_at
job_events      job_id, ts, type, payload_json      (live progress for UI and MCP)
audit_events    ts, github_login, action, detail_json
```

Local CLI runs use the same schema in `<data_dir>/manyeyes.db`.

### Data handling and retention

- **What leaves the host.** Reviewers send repository content to their provider (OpenAI for Codex, Anthropic for Claude) under the logged-in account's terms. The synthesis step sends all reviews to the OpenAI API under the API key's terms. The self-hosting guide says this plainly: do not review repositories whose content may not go to these providers.
- **What is stored.** Prompts, reviews, synthesis, GitHub logins, token metadata and timings. No repository content beyond what reviewers quote.
- **Operator duty.** The operator of a deployment decides the lawful basis, provider agreements (DPA) and transfer terms for the data they send to providers. The self-hosting guide lists what is sent where, so operators can make that call.
- **Erasure.** `manyeyes users forget <login>` deletes that login's tokens, sessions, audit events, usage records and jobs (with their review text). Reviewer telemetry without text stays, so stats survive. This cannot be undone.
- **Retention.** `retention.days` (default 0 = keep forever). A daily task deletes jobs older than that, with their runs and events. `manyeyes jobs delete <id>` and the API delete one job. Telemetry rows can be kept without review text (`retention.keep_telemetry = true`), so stats survive.

## 7. Configuration

Precedence: built-in defaults < config file < `MANYEYES_*` environment < CLI flags. The config file is TOML at `$MANYEYES_CONFIG`, default `<data_dir>/config.toml`. Secrets come only from the environment or from files named in `*_file` keys; the config file never holds a secret value.

Every key, its environment name and its default are in [configuration.md](configuration.md), generated from the code.

## 8. Web UI

The look: Plus Jakarta Sans for text, Space Grotesk for display, a paper / ink / blue / line palette, a night palette for dark mode, 1200 px container. Plain HTML, CSS and JavaScript with the tokens as CSS variables, so the image needs no Node build for the UI. Self-hosters can override the tokens with one CSS file.

Screens: sign-in, job list with live status, job detail (synthesis first, then per-reviewer runs with model, tokens, duration, outcome), new review / spar form, API tokens, stats.

## 9. Research (deferred)

The replay study (4 / 8 / 10 / 12 `gpt-6.1-sol` reviewers against about 20 past PRs with known bugs, with `gpt-6-astra` — the previous Codex default model — and the old Codex + Claude mix as baselines) is deferred. The current default rosters come from smaller measured runs (section 13).

When it runs, two lessons from the design review apply:

- Subsets of one large concurrent run are valid for finding quality (recall, false positives, duplicates), but not for wall-clock or rate limits, because the large run adds contention. Latency and rate limits need separate runs at each roster size.
- The telemetry in section 6 records everything that study needs, so real runs can answer most of it over time.

## 10. Security model

- Reviewers run in jails (section 3.3). They see only the code under review and their own auth home.
- A prompt-injected reviewer can still send the code under review to its own provider and write a misleading review. Review only repositories you are willing to send to the providers.
- The synthesis key, the GitHub token, the session secret and the database never reach a reviewer.
- The server has no anonymous mode. Tokens are stored hashed. Cookies are httpOnly, Secure, SameSite=Lax.

## 11. Deployment

- **CLI:** `uv tool install`, then `manyeyes login codex`. See [quickstart.md](quickstart.md).
- **Server:** one container with a systemd `--user` unit or Docker Compose behind a reverse proxy. See [self-hosting.md](self-hosting.md).
- **From the earlier agent-swarm server:** the MCP tool names, arguments and response keys stay the same (`tests/fixtures/mcp_v1_tools.json`), so clients keep their configuration. Point the proxy at manyeyes; keep the old server until the new one has run for some time, so a rollback is one proxy change.

## 12. Release

- Open source under Apache-2.0 (explicit patent grant; common for developer infrastructure).
- No hardcoded hosts or users, no secrets, no client names in code, config defaults or docs.
- Semver. `CHANGELOG.md` in Keep a Changelog format.
- GitHub Actions: tests and lint on every PR; release workflow on `v*` tags (GitHub release and GHCR image). PyPI later.

## 13. Decisions

| Decision | Status |
|---|---|
| Public name | manyeyes (2026-11-10) |
| Default roster | measured: full `codex:8,claude:2`, quick `codex:4,claude:1`, deep `codex:8,claude:3` (thorough) |
| Claude | Optional reviewer; in the default rosters because it finds bugs Codex misses |
| License | Apache-2.0 (2026-11-10) |

## Naming

Checked on 2026-11-10 against PyPI (`/pypi/<name>/json`) and GitHub repository search.

| Name | PyPI | GitHub | Note |
|---|---|---|---|
| **manyeyes** (chosen) | free | 16 repos; an unrelated `manyeyes` org (speech models) | From Linus's law. Short, memorable. |
| shallowbugs | free | 0 | Same origin, more distinctive. |
| codejury | free | 19 repos, one with 149 stars | Fits the verdict model. Crowded. |
| reviewswarm | free | 0 | Plain and descriptive. |
| bugjury | free | 0 | Short, clear, free. |
