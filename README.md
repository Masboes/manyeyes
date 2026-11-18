# manyeyes

> "Given enough eyeballs, all bugs are shallow." — Linus's law

**manyeyes runs a swarm of independent AI code reviewers on your change, then merges their findings into one list, ranked by how many reviewers found each problem.**

Several reviewers that each read the code alone find more than one reviewer that reads it several times. Where several of them flag the same line on their own, the finding is usually real. manyeyes runs Codex and Claude reviewers by default (Claude is optional), each one in its own sandbox, and gives you the merged synthesis in minutes.

![The manyeyes web UI: a finished review with the synthesis first](docs/images/job.png)

There are two ways to use it. Both run the same engine.

| | **CLI** — no server, no MCP | **Server** — self-hosted |
|---|---|---|
| For | You, in any git repository; scripts; coding agents with a shell | A team; coding agents over MCP; a browser |
| Start | `manyeyes review` | `manyeyes serve` (Docker) |
| Results | Printed to stdout, saved under `~/.manyeyes/results/` | Returned to the MCP caller, shown in the web UI |

---

## Use it from the command line

The CLI needs nothing but Docker and your reviewer logins. Run it in a repository, wait a few minutes, read the synthesis.

### Install

```bash
uv tool install git+https://github.com/Masboes/manyeyes@v1.5.0   # or: pipx install git+...
```

The CLI is small. The reviewers (Codex, Claude Code) and the sandbox ship in the manyeyes Docker image, which the CLI pulls on first use. (A PyPI package comes later.)

**Let a coding agent do it.** Tell Claude Code (or any agent with a shell): *"Install manyeyes for me: follow docs/install-for-agents.md in github.com/Masboes/manyeyes."* It does every step and stops only for the logins, which need you.

### Log the reviewers in (once)

```bash
manyeyes login codex            # ChatGPT device login: open the URL, sign in, enter the code
manyeyes login claude           # optional: type /login, pick your Claude account, then /exit
```

