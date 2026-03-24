# Claub — Discord Bot for Claude Code CLI

> **Note for the dev-time Claude Code instance:** This file documents the **Claub project** — a bot that spawns its own Claude CLI processes at runtime. References to "agents", "permissions", "MCP configs", and "sessions" below describe how the **bot's** Claude processes are configured, **not** how you (the Claude Code instance helping develop this project) should behave. Do not adopt the bot's permission settings, or agent prompts as your own.

A Discord bot that bridges Discord channels to Claude Code CLI sessions. Each agent gets its own channel and a persistent streaming process, with optional cron schedules. Runs in Docker.

## Quick Start

```bash
# Create your instance directory with config, data, workspaces, and mcps
mkdir -p ~/docker/claub/{config/agents,data,workspaces,mcps}

# Add your config files (agents.yaml, CLAUDE.md, settings.json, mcp.json, agents/*.md)
# See Configuration section below

# Set your Discord bot token
echo 'DISCORD_BOT_TOKEN=your-token' > .env

# Build and start
docker compose up -d

# Authenticate Claude CLI (one-time, credentials persist in named volume)
docker exec -it claude-claub-1 claude
# Follow login flow, then exit
```

## Architecture

```
Discord User
    │
    ▼
AssistantBot (discord.py)  [runs in Docker container]
    │
    ├─ Router ──► maps channel ID → agent name
    │
    ├─ Agent Processes (AgentProcess, one per agent)
    │   └─ Long-running `claude --agent {name} --input-format stream-json --output-format stream-json`
    │   └─ Communicates via stdin/stdout JSON events
    │   └─ Started lazily on first message, supervised — auto-restarts on crash
    │
    ├─ Scheduler (APScheduler)
    │   └─ Fires any agent on cron schedules (including main)
    │   └─ Loads from /claub/data/schedules.json
    │
    └─ MCP Server (FastMCP, localhost:9400)
        └─ Agents manage their own schedules via list/create/delete tools
```

Claude CLI uses its native `~/.claude/` path inside the container. Config files are copied from `/claub/config/` into `~/.claude/` at container startup by the entrypoint script. Credentials persist in a named Docker volume (`claude-home`).

## Project Structure

```
bot/                              # Python package (discord bot)
  pyproject.toml                  # deps: discord.py, apscheduler, pyyaml, python-dotenv
  src/claude_assistant/
    main.py                       # Entry point — resolves paths, starts bot
    config.py                     # Parses agents.yaml → AssistantConfig dataclasses
    discord_bot.py                # AssistantBot — routing, message handling, lifecycle
    claude_process.py             # AgentProcess — persistent stream-json process per agent
    router.py                     # Channel ID → agent name mapping
    scheduler.py                  # APScheduler cron wrapper
    session.py                    # SessionStore — atomic JSON persistence of session IDs
    chunker.py                    # Splits long messages for Discord's 2000-char limit
    mcp_server.py                 # FastMCP HTTP server — schedule management tools for agents
    schedule_store.py             # ScheduleStore — atomic JSON persistence of schedules
  tests/                          # pytest + pytest-asyncio

Dockerfile                        # Python 3.12 + Node.js + uv + Claude CLI
entrypoint.sh                     # Copies config into ~/.claude/, starts bot
docker-compose.yml                # Service definition with volumes — ALL docker compose commands run from this project root
.dockerignore                     # Build context exclusions

scripts/                          # Legacy service management (launchd, pre-Docker)
  run.sh                          # Wrapper for launchd
  ctl.sh                          # Service control
  com.asempruch.claub.plist       # launchd plist template

/claub/                           # Instance root inside container (bind-mounted from host)
  config/                         # All user-editable configuration
    agents.yaml                   # Agent definitions, channel IDs
    mcp.json                      # Shared MCP server config (e.g. Playwright)
    settings.json                 # Claude tool permissions (allow list)
    CLAUDE.md                     # Global agent guidelines (applies to all agents)
    agents/                       # Agent system prompts and per-agent MCP configs
      main.md                     # Main agent system prompt
      journalist.md               # Journalist agent system prompt
      journalist.mcp.json         # Optional per-agent MCP config
  mcps/                           # Custom MCP servers (one dir per server)
    leetcode-stats/               # Example: LeetCode GraphQL API wrapper
      pyproject.toml
      server.py
  workspaces/                     # Runtime scratch dirs per agent (auto-created)
  data/                           # sessions.json — session ID persistence
                                  # schedules.json — dynamic schedule persistence
```

