# Installing manyeyes — instructions for a coding agent

This page is written for an AI coding agent (Claude Code, Codex, …) that sets manyeyes up for a person. Follow the steps in order. Each step says how to check it worked. Steps marked **HUMAN** need the person: stop, tell them exactly what to do, and wait.

Pick the path first:

- **Local CLI** (most people): reviews run on this machine, from the terminal or from your shell tool. No server, no MCP. Steps 1–6.
- **Server** (a team, MCP clients, web UI): follow [self-hosting.md](self-hosting.md) instead.

## 1. Check the prerequisites

```bash
docker info --format '{{.ServerVersion}}'   # Docker must run, and this user must be allowed to use it
uv --version || pipx --version              # one of the two
git --version
```

If `docker info` says "permission denied", the user is not in the `docker` group: ask the person to fix that (**HUMAN**: `sudo usermod -aG docker $USER`, then log out and in).

## 2. Install the CLI

```bash
uv tool install git+https://github.com/Masboes/manyeyes@v1.2.0    # or: pipx install git+...
manyeyes --version                                                  # prints: manyeyes 1.2.0
```

Check: `docker pull ghcr.io/masboes/manyeyes:1.2.0` succeeds.

## 3. Log the reviewers in — HUMAN

These are interactive logins; you cannot do them for the person. Ask them to run, in a terminal:

```bash
manyeyes login codex     # device login: open the URL it prints, sign in with ChatGPT, enter the code
manyeyes login claude    # optional: type /login, pick the Claude account, finish in the browser, then /exit
```

Codex is required. Claude is optional, but the default rosters include it: without a Claude login, add `export MANYEYES_ROSTER=codex:10 MANYEYES_ROSTER_QUICK=codex:5 MANYEYES_ROSTER_DEEP=codex:10` to the person's shell profile. The logins are stored in `~/.manyeyes/auth/` and reused by every run.

## 4. The synthesis key (optional)

The synthesis merges all reviews into one ranked list; it needs an OpenAI API key. Without it, every review is still returned. Ask the person for the key; never echo it back:

```bash
umask 077 && echo 'MANYEYES_SYNTH_OPENAI_API_KEY=<key>' >> ~/.manyeyes/secrets.env
```

The key is used only for the synthesis; reviewers never receive it.

## 5. Verify

```bash
manyeyes doctor --probe
```

Every line must start with `ok` (lines with `--` are optional parts that are not set up). `--probe` sends a one-line prompt to each reviewer kind and costs a few seconds.

## 6. Use it

From inside any git repository:

```bash
manyeyes review --json > /tmp/manyeyes-review.json     # 3–15 minutes; run it in the background
```

- Read `.synthesis` from the JSON: findings grouped by how many reviewers agreed, each with `path:line` and a severity.
- Exit code `0`: at least one review came back. `1`: failed (see stderr). `2`: every reviewer was rate-limited; try later.
- `--roster codex:4` is faster; `--base <ref>` changes the base; `--focus "<text>"` steers the reviewers.
- Act on findings several reviewers agree on; judge single-reviewer findings yourself, against the code.

Suggested lines for the person's `CLAUDE.md`:

```markdown
## Code review
Before opening a PR, run `manyeyes review --json` (background, 3–15 min). Fix findings that
several reviewers agree on; judge single-reviewer findings on their merits.
```

## Updating

```bash
manyeyes version --check        # prints the install command for a newer release
uv tool install --force git+https://github.com/Masboes/manyeyes@vX.Y.Z
```
