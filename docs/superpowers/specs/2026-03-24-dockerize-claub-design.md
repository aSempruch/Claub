# Dockerize Claub

**Date:** 2026-03-24
**Status:** Approved
**Branch:** feature/dockerize

## Goal

Containerize the Claub Discord bot so it runs without the `~/.claub/home/` HOME override hack. In Docker, Claude CLI uses its native `~/.claude/` path directly. The container is single-purpose — its only job is running Claub. Docker is the primary deployment target; bare-metal is not a supported path.

## Container Layout

```
/app/                        # Bot source (baked into image at build time)
/claub/                      # Instance root — single or multi-mount from host
  config/                    # agents.yaml, CLAUDE.md, settings.json, mcp.json, agents/
  data/                      # sessions.json, schedules.json
  workspaces/                # Agent scratch dirs (one per agent, auto-created)
  mcps/                      # Custom MCP servers (optional)
~/.claude/                   # Native Claude CLI home (named volume for credential persistence)
```

### Mount Strategies

**Single mount:** Bind one host directory to `/claub/` containing `config/`, `data/`, `workspaces/`, `mcps/` subdirs.

**Multi mount:** Bind separate host directories to `/claub/config/`, `/claub/data/`, `/claub/workspaces/`, `/claub/mcps/`.

Credentials persist in a named Docker volume mapped to `~/.claude/` (e.g., `claude-home:/root/.claude`). User authenticates once via `docker exec -it <container> claude` and credentials survive rebuilds.

## Image (Dockerfile)

**Base:** `python:3.12-slim`

**Build steps:**

1. Install Node.js (Claude CLI is npm-based) and `uv` package manager
2. Install Claude CLI globally via npm (`@anthropic-ai/claude-code`)
3. Copy `bot/` source and `uv.lock` into `/app`
4. Install Python dependencies with `uv sync`

**Expected image size:** ~500MB+ due to Node.js + Claude CLI + Python + uv. Acceptable for a single-purpose container.

**Default env vars:**

- `CLAUB_HOME=/claub`

**Entrypoint:** `entrypoint.sh` (see below)

### entrypoint.sh

Runs before the bot on every container start:

1. Copy config files from `/claub/config/` into `~/.claude/`:
   - `agents/` directory (recursive copy)
   - `settings.json`
   - `CLAUDE.md`
   - Skip `.credentials.json` and other existing files in the volume (preserve credentials)
2. Ensure `/claub/data/`, `/claub/workspaces/` exist (create if missing)
3. `exec uv run claude-assistant` (exec ensures bot is PID 1 for signal handling)

No symlinks — plain copies. Config changes require container restart, which is needed anyway since the bot reads config at startup.

### .dockerignore

Exclude: `.git/`, `scripts/`, `tests/`, `__pycache__/`, `*.pyc`, `.envrc`, `docs/`, `.gitignore`. Include `uv.lock` for reproducible builds.

## docker-compose.yml

```yaml
services:
  claub:
    build: .
    environment:
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
    volumes:
      - claude-home:/root/.claude
      - ./instance/config:/claub/config
      - ./instance/data:/claub/data
      - ./instance/workspaces:/claub/workspaces
      - ./instance/mcps:/claub/mcps
    healthcheck:
      test: ["CMD", "pgrep", "-f", "claude-assistant"]
      interval: 30s
      timeout: 5s
      retries: 3

volumes:
  claude-home:
```

Or single mount variant: `./instance:/claub` (replaces the four bind mounts).

## Bot Code Changes

### main.py

Update path resolution:

- `{CLAUB_HOME}/config/` (unchanged)
- `{CLAUB_HOME}/home/` — remove entirely, no longer exists
- `{CLAUB_HOME}/workspaces/` (unchanged)
- `{CLAUB_HOME}/data/` (unchanged)

Remove `home_dir` from the paths passed to `AssistantBot`.

### claude_process.py

- Remove the `HOME` env var override entirely. Claude CLI uses the real home.
- Remove `home_dir` parameter from `AgentProcess`.
- MCP config paths still resolve from `CLAUB_HOME`.

### discord_bot.py

- Remove `home_dir` from constructor and from `AgentProcess` instantiation.

## Sandbox Configuration

The current `settings.json` configures macOS Seatbelt sandboxing which does not exist on Linux. Claude CLI gracefully ignores sandbox config on non-macOS platforms, so the same `settings.json` can be used. No Docker-specific settings file needed.

## MCP Server Networking

**Embedded schedule MCP:** Runs inside the container on `127.0.0.1:9400`. Works as-is since Claude CLI processes and the MCP server are all in the same container.

**Custom MCPs (mounted in `/claub/mcps/`):** These run as child processes spawned by Claude CLI inside the container. MCP commands in `mcp.json` or per-agent `.mcp.json` files must reference paths and binaries available inside the container. For MCPs that need `npx`, Node.js is available (installed for Claude CLI). For MCPs needing other runtimes, those must be installed in the image or the MCP run as a separate container/sidecar.

## What Doesn't Change

- `config.py` — still parses agents.yaml identically
- `scheduler.py`, `schedule_store.py`, `mcp_server.py` — unchanged
- `router.py`, `chunker.py`, `session.py` — unchanged
- All config file formats (agents.yaml, settings.json, mcp.json, agent prompts) — unchanged
- Schedule MCP server (baked into image as part of the bot)

## Authentication

Manual for now. After first `docker compose up`:

```bash
docker exec -it <container> claude
# Follow login flow, then exit
# Credentials persist in claude-home volume
```
