# One image for both modes: the local CLI starts it per run, the server runs it
# long-lived (`manyeyes serve`). Reviewer CLIs are pinned; logins are mounted at
# run time, never baked in.
FROM python:3.12-slim-bookworm

ARG CODEX_VERSION=0.167.0
ARG CLAUDE_CODE_VERSION=2.1.322
ARG NODE_MAJOR=22

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# bubblewrap: the reviewer jail and codex's own sandbox. ripgrep: both CLIs
# search with it. tini: clean signal handling for the server and reviewers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bubblewrap ca-certificates curl git gnupg ripgrep tini \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g --omit=dev "@openai/codex@${CODEX_VERSION}" \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Repositories under review are mounted from the host and owned by another
# uid; without this git refuses them ("dubious ownership").
RUN git config --system --add safe.directory '*'

COPY pyproject.toml README.md /src/
COPY src /src/src
RUN pip install "/src[server]" && rm -rf /src

# The container runs as the caller's uid (local) or a fixed uid (server), so
# HOME must be writable by anyone.
RUN useradd --uid 10001 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin manyeyes \
    && mkdir -p /data && chown 10001:10001 /data

ENV HOME=/tmp \
    MANYEYES_IN_CONTAINER=1 \
    MANYEYES_DATA_DIR=/data \
    MANYEYES_SERVER_HOST=0.0.0.0

USER 10001
EXPOSE 8789
ENTRYPOINT ["tini", "--"]
CMD ["manyeyes", "serve"]