The logins live in `~/.manyeyes/auth/`. Every run mounts that folder, so you log in once on your machine. Codex uses your ChatGPT login only, never an API key. A server can hold more than one login per provider (`manyeyes login codex --name second`); reviewers then move to another login when one hits its limit. See [More than one login](docs/self-hosting.md#more-than-one-login-per-provider).

The default rosters include Claude. Without a Claude login, use Codex only: `export MANYEYES_ROSTER=codex:6 MANYEYES_ROSTER_QUICK=codex:3 MANYEYES_ROSTER_DEEP=codex:10` (a server sets the rosters in Settings).

For the synthesis step, add an OpenAI API key (optional; without it you get every review, but no merged list):

```bash
echo 'MANYEYES_SYNTH_OPENAI_API_KEY=sk-...' >> ~/.manyeyes/secrets.env && chmod 600 ~/.manyeyes/secrets.env
```

The key is used for the synthesis only. Reviewers never receive it.

Check everything:

```bash
manyeyes doctor --probe
```

### Review

```bash
cd your-repo
manyeyes review                                  # this branch against origin/HEAD (or main)
manyeyes review --base develop                   # against another base
manyeyes review --roster codex:2                 # fewer reviewers: cheaper
manyeyes review --roster codex:8,claude:2        # more reviewers
manyeyes review --focus "check the migration for data loss"
manyeyes review --repo owner/name --pr 123       # clone and review a GitHub pull request
```

Ask the swarm a free-form question instead:

```bash
manyeyes spar "Redis or in-process cache for the session store? Argue both sides."
manyeyes spar --roster codex:3 "Is this retry loop idempotent?"
```

Progress goes to stderr, the synthesis to stdout:

```
$ manyeyes review --roster codex:3,claude:1
manyeyes: job r-d0daffaa: your-repo — 4 reviewers (codex:3,claude:1)
manyeyes: 4 reviewers started; quorum 3
manyeyes: [1/4] codex#2    ok               2m27s
manyeyes: [2/4] codex#3    ok               2m27s
manyeyes: [3/4] codex#1    ok               2m55s
manyeyes: [4/4] claude#1   ok               3m58s
manyeyes: synthesis…
manyeyes: synthesis done in 41.8s
# Review: your-repo

**Reviews:** 4 of 4 succeeded

## Synthesis

## Cross-family consensus

### Relative repository paths fail in Docker mode — `src/cli/launcher.py:67` — medium
**[codex#1, codex#2, codex#3, claude#1]** · 2 families · 4 of 4 reviewers
When the caller's working directory is outside the repository, …
…
manyeyes: full result: /home/you/.manyeyes/results/r-d0daffaa.md
```

| Option | Effect |
|---|---|
| `--full` | Print every review after the synthesis |
| `--json` | Print the result as JSON: status, outcome counts, synthesis, every review with its tokens and timing |
| `--no-synth` | Skip the synthesis |
| `--roster` | Reviewers for this run, for example `codex:10` or `codex:8,claude:2` |
| `--quick` | Quick mode: fewer reviewers, about 3–4 minutes — while you iterate |
| `--deep` | Deep mode: thorough prompt, 5–15 minutes, about 10× the tokens — for risky changes |
| `--context owner/name` | A related repo the reviewers get read-only, to catch breakage across repos (repeat, at most 5) |
| `manyeyes jobs` | List past runs; `manyeyes jobs show <id>` prints one again |
| `manyeyes usage` | How much of the Codex and Claude plans is used, and when each window resets (spends no tokens) |
| `manyeyes version --check` | Show the version and whether a newer release is out |

Exit codes: `0` at least one review came back, `1` the run failed, `2` every reviewer was rate-limited, `130` interrupted.

### What the reviewers see

The reviewers get a clean copy of your change: the tracked files at HEAD, plus your uncommitted changes and untracked files (up to 1 MB each). Ignored files, such as `.env`, never reach a reviewer. Each reviewer runs in a bubblewrap jail that shows it only that copy and its own login — not your home folder, not your other secrets.

### Review standards for your repository

Put a `MANYEYES.md` file in the root of your repository, or in `.github/`. It works like a `CLAUDE.md`, but for reviews only. Every reviewer and the synthesis get its text as the repository's review standards. For example:

```markdown
- Every database query goes through the repository layer. Flag raw SQL.
- A new endpoint needs an authorization check and a test for it.
- Ignore style: the linter handles it.
```

manyeyes reads the file from the base branch, never from the change under review. A pull request therefore cannot change the rules it is judged by, if its base branch is protected. A pull request on top of another branch gets the standards of that branch. A change to `MANYEYES.md` applies after it merges. Text over 20,000 characters is cut, and a file over 80 KB is not used.

### The pull request's own words

When you review a pull request (not a branch), the reviewers also get its title, description and comments. They use them to learn what the change claims to do, and they check those claims against the code. Comments from bots are left out, so an earlier review cannot steer a new one. Only comments from the pull request's author (also an outside contributor) and from the repository's owners, members and collaborators count; other people's comments never reach a reviewer. The description is always included, also when a bot such as Dependabot wrote the pull request. The text is marked as data, not as instructions. Like the code, this text goes to the reviewer providers (OpenAI, Anthropic) and can appear in quotes in the result. Turn it off with `review.pr_context = false`.

If manyeyes cannot read `MANYEYES.md` or the pull request comments, the review still runs, and the result starts with a note that says so.

### From a coding agent, without MCP

Any agent with a shell can call the CLI directly. Add this to the agent's instructions (for example your `CLAUDE.md`):

```markdown
## Code review
Before you open a PR, run `manyeyes review --json` in the repository (it takes 3 to 15
minutes; run it in the background). Read `synthesis` from the JSON output. Fix every
finding that several reviewers agree on; judge single-reviewer findings on their merits.
```

`--json` keeps the output easy to parse, and the exit code tells the agent whether the run worked.

---

## Run it as a server

The server gives a team one place to run reviews: an MCP endpoint for coding agents, a REST API, and a web UI where you watch jobs, read reviews, change the default line-up and see stats.

| Jobs | Settings | Stats |
|---|---|---|
| ![Job list](docs/images/jobs.png) | ![Review defaults](docs/images/settings.png) | ![Stats](docs/images/stats.png) |

Connect Claude Code (it signs you in with GitHub on the first call):

```bash
claude mcp add --transport http manyeyes https://your-host/manyeyes/mcp
```

The agent then sees `swarm_review`, `swarm_spar`, `swarm_get`, `swarm_ls` and a few more. `swarm_review` blocks until the review is done and returns the synthesis, so the agent can act on it in the same turn. For a change that other repositories depend on, pass `context_repos` (for example `["acme/web", "acme/events@main"]`): the reviewers get those repos read-only and check whether the change breaks a caller, a shared contract or an event shape there. The server keeps a cached clone of each, so after the first use a context repo adds seconds, not minutes.

Three modes: `"full"` (default, the normal review, 4–5 minutes), `"quick"` (fewer reviewers, 3–4 minutes, while iterating) and `"deep"` (a thorough prompt that reads every changed file and traces dependencies, 5–15 minutes and about 10× the tokens — for risky changes only).

Self-hosting takes a data directory, a GitHub OAuth App and one container: see the [self-hosting guide](docs/self-hosting.md).

<img src="docs/images/jobs-phone-dark.png" alt="The job list in dark mode on a phone" width="260">

---

## Defaults

| Setting | Default |
|---|---|
| Reviewers, full mode (default) | `codex:4,claude:2`, standard prompt — about 4–5 minutes |
| Reviewers, quick mode | `codex:2,claude:1`, standard prompt — about 3–4 minutes |
| Reviewers, deep mode | `codex:8,claude:3`, thorough prompt — 5–15 minutes, about 10× the tokens |
| Codex model, effort | `gpt-6.2-sol`, `medium` |
| Claude model, effort | `sonnet` (Claude Code's newest Sonnet), `high` |
| Per-reviewer time limit | 10 minutes (20 in deep mode) |
| Straggler rule | when 80 % of reviewers have succeeded (at most all but one), the rest get 2 more minutes; a provider without a finished review yet is not cut off |
| Synthesis model | `gpt-6.1-sol` |

Change any of them per run (`--roster`, `--set key=value`), in `~/.manyeyes/config.toml`, with environment variables, or — on a server — on the Settings page. See the [configuration reference](docs/configuration.md).

## How it works

```
your change ──► clean copy ──► reviewer 1 ─┐
                               reviewer 2 ─┤   each in its own jail,
                                  …        ├─► each reads the code alone ──► synthesis ──► you
                               reviewer N ─┘   (Codex, Claude)                (OpenAI API)
```

Every reviewer run is recorded: model, effort, duration, input/cached/output/reasoning tokens, and its outcome (`success`, `timeout`, `straggler_killed`, `rate_limited`, `auth_failed`, `error`, `cancelled`) with a one-line reason.

## Documentation

- [Quickstart](docs/quickstart.md) — local install, first review
- [Install guide for coding agents](docs/install-for-agents.md) — exact steps, checks, and what needs a human
- [Self-hosting](docs/self-hosting.md) — server, GitHub sign-in, reverse proxy, MCP clients, trust model
- [Configuration reference](docs/configuration.md) — every setting
- [Architecture](docs/architecture.md) and [design](docs/design.md)
- [Releasing](docs/releasing.md) — versions, releases, how updates reach users
- [Changelog](CHANGELOG.md)

## Status

`1.5.0` — stable.

## License

[Apache License 2.0](LICENSE).
