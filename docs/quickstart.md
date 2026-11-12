# Quickstart (local CLI)

You need Docker and a ChatGPT account with Codex access. Claude is optional.

## 1. Install

```bash
uv tool install git+https://github.com/Masboes/manyeyes@v1.2.0   # or: pipx install git+...
```

(`uv tool install manyeyes` works once the package is on PyPI.)

The CLI is small. The reviewers (Codex, Claude Code) and the sandbox run in the manyeyes Docker image, which the CLI pulls on first use.

## 2. Log the reviewers in

```bash
manyeyes login codex          # device login: open the URL, sign in, enter the code
manyeyes login claude         # optional: type /login, choose your Claude account, then /exit
```

The logins live in `~/.manyeyes/auth/codex` and `~/.manyeyes/auth/claude`. The container mounts these folders, so you log in once on your machine and every run uses it. Codex uses your ChatGPT login only, never an API key.

No Claude login? Then use Codex-only rosters, or every review reports the Claude reviewers as failed:

```bash
export MANYEYES_ROSTER=codex:10 MANYEYES_ROSTER_QUICK=codex:5 MANYEYES_ROSTER_DEEP=codex:10
```

## 3. Optional: the synthesis

The synthesis merges all reviews into one list. It is one call to the OpenAI API:

```bash
echo 'MANYEYES_SYNTH_OPENAI_API_KEY=sk-...' >> ~/.manyeyes/secrets.env
chmod 600 ~/.manyeyes/secrets.env
```

This key is used for the synthesis only. Reviewers never receive it. Without a key, you get every review but no synthesis.

## 4. Check

```bash
manyeyes doctor --probe
```

Every line should say `ok`. `--probe` sends a one-line prompt to each reviewer kind in your roster.

## 5. Review

```bash
cd your-repo
manyeyes review                       # current branch against origin/HEAD (or main)
manyeyes review --base develop        # another base
manyeyes review --roster codex:4      # fewer reviewers, faster
manyeyes review --roster codex:8,claude:2 --focus "check the migration"
manyeyes review --repo owner/name --pr 123   # clone and review a GitHub PR
manyeyes spar "Should this cache live in Redis or in-process? Argue both sides."
```

The synthesis prints to stdout; progress goes to stderr. `--full` adds every review, `--json` prints machine-readable output. Every run is saved: `manyeyes jobs` lists them, `manyeyes jobs show <id>` prints one.

## What reviewers see

Reviewers get a clean copy of your change: the tracked files at HEAD, plus your uncommitted changes and untracked files (up to 1 MB each). Ignored files, such as `.env`, never reach a reviewer. Each reviewer runs in a jail that shows it only that copy and its own login.

## Without Docker

On Linux with `bwrap` (bubblewrap), Codex and Claude Code installed:

```bash
manyeyes --no-container review
```
