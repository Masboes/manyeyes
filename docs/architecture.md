# Architecture

One package, one engine, two entry points. The full design and its decisions are in [design.md](design.md).

```
manyeyes CLI ──► engine ◄── server (FastAPI: web UI, /api, /mcp, GitHub sign-in)
                   │                 │
                   ▼                 ▼
          reviewers in jails     SQLite store (jobs, reviewer_runs, synth_runs,
          (codex ×N, claude ×M)   job_events, users, api_tokens, audit, settings)
                   │
                   ▼
          synthesis (OpenAI API)
```

## Modules

| Path | Job |
|---|---|
| `engine/runner.py` | Starts the reviewers in parallel, applies the timeout and the straggler policy, cancels, emits events. |
| `engine/jail.py` | The bubblewrap jail around every reviewer, and the jail self-test. |
| `engine/reviewers/` | One adapter per CLI: argv, environment, output parsing, token usage. |
| `engine/classify.py` | Turns a failed run into one outcome and a one-line reason. |
| `engine/synth.py` | The one OpenAI call that merges the reviews. |
| `engine/bundle.py` | Markdown and JSON rendering. |
| `sources.py` | Prepares the code: a clean local copy, or a partial GitHub clone. |
| `jobs.py` | Runs one job and records everything in the store (shared by CLI and server). |
| `store/` | SQLite store, numbered migrations, stats. |
| `server/` | App, REST API, MCP tools, auth, queue worker, static web UI. |
| `cli/` | Commands, and the launcher that runs them in the Docker image. |
| `config.py` | The key registry and the five configuration layers. |

## A job, end to end

1. A caller submits a review (MCP, API, web UI or CLI). The store records it as `queued`.
2. The worker claims it, resolves the settings (file, environment, web UI overrides) and clones the change.
3. The runner starts every reviewer in its own jail. Each one reads the code, writes a review and exits.
4. When enough reviews have succeeded (quorum), the remaining reviewers get a short grace period, then they are stopped. A reviewer whose family (codex, claude) has no successful review yet is not stopped: it runs until its family has one, or until its own timeout.
5. The synthesis merges the reviews. The model sees them under anonymous labels (R1, R2, … in random order, no model names), to reduce vendor bias; it returns the merged findings as JSON. The code then places each finding by agreement (cross-family, single-family, singleton) and lists low-severity single-reviewer points under "Minor notes".
6. The store records each reviewer's outcome, duration and tokens, the synthesis and the rendered result. The MCP call that was waiting returns the synthesis.

## Outcomes

`success`, `timeout`, `straggler_killed`, `rate_limited`, `auth_failed`, `error`, `cancelled`. Each failed run also has a one-line `failure_reason` and the tail of its output in `failure_detail`.
