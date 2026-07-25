FROM python:3.12-slim

# Install Node.js (for Claude CLI) and uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git \
    texlive-latex-base texlive-latex-recommended texlive-fonts-recommended && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:$PATH"

# Install Claude CLI (pinned — bump deliberately, not via floating "latest")
RUN npm install -g @anthropic-ai/claude-code@2.1.219

# Notion MCP server (pinned) — used by agents via per-agent .mcp.json
RUN npm install -g @notionhq/notion-mcp-server@2.4.1

# Install Python deps (deps layer cached separately from source)
WORKDIR /app/bot
COPY bot/pyproject.toml bot/uv.lock ./
COPY bot/src/ ./src/
RUN uv sync --no-dev --frozen

# Container layout
ENV CLAUB_HOME=/claub
RUN mkdir -p /claub/config /claub/data /claub/workspaces /claub/mcps

# Bake repo MCP servers into image
COPY mcps/ /app/mcps/

# Lock down git: neutralize hooks and block network protocols so agents can use
# git for local versioning without code-execution or network escape hatches.
# This file is root-owned; agents run as root inside the container but cannot
# override it via `git -c` because the Bash allowlist rejects that form.
RUN printf '%s\n' \
    '[core]' \
    '    hooksPath = /dev/null' \
    '[protocol]' \
    '    allow = never' \
    '[safe]' \
    '    directory = *' \
    '[user]' \
    '    name = Claub Agent' \
    '    email = agent@claub.local' \
    > /etc/gitconfig

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