## Configuration

All configuration lives in `/claub/config/` inside the container (bind-mounted from the host, e.g. `~/docker/claub/config/`). The entrypoint copies agent prompts, `settings.json`, and `CLAUDE.md` into `~/.claude/` at container startup so Claude CLI finds them in the expected locations. The base path is set via `CLAUB_HOME=/claub` (configurable).

### agents.yaml

```yaml
agents:
  main:
    channel_id: "123456789"       # Required — every agent needs a channel
    display_name: "Main"          # Optional — display name for the agent
    avatar_url: "https://..."     # Optional — avatar URL for the agent
    allowed_tools_additional: []  # Optional — additional tools beyond defaults
  journalist:
    channel_id: "987654321"
```

An `agents.main` entry is required. Schedules are managed dynamically via the MCP server (see Schedule Management below) — not in `agents.yaml`.

### Schedule Management

Schedules are managed dynamically at runtime via an embedded MCP server. Agents can create, list, and delete their own cron schedules using MCP tools (`mcp__schedules__list_schedules`, `mcp__schedules__create_schedule`, `mcp__schedules__delete_schedule`).

Schedule data is persisted to `/claub/data/schedules.json` (machine-managed, not hand-edited):

```json
{
  "agent_name": [
    {
      "id": "a1b2c3",
      "cron": "0 9 * * *",
      "prompt": "Do the thing",
      "one_shot": false
    }
  ]
}
```

- **`one_shot`**: If true, the schedule fires once and is then automatically deleted.
- The bot's APScheduler syncs from this file on startup and immediately on mutations.
- The MCP server runs on `127.0.0.1:9400` (configurable via `CLAUB_MCP_PORT` env var).
- Agent name is passed via the `X-Agent-Name` HTTP header, resolved from the `${CLAUB_AGENT_NAME}` env var set in each agent's process.
- Schedule changes trigger a notification in the agent's Discord channel.

### Agent Context — The Three-Level Split

Agent behavior is configured at three levels. Getting this split right matters — putting the wrong thing at the wrong level leads to either rigid agents that can't adapt or unstable agents that lose their identity.

**Level 1: Global** (`/claub/config/CLAUDE.md`) — Rules that apply to **all** agents: safety, Discord behavior, workspace usage, memory protocol. Copied into `~/.claude/CLAUDE.md` at container startup.

**Level 2: Agent identity** (`/claub/config/agents/{name}.md`) — The agent's **stable core**: personality, communication style, role definition, capabilities, and memory structure. This is **who the agent is** — it should rarely change. Think of it as the agent's DNA. Does NOT include:
- Specific targets, criteria, or parameters that the user might adjust (those go in workspace CLAUDE.md)
- Scheduled task instructions (those go in the cron `prompt` in agents.yaml)
- Runtime state or progress tracking (that goes in memory)

**Level 3: Living config** (`/claub/workspaces/{name}/CLAUDE.md`) — The **fluid details** the agent works with day-to-day: current targets, search criteria, focus areas, topic lists, thresholds. The agent can self-modify this file when the user asks to shift focus (e.g., "stop covering crypto", "raise my comp target to $180k"). The agent `.md` should reference this file and tell the agent it exists.

**Rule of thumb:** If you'd change it by editing the agent's personality, it's Level 2. If you'd change it by telling the agent "from now on, focus on X instead of Y", it's Level 3.

Each agent also gets a workspace directory at `/claub/workspaces/{name}/`. These are **runtime scratch directories** created automatically. Agents can write files or store data there as they see fit.

### Adding a New Agent

1. Add entry to `/claub/config/agents.yaml` with `channel_id` and optional `schedule` (cron prompts should contain the task instructions, not the agent `.md`)
2. Create `/claub/config/agents/{name}.md` — stable identity only (personality, style, capabilities, memory structure). Must include YAML frontmatter with `name` and `description`. Reference the workspace CLAUDE.md so the agent knows to read it.
3. Create `/claub/workspaces/{name}/CLAUDE.md` — fluid details the agent works with (targets, criteria, focus areas). The agent can self-modify this file.
4. Optionally create `/claub/config/agents/{name}.mcp.json` for agent-specific MCP servers
5. Restart the container: `docker compose restart`

