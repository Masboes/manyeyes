# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Quota alerts. When a plan window of a day or longer (such as the weekly window) of a reviewer login reaches `alerts.usage_percent` (default 80), the server sends one alert to `alerts.url`: plain text with a `Title` header, the form that ntfy takes. Without `alerts.url`, nothing is sent. `manyeyes test-alert` sends a test alert.

### Notes
- Measured quota of Codex fast mode: in one week on a live server, 948 `gpt-6.2-sol` fast reviewers used 86% of a Pro week, about 0.09% each. A full review with 4 Codex reviewers is then about 0.36%, about 275 full reviews a week. This is in the 200–330 range that 1.6.2 estimated.

## [1.6.2] — 2026-11-18

### Added
- Setting `reviewers.codex.service_tier`: `standard` (default) or `fast`. `fast` turns on Codex fast mode. In a test on 6 PRs, a `gpt-6.2-sol` reviewer took 66 s instead of 129 s (median) and found the same issues; a full review then takes about 3 minutes, because the Claude reviewers become the slowest. OpenAI's pages give 2x to 2.5x for how much more fast mode counts against a ChatGPT plan's limits. With the estimate that a `gpt-6.2-sol` reviewer uses 1.5–2x the quota of a `gpt-6.1-sol` one (measured: 0.025% of a Pro week), one Pro account covers about 200–330 fast full reviews a week, against about 500–650 without fast mode. These numbers count the Codex reviewers only.

### Fixed
- The server skips a saved web UI setting that it does not accept (an unknown key or a bad value), and keeps the others. An admin can clear such a setting with `PUT /api/settings` and the value `null`. Before, one unknown key (for example after a downgrade to a version without it) made the server ignore all saved settings, including per-person limits, and blocked every change on the Settings page. Versions before 1.6.2 still have that behavior: before you downgrade below 1.6.2, reset `reviewers.codex.service_tier` on the Settings page, and remove it from `config.toml` and the environment.

## [1.6.1] — 2026-11-18

### Changed
- Synthesis model `gpt-6.2-sol` (was `gpt-6.1-sol`). On the same 12 inputs (6 PRs, 2 rounds), the synthesis took 21 s instead of 36 s (median), 36 s instead of 58 s at most. In the blind test for 1.6.0 it kept the same real issues as `gpt-6.1-sol`. The API price per token is the same.

## [1.6.0] — 2026-11-18

### Changed
- New defaults:
  - Codex model `gpt-6.2-sol` (was `gpt-6.1-sol`).
  - Full roster `codex:4,claude:2` (was `codex:8,claude:2`).
  - Quick roster `codex:2,claude:1` (was `codex:4,claude:1`).
  - Deep roster `codex:8,claude:3` and synthesis model `gpt-6.1-sol` do not change.
  - Codex-only rosters in the docs: `codex:6`, quick `codex:3`, deep `codex:10`.
- Why, from a blind test on 6 real PRs against the code:
  - 10 × `gpt-6.2-sol` found 34 real issues (11 high or medium). 10 × `gpt-6.1-sol` found 6 (4), in two runs a week apart.
  - From those reviews, the new full roster finds about 60% more high and medium issues than the old one, for about the same Codex quota (estimated from tokens).
  - From the same reviews, the synthesis of the new full roster kept 49 real issues, against 31 for the old roster.
  - Claude reviewers still find other issues, mostly low ones, and finish before `gpt-6.2-sol`.
  - As the synthesis model, `gpt-6.2-sol` scored the same as `gpt-6.1-sol`.
- A `gpt-6.2-sol` reviewer takes about twice as long. A full review takes about 4–5 minutes instead of about 3. A deep review of a large PR can take 15.
- A server with its own `roster` or model settings keeps them. If a Codex plan cannot use `gpt-6.2-sol`, set `reviewers.codex.model` to `gpt-6.1-sol`.
- Straggler rule: with 3 or more reviewers and `limits.quorum_fraction` below 1, quorum is at most all but one. Before, a 3- or 4-reviewer run needed every reviewer to succeed, so one hung reviewer held the run until its hard timeout. A reviewer of a provider with no finished review yet is still not cut off.

## [1.5.0] — 2026-11-16

### Added
- More than one reviewer login per provider (`manyeyes login codex --name second`, stored in `auth/codex@second`). Each reviewer gets the login with the most quota left. On a rate limit or a failed login, the reviewer runs once more on another login, within the same time limit. A used-up plan puts the login to rest until its full plan window resets (`limits.login_rest_s` when unknown). The Stats page shows usage per login; the people who may change settings can switch a login off and on and end a rest (`POST /api/logins/{kind}/{name}`, audit-logged). Reviewer runs record their login.
- Pull request context. A review of a pull request now gives the reviewers its title, description and comments, as quoted JSON data, not instructions. Only comments from the PR's author and the repository's owners, members and collaborators count; bots are left out. The newest comments win when there are many. Setting: `review.pr_context`, on by default: after the upgrade this text goes to the reviewer providers, like the code. A branch review is unchanged.
- A note at the top of the result when `MANYEYES.md` or the pull request comments could not be read. Before, only the server log showed it. The note also appears in the GitHub comment.

## [1.4.0] — 2026-11-16

### Added
- Review standards per repository. A `MANYEYES.md` file in the root of a repository, or in `.github/`, goes to every reviewer and to the synthesis as the repository's review standards. manyeyes reads it from the base branch, so a pull request cannot change the rules it is judged by. See "Review standards for your repository" in the README.

## [1.3.0] — 2026-11-16

### Added
- Reviews from a pull request comment. With an optional GitHub App (`github_app.id`, `github_app.private_key`, `github_app.webhook_secret`), a comment that starts with a tag of the App (`@<app-name> review quick: focus text`) queues a review of that pull request. The App reacts with 👀, and posts the synthesis, or the reason for a failure, on the pull request when the job ends. Only people on the allowlist who are also owner, member or collaborator of the repository can start a review, and the per-person limits apply to them. The App's tokens are limited to the one repository and never reach a reviewer. See the self-hosting guide.

### Changed
- A job that runs when the server stops now ends as `failed` with "the server stopped while this job ran", not as `cancelled`. A person's cancel stays `cancelled`.

### Fixed
- The `secrets.env` example in the self-hosting guide put comments after the values. The env-file reader keeps such a comment as part of the value. The comments are now on their own lines.

## [1.2.0] — 2026-11-12

### Changed
- The synthesis model no longer gets told which reviewer is Codex or Claude: reviews get anonymous labels in random order, without model or vendor names, to reduce vendor bias (a review's own wording can still give hints). The model returns the merged findings as JSON, and manyeyes places each finding by agreement itself, from the reviewers the model credits, so the model no longer counts families or reviewers. A finding the model credits to no known reviewer is shown under "Unlinked findings", not dropped.
- Low-severity points that only one reviewer raised move to a short "Minor notes" list at the end. Each keeps its full text on one line. In a blind test of the first version of this change on 6 real PRs, the content matched the previous synthesis exactly (same real, false and dropped counts), and the judge preferred the new layout in all 6.

### Fixed
- A Codex reviewer whose own sandbox could not start (for example its helper is missing because the data directory is under /tmp) no longer counts as a successful review: no command ran, so its text only said that it could not see the code. It now fails and names the cause.
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
