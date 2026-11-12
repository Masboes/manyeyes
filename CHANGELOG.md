# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- The synthesis model no longer gets told which reviewer is Codex or Claude: reviews get anonymous labels in random order, without model or vendor names, to reduce vendor bias (a review's own wording can still give hints). The model returns the merged findings as JSON, and manyeyes places each finding by agreement itself, from the reviewers the model credits, so the model no longer counts families or reviewers. A finding the model credits to no known reviewer is shown under "Unlinked findings", not dropped.
- Low-severity points that only one reviewer raised move to a short "Minor notes" list at the end. Each keeps its full text on one line. In a blind test of the first version of this change on 6 real PRs, the content matched the previous synthesis exactly (same real, false and dropped counts), and the judge preferred the new layout in all 6.

### Fixed
- A Codex reviewer that could not run any command (its sandbox helper missing, which happens when the data directory is under /tmp) no longer counts as a successful review. Its text only said that it could not see the code; it now fails with that reason.
- A review whose `branch` or `base` is a commit SHA (7 to 40 hex characters) no longer fails with "couldn't find remote ref". A branch with that name still wins; a short SHA is looked up on all branches. Context repositories (`owner/name@ref`) accept a commit SHA too.
- A GitHub API call that fails with a 5xx or a connection error is retried twice (after 1 s and 3 s); a 4xx is not retried. A timeout is not retried. A `git fetch` or `ls-remote` that fails with a server or network error is retried once.
- The straggler kill no longer removes a whole reviewer family. When the grace ends, a reviewer whose family (codex, claude) has no successful review yet keeps running until one of its family succeeds, it reaches its own timeout, or the job needs the remaining time for the synthesis. This keeps the cross-family view in the synthesis.

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
