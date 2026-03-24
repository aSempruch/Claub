FROM python:3.12-slim

# Install Node.js (for Claude CLI) and uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:$PATH"

# Install Claude CLI
RUN npm install -g @anthropic-ai/claude-code

# Install Python deps (deps layer cached separately from source)
WORKDIR /app/bot
COPY bot/pyproject.toml bot/uv.lock ./
COPY bot/src/ ./src/
RUN uv sync --no-dev --frozen

# Container layout
ENV CLAUB_HOME=/claub
RUN mkdir -p /claub/config /claub/data /claub/workspaces /claub/mcps

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