### MCP Servers

MCP servers give agents access to external tools (APIs, browsers, etc.) without granting arbitrary code execution. Two levels:

**Shared** (`/claub/config/mcp.json`) — passed to all agents:

```json
{
  "mcpServers": {
    "playwright": {
      "type": "http",
      "url": "http://host.docker.internal:3846/mcp"
    }
  }
}
```

**Per-agent** (`/claub/config/agents/{name}.mcp.json`) — additional MCPs for a specific agent only. Both files are passed via `--mcp-config` when the agent runs.

#### Playwright MCP (Host-Side)

Playwright runs on the **host** as a launchd service (not inside the container — it needs a browser). The container connects to it via `host.docker.internal`:

```bash
scripts/playwright-mcp.sh install   # Install launchd plist
scripts/playwright-mcp.sh start     # Start the service
scripts/playwright-mcp.sh status    # Check status
scripts/playwright-mcp.sh logs      # View logs
```

The service runs `npx @playwright/mcp@latest --port 3846 --host 127.0.0.1 --allowed-hosts host.docker.internal:3846` and auto-starts on reboot.

#### Custom MCP Servers

Custom MCP servers live in `/claub/mcps/` (bind-mounted from host). The entrypoint automatically runs `uv sync` for any subdirectory containing a `pyproject.toml` on every container start, so Python-based MCPs are always ready.

Use the `/build-mcp-server` skill for the full guide on building, wiring, and testing custom MCP servers for agents.

### Permissions (/claub/config/settings.json)

Tool allow-list for all agents:

```json
{
  "permissions": {
    "allow": ["mcp__playwright__*", "mcp__schedules__*", "WebFetch", "WebSearch"],
    "additionalDirectories": ["/tmp"]
  }
}
```

The container provides isolation — no macOS Seatbelt sandbox needed. Agents run with `--permission-mode acceptEdits` and are constrained by the container's filesystem boundaries.

## Message Flow

1. User sends message in Discord
2. Router checks channel ID → agent name (or ignores if unknown channel)
3. Bot gets or starts the agent's persistent stream-json process (lazy startup)
4. Message sent to process via stdin, response read from stdout events until `type: result`
5. On process error: restart and retry once. On auth error: notify user.
6. Response chunked at newline boundaries (max 2000 chars) and sent back

### Commands

- `/clear` — stops agent process for current channel, clears session (next message starts fresh)
- `/clear {agent}` — same, but targets a specific agent by name

## Session Persistence

Session IDs are stored in `/claub/data/sessions.json` (agent name → session UUID). On startup or message send, the bot passes `--resume {session_id}` to maintain conversation context. If resume fails, the session is cleared and a fresh one starts.

## Service Management

The bot runs as a Docker container via `docker compose`. It auto-restarts on crash (`restart: unless-stopped`).

```bash
docker compose up -d          # Start (build if needed)
docker compose up -d --build  # Rebuild image and start
docker compose restart        # Restart (picks up config changes)
docker compose stop           # Stop the container
docker compose logs --tail 50 # Last 50 lines of logs
docker compose logs -f        # Tail logs live
```

To access a shell inside the container:

```bash
docker exec -it claude-claub-1 bash
```

## Deploying Changes

**Bot code changes** (anything in `bot/`): rebuild the image and restart:

```bash
docker compose up -d --build
```

**Config changes** (agent prompts, settings.json, CLAUDE.md, mcp.json): just restart — the entrypoint re-copies config at startup:

```bash
docker compose restart
```

## Authentication

Credentials persist in the `claude-home` named Docker volume. Authenticate once after first deploy:

```bash
docker exec -it claude-claub-1 claude
# Follow login flow, then exit
```

If auth expires, repeat the same command.

## Development

### Running Tests

```bash
cd bot
uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py
```

