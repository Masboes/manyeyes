# Self-hosting the server

The server gives a team one place to run reviews: an MCP endpoint for coding agents, a REST API, and a web UI. It is the same Docker image as the CLI, started with `manyeyes serve`.

## Trust model — read this first

- One deployment serves one team that trusts each other.
- Everyone on the allowlist can review every repository that the server's GitHub token can read, and can see every job and every review. People can cancel and delete only their own jobs.
- With the optional GitHub App, people on the allowlist can also start a review from a pull request comment, on the repositories where the App is installed. The App posts the synthesis on that pull request, where everyone who can read the pull request sees it.
- Reviewers send the code under review to their provider (OpenAI for Codex, Anthropic for Claude), under the terms of the logged-in accounts. The synthesis sends all reviews to the OpenAI API. Do not review code that may not go to these providers. You, the operator, decide the lawful basis and the provider agreements for that data.
- A prompt-injected reviewer can send the code it sees, and its own provider login, to any host: reviewers need the network. It cannot read the database, the other secrets or other jobs: each reviewer runs in a bubblewrap jail, and the server refuses to start if a self-test can see through the jail.
- The jail needs user namespaces, so the container runs with `seccomp`, `apparmor` and `systempaths` unconfined. The container runs as a normal user. If you need a stronger outer boundary, run it under gVisor or in a VM.

## 1. Data directory

```bash
mkdir -p /srv/manyeyes && cd /srv/manyeyes      # any path; it is mounted at /data
echo your-github-login > allowlist.txt          # one GitHub login per line
```

Log the reviewers in, into this directory (once):

```bash
docker run --rm -it --user "$(id -u):$(id -g)" -v "$PWD:/data" ghcr.io/masboes/manyeyes \
    manyeyes login codex
```

## 2. GitHub OAuth App

GitHub → Settings → Developer settings → OAuth Apps → New OAuth App.

| Field | Value |
|---|---|
| Homepage URL | `https://example.org/manyeyes/` |
| Redirect URIs | `https://example.org/manyeyes/auth/callback` and `https://example.org/manyeyes/mcp/auth/callback` |
| Allow wildcard matching | off |
| Expire user access tokens | off |

Put the ID and the secret in `secrets.env` (mode 600):

```
MANYEYES_GITHUB_CLIENT_ID=Ov23...
MANYEYES_GITHUB_CLIENT_SECRET=...
# Optional: the synthesis.
MANYEYES_SYNTH_OPENAI_API_KEY=sk-...
# Optional: private repositories (read-only).
MANYEYES_GITHUB_TOKEN=github_pat_...
```

Put a comment on its own line. A `#` after a value becomes part of the value.

For private repositories, a fine-grained personal access token with read-only `Contents` and `Pull requests` on the repositories you want reviewed is enough.

And the public settings in `server.env`:

```
MANYEYES_SERVER_PUBLIC_URLS=https://example.org/manyeyes
MANYEYES_SERVER_TRUSTED_PROXIES=172.16.0.0/12
```

`TRUSTED_PROXIES` lists the addresses whose `X-Forwarded-*` headers the server believes: your reverse proxy as the container sees it.

## 3. Run

With Docker Compose: see [`compose.yaml`](../compose.yaml).

With a `systemd --user` unit: see [`deploy/manyeyes.service`](../deploy/manyeyes.service) and [`deploy/manyeyes-docker`](../deploy/manyeyes-docker).

## 4. Reverse proxy

The server works at the root of a host or under a path prefix. Under a prefix, the proxy strips the prefix and sends `X-Forwarded-Prefix`. MCP sign-in also needs two discovery paths at the origin root. See [`deploy/nginx.conf.sample`](../deploy/nginx.conf.sample).

Never let the proxy redirect `/mcp`: clients drop the `Authorization` header on a redirect.

MCP sign-in uses the first URL in `MANYEYES_SERVER_PUBLIC_URLS` as the canonical MCP address. The web UI and API answer on every listed URL; tokens work on every URL.

Use `location ^~ /prefix/` in nginx, so a general regex rule for `*.js` or `*.css` files cannot take the UI's assets. If nginx runs in Docker with the config bind-mounted as a single file, edit that file in place or restart the container after the change.

## 5. Connect clients

- **Browser**: open the URL and sign in with GitHub.
- **Claude Code**: `claude mcp add --transport http manyeyes https://example.org/manyeyes/mcp`. The first call opens a GitHub sign-in.
- **Clients of the previous agent-swarm server**: keep them as they are. The tool names (`swarm_review`, `swarm_spar`, …) and arguments are unchanged, and the server tells clients it is "manyeyes (formerly agent-swarm)". Renaming a client's label (`claude mcp remove agent-swarm && claude mcp add … manyeyes …`) is optional; it changes the tool prefix from `mcp__agent-swarm__` to `mcp__manyeyes__`, so update permission lists that name the old prefix.
- **Scripts, CI, header-only clients**: create a token on the Tokens page, then send `Authorization: Bearer <token>`.

