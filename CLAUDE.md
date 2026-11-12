# manyeyes — notes for coding agents

To **install or use** manyeyes (not develop it), read `docs/install-for-agents.md` instead.

Multi-reviewer code review: one engine (`src/manyeyes/engine`), a CLI and a server. Read `docs/design.md` before structural changes.

## Commands

```bash
uv sync --all-extras          # Python 3.12 venv with server extras and dev tools
uv run pytest -q              # fast; uses a fake codex, no network
uv run ruff check src tests
uv run manyeyes --no-container doctor --probe   # real logins needed
docker build -f docker/Dockerfile -t manyeyes:dev .
```

## Rules that must not break

- The OpenAI API key (`synth.api_key`) is for the synthesis only. Never pass any key or token to a reviewer process; reviewer environments are built from an allowlist (`engine/reviewers/base.py`).
- Every reviewer runs in the jail (`engine/jail.py`). Never bind a path that contains the data directory. Keep the jail self-test passing.
- Codex authenticates with a ChatGPT login (mounted `CODEX_HOME`) only.
- MCP tool names, argument names and response keys stay compatible (`tests/fixtures/mcp_v1_tools.json`). Additive changes only.
- No hardcoded hosts, users or paths. New settings go into `KEYS` in `config.py`; then regenerate `docs/configuration.md` (`manyeyes config --reference`).
- Schema changes are new numbered files in `store/migrations/`.

## Style

Conventional commits, lowercase imperative. Docs in plain, short English.