Integration tests (require real Claude CLI auth):
```bash
CLAUDE_INTEGRATION_TEST=1 uv run --extra dev pytest tests/test_integration.py -v
```

### Testing Claude CLI Directly

To test how agents behave inside the container:

```bash
# Quick auth check
docker exec claude-claub-1 claude -p "say hello"
```

To run an agent interactively (same config, workspace, and permissions as the bot):

```bash
# Interactive agent session (e.g. main)
docker exec -it claude-claub-1 bash -c 'cd /claub/workspaces/main && claude --agent main --permission-mode acceptEdits --mcp-config /claub/config/mcp.json --no-session-persistence'
```

### Key Design Decisions

- **All-streaming**: Every agent (including main) runs as a persistent `AgentProcess` using stream-json I/O. No one-shot processes.
- **Lazy startup**: Agent processes start on first message or scheduled trigger, not eagerly at boot. Supervisor restarts dead processes.
- **Stream lock inside AgentProcess**: Internal asyncio lock serializes send/receive on the stream-json pipe. No bot-level locks needed.
- **Lifecycle lock**: Separate lock in AgentProcess protects start/stop/restart transitions from racing with the supervisor.
- **Idle reaper**: Background task kills agent processes after 10 minutes of inactivity. Prevents stale processes from holding expiring OAuth tokens, which caused auth races when multiple long-lived processes shared the same credentials file. A `_reaped` set prevents the supervisor from immediately restarting intentionally killed processes.
- **Global agent lock**: `_agent_lock` in `AssistantBot` serializes all agent API calls so only one agent talks to Claude at a time, preventing credential/token races.
- **Docker-first**: The container runs Claude CLI with its native `~/.claude/` path. Config is copied from `/claub/config/` into `~/.claude/` at startup by the entrypoint. No HOME override hack.
- **acceptEdits permission mode**: All processes run with `--permission-mode acceptEdits`
- **Entrypoint config copy**: `entrypoint.sh` copies agents/, settings.json, CLAUDE.md from `/claub/config/` into `~/.claude/` on every container start. No symlinks — plain copies.
- **Separated instance from source**: Bot code is baked into the image; user config and runtime state are bind-mounted from the host into `/claub/` (configurable via `CLAUB_HOME` env var).
- **Embedded MCP server for schedules**: FastMCP HTTP server runs inside the bot process on localhost. Agents manage their own cron schedules via MCP tools. Changes take effect immediately — no file polling or restart needed. One-shot schedules are deleted from persistence before execution to prevent duplicate firing on crash recovery.

### Agent Memory System

Agents are long-running assistants (not dev tools) that may operate for months. Memory is file-based, stored in each agent's workspace at `/claub/workspaces/{name}/memory/`. The system is designed to stay useful over time without unbounded growth.

Memory guidelines live within the broader agent configuration system (see "Agent Context" above). `/claub/config/CLAUDE.md` defines global rules that apply to all agents — safety, Discord behavior, workspace usage, and the memory protocol. `/claub/config/agents/{name}.md` defines each agent's role, personality, task instructions, and agent-specific memory structure. When editing memory guidelines, preserve this split: global rules enforce mechanical discipline; agent-specific rules define *what* to remember and *how long* to keep it.

**Design principles to enforce:**
- **Mandatory startup read**: Every agent reads `memory/index.md` before doing any work, every session. No conditional checks.
- **Write-time pruning**: Every memory write must include a review of the index. Remove outdated entries, merge overlapping ones. This is the primary defense against memory bloat.
- **Compaction awareness**: Claude's context compaction can silently drop conversation history. Anything important must be written to memory files promptly — not deferred to end-of-session.
- **Current state wins**: When memory conflicts with observed reality, trust what's there now and update the memory. Stale entries that persist cause compounding errors.
- **Bounded growth**: Index should stay under ~50 entries. Agent-specific rules should define retention periods (e.g., journalist keeps 7 days of briefs). Memory without a pruning policy will degrade agent performance over time.

### Dependencies

Core: `discord.py`, `apscheduler`, `pyyaml`, `python-dotenv`, `fastmcp`, `uvicorn`
Dev: `pytest`, `pytest-asyncio`
Build: `hatchling`

## Branching

Development happens on feature branches. The `main` branch is the stable trunk.