## Optional: reviews from a pull request comment

With a GitHub App, a person can start a review from a pull request. They tag the App in a comment, and the App answers on the pull request with the synthesis.

```
@your-app-name review
@your-app-name review quick
@your-app-name review deep: check the migrations
```

The tag is `@` plus the name of your App. Start the comment with the tag: a tag later in the comment, in code or in a quote (`> ...`) does nothing, so examples and replies cannot start a review by accident. After the tag comes an optional word `review`, an optional mode (`quick`, `full` or `deep`; the default is `full`), and then free text that the reviewers get as the focus.

**Who can start a review.** Both conditions must be true:

- The person is on the allowlist.
- GitHub marks the comment as written by the repository owner, a member of the organization that owns it, or a collaborator on it.

Other comments do nothing. GitHub can mark a member who hides their organization membership as a contributor; such a person must make the membership public or be added as a collaborator. Strangers on a public repository cannot start reviews. A collaborator who is not on the allowlist gets a short answer. The per-person limits apply to the person who wrote the comment.

**Requirement.** GitHub must reach the server over HTTPS. A server that only runs on an internal network cannot get the webhooks.

### Create the App

Your organization → Settings → Developer settings → GitHub Apps → New GitHub App.

| Field | Value |
|---|---|
| GitHub App name | The tag people type, for example `acme-manyeyes`. Choose a name that is not a GitHub user: a tag of a user's name also notifies that user. |
| Homepage URL | `https://example.org/manyeyes/` |
| Webhook URL | `https://example.org/manyeyes/github/webhook` |
| Webhook secret | A long random string (`openssl rand -hex 32`) |
| Repository permissions | Contents: read. Issues: read and write. Pull requests: read and write. Metadata: read (always on). |
| Subscribe to events | Issue comment |
| Where can this GitHub App be installed? | Only on this account |

Then:

1. Generate a private key on the App page. GitHub downloads a `.pem` file.
2. Install the App on the repositories that you want reviewed.
3. Give the server the App ID (on the App page), the key and the webhook secret, in `secrets.env` or in your secret store:

```
# The key file must be readable by the server only (mode 600).
MANYEYES_GITHUB_APP_ID=123456
MANYEYES_GITHUB_APP_PRIVATE_KEY_FILE=/data/github-app.pem
MANYEYES_GITHUB_WEBHOOK_SECRET=...
```

4. Restart the server. On the App's "Advanced" tab, find the first delivery (a `ping`) and click "Redeliver". GitHub sent it when you created the App, before the server knew the App. The redelivery shows a green tick when the server answers.

For each job, the server gets tokens for that one repository from the App: a read-only token to clone the code, and a token to post the answer. As with `MANYEYES_GITHUB_TOKEN`, no reviewer gets a token. A review started from a comment uses the App's tokens, not `MANYEYES_GITHUB_TOKEN`.

The answer holds the synthesis, or a short, fixed reason when the job failed. The details of a failure stay on the job page. When GitHub is down, the server tries again: twice at once, then at its next start, for jobs that ended in the last day. Set `MANYEYES_SERVER_PUBLIC_URLS`: the answer then links to the full result in the web UI.

If you rename the App, restart the server: it reads the App name once.

## Resource limits

Everything manyeyes runs — the server and every reviewer — lives in one container, so one Docker limit caps all of it:

- `MEMORY` / `CPUS` in the deploy env file (systemd unit), or `MANYEYES_MEMORY` / `MANYEYES_CPUS` with Compose.
- `MANYEYES_SERVER_MAX_CONCURRENT_JOBS` (default 2) and `MANYEYES_SERVER_MAX_CONCURRENT_REVIEWERS` (default 15) cap the work itself: reviewers beyond the cap wait for a slot, and the wait does not count toward their time limit.

Reviewers are API clients (the models run at the provider), so memory grows with the number of reviewers running at once, not with model size. When the memory limit is hit, the kernel stops one reviewer; that reviewer is reported as "killed by SIGKILL, most likely out of memory" and the job continues with the others. For the CLI, set `container.memory` and `container.cpus`.

## Many users (a company)

For a server shared by a team that should not be able to use up each other's quota:

- `MANYEYES_AUTH_ADMINS=alice,bob` names the admins. Admins can:
  - change settings, including the limits;
  - cancel and delete anyone's jobs, and read their results;
  - see the full audit log and everyone's usage on the Stats page;
  - run jobs without limits.

  Everyone signed in can read every job and its results (the server is for a team that trusts each other with code). Other people cancel and delete only their own jobs, and see only their own audit entries and usage.

  **Warning:** while the list is empty, everyone may change settings, so everyone can also remove the limits. The limits still apply, but they are not enforced against a person who wants to get around them. Name the admins on a shared server; the server logs a warning at start when limits are set and no admins are named.
