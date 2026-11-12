# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.1.2] — 2026-11-12

First public release: the repository is now `Masboes/manyeyes`, open source under Apache-2.0.

### Changed
- `manyeyes version --check` and the server's update check read releases from `Masboes/manyeyes` (setting `update.repo`).
- Install docs no longer need a GitHub login for the repository or the image.

## [1.1.1] — 2026-11-10

### Fixed
- The synthesis no longer drops an issue that only one reviewer raised, such as a risk the reviewer could not verify or the same bug left unfixed elsewhere. In a test on 6 real PRs, every tested synthesis model dropped such issues; with the final rule, gpt-6.1-sol kept all three in each of 6 runs. A synthesis is now about 40% longer; the extra findings are mostly low severity.

## [1.1.0] — 2026-11-10

For servers shared by a team: per-person limits, and a view of how much of the provider plans is used.

### Added
- Apache License 2.0.
- Multi-user controls: `auth.admins`; per-person limits (`limits.user_jobs_per_hour`, `_per_day`, `user_deep_jobs_per_day`, `user_reviewer_runs_per_day`) returning HTTP 429 with the reason; a per-person running-jobs cap that lets other people go first; per-person stats.
- Plan usage: the Stats page and `manyeyes usage` show how much of the Codex and Claude plans is used and when each window resets, read every 30 minutes without spending tokens (`usage.interval_min`, `GET /api/usage`).

## [1.0.0] — 2026-11-10

First stable release. manyeyes now serves the production MCP endpoint that the previous agent-swarm server used; clients keep their configuration.

### Added
- Quick and full modes: `roster_quick` setting, `mode` argument on `swarm_review` and `swarm_spar` (default `full`), `--quick` on the CLI, and a Quick run choice in the web UI.
- `review.depth` setting: `standard` or `thorough` review prompt.
- Deep mode (`mode=deep`, `--deep`, `roster_deep`): the thorough prompt with Codex and Claude, for risky changes.

- Context repositories: `context_repos` on `swarm_review`, `--context` on the CLI, and a field in the web UI. Reviewers get up to 5 related repositories read-only (cached clones, a fresh snapshot per job) to check cross-repo breakage.

### Changed
- Synthesis model `gpt-6.1-sol` (was `gpt-5.6-luna`): in a blind test on 6 real PRs it kept 44 of 47 real findings against 41, with no misattributed reviewers, for about 9 seconds more.
- Deep mode gets double time limits (at least 20 minutes per reviewer, 40 per job).
- Default rosters from a measured comparison on 6 real PRs: full `codex:8,claude:2`, quick `codex:4,claude:1`, deep `codex:8,claude:3`. Codex-only reviews missed most severe issues that Claude found, and the reverse.
- Time limits tightened to what reviews take: 10 minutes per reviewer, 20 minutes per job.

## [0.1.0] — 2026-11-10

### Added
- Review engine: Codex (default, 10 × `gpt-6.1-sol`, medium) and Claude reviewers in parallel, each in a bubblewrap jail; success-based quorum with straggler grace; per-reviewer telemetry (model, effort, duration, input/cached/output/reasoning tokens, outcome, failure reason); synthesis through the OpenAI API.
- CLI: `review` (clean local copy or GitHub PR), `spar`, `login`, `doctor --probe`, `jobs`, `config`, `serve`, `users forget`.
- Server: SQLite job queue, REST API, MCP tools compatible with the previous swarm server, GitHub sign-in with an allowlist, personal API tokens, review defaults editable at runtime, audit log, retention, prefix-aware URLs and MCP OAuth discovery behind a path prefix.
- One Docker image for the CLI and the server; `systemd --user` unit, Compose file and nginx sample.
- Web UI: jobs with live progress, job detail (synthesis first, per-reviewer telemetry), new review and spar, review defaults editable in the browser, API tokens, stats, dark mode.
- Update checks: the server looks for a newer GitHub release twice a day and the web UI shows "Update available"; `manyeyes version --check` for the CLI; `PULL=1` makes a restart update a systemd deployment.
- Resource limits: one memory and CPU ceiling for the whole container; up to 2 jobs and 15 reviewers at once.