- Per-person limits (0 = no limit; admins can change them on the Settings page):

  | Setting | What it caps |
  |---|---|
  | `limits.user_jobs_per_hour` | reviews and spars per person per hour |
  | `limits.user_jobs_per_day` | reviews and spars per person per day |
  | `limits.user_deep_jobs_per_day` | deep reviews per person per day (they cost about 5× the tokens) |
  | `limits.user_reviewer_runs_per_day` | reviewer runs per person per day, summed over rosters — the fairest token cap |
  | `limits.user_concurrent_jobs` | jobs per person running at once; others' jobs go first |

  A caller over a limit gets HTTP 429 (MCP: a tool error) that says which limit and when the next job is possible. Every admitted job counts, also one that later fails or is cancelled or deleted: usage is recorded when the job is accepted. Logins are compared without case.
- The Stats page shows jobs, reviewer runs and tokens per person.

A starting point for 20 people on one ChatGPT Pro and one Claude seat: `user_jobs_per_hour=6`, `user_reviewer_runs_per_day=200`, `user_deep_jobs_per_day=3`, `user_concurrent_jobs=1`, with `server.max_concurrent_jobs=3`.

## More than one login per provider

One Codex login is a single point of failure: when it hits its plan limit, every Codex reviewer fails. Add more logins under a name:

```bash
docker run --rm -it --user "$(id -u):$(id -g)" -v "$PWD:/data" ghcr.io/masboes/manyeyes \
    manyeyes login codex --name second
```

The first login lives in `auth/codex` and is called `default`; another one lives in `auth/codex@second`. Claude works the same way (`manyeyes login claude --name second`).

- **Choice.** Each reviewer gets the login with the most quota left, from the newest usage reading. A login with reviewers on it counts as fuller, so a review spreads over the logins.
- **Failover.** When a reviewer hits a rate limit or a failed login, it runs once more on another login, within the same reviewer time limit. A used-up plan ("usage limit") also puts the login to rest: until its full plan window resets, from the usage reading, else for `limits.login_rest_s` (30 minutes). A passing 429 or an overloaded provider gives no rest.
- **Dashboard.** The Stats page shows each login: its plan, how much of each window is used, whether it rests, and how many reviewers run on it. The people who may change settings (the admins, or everyone while `auth.admins` is empty) can switch a login off and on, and end a rest early. Each change is in the audit log with their GitHub login. Every signed-in person sees the login names and their state, never a credential.
- **Isolation.** A reviewer gets read-write access to its own login folder only.

**Warning:** use one login per paid seat. Several personal subscriptions used to pool quota for a team can break the provider's terms. Business plans (for example ChatGPT Business or Claude Team) give each seat its own login.

## Updating

The server checks GitHub for a new release twice a day and shows "Update available" in the web UI, with the release notes and the commands.

- **systemd unit**: set `IMAGE=ghcr.io/masboes/manyeyes:latest` and `PULL=1` in the deploy env file. Every start pulls the newest image, so an update is `systemctl --user restart manyeyes`. To pin a version, set `IMAGE=...:X.Y.Z` and `PULL=0`.
- **Docker Compose**: `docker compose pull && docker compose up -d`.
- **CLI**: `uv tool install --force git+https://github.com/Masboes/manyeyes@vX.Y.Z` (`manyeyes version --check` prints it). The CLI runs the image that matches its own version.

A restart ends running jobs (they are marked failed with "server restarted"); queued jobs run after it. Update when the job list is quiet.

## Operations

- Health: `GET /healthz`.
- Logs: the container's stdout.
- Restart: running jobs become `failed` with "server restarted"; queued jobs run.
- Review defaults (roster, models, limits): change them on the Settings page. A change applies to the next job. The default rosters include Claude: without a Claude login, set Codex-only rosters there (for example `codex:10`, quick `codex:5`).
- Plan usage: the Stats page shows how much of the Codex and Claude plans the reviewer logins have used, and when each window resets. The server reads it every 30 minutes (`usage.interval_min`, 0 turns it off). Reading spends no tokens: Codex answers from its app server, and Claude's `/usage` is a local command. The numbers cover all use of those logins, not only manyeyes.
- Retention: set `MANYEYES_RETENTION_DAYS` to delete old jobs. Telemetry stays unless `MANYEYES_RETENTION_KEEP_TELEMETRY=false`.
- Remove a person: delete the line from `allowlist.txt` (takes effect on their next request), then `manyeyes users forget <login>` to erase their jobs and tokens.
