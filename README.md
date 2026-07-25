# Claub

A Discord bot that gives Claude Code CLI agents persistent channels. Each agent gets its own Discord channel, a long-running streaming process, and optional cron schedules — all running in Docker.

## Architecture

```
Discord User
    │
    ▼
AssistantBot (discord.py)
    │
    ├─ Router ──► maps channel ID → agent name
    │
    ├─ Agent Processes (one per agent)
    │   └─ Long-running `claude --output-format stream-json`
    │   └─ Communicates via stdin/stdout JSON events
    │   └─ Lazy startup, supervised — auto-restarts on crash
    │
    ├─ Scheduler (APScheduler)
    │   └─ Agents manage their own cron schedules via MCP tools
    │
    └─ MCP Server (FastMCP, localhost:9400)
        └─ Schedule management tools exposed to agents
```

## Highlights

- **Persistent streaming** — each agent runs as a long-lived `stream-json` process, not one-shot invocations. Internal asyncio locks serialize communication on the pipe.
- **Lazy startup with supervision** — processes start on first message or scheduled trigger. A supervisor restarts dead processes; an idle reaper kills inactive ones after 10 minutes to prevent stale OAuth tokens.
- **Per-agent persistent Playwright sessions** — each agent has its own browser profile (cookies, localStorage, saved logins) that survives bot and host restarts. Log into a site once as one agent and stay logged in; other agents on the same site are cleanly isolated. Implemented via a host-side bridge daemon and generic `on_start` / `on_stop` lifecycle hooks in `agents.yaml` — the bot itself stays Playwright-agnostic.
- **Three-level agent configuration** — global rules (Level 1), agent identity (Level 2), and workspace living config that agents can self-modify (Level 3). See [`example/`](example/) for the full pattern.
- **File-based agent memory** — agents maintain their own memory in workspace directories with mandatory startup reads, write-time pruning, and bounded growth.
- **Human-like scheduling** — agents use one-shot schedules with natural time variation instead of rigid cron. A lognormal jitter model and beta-distributed skip probability make agent check-ins feel organic.
- **Embedded MCP server** — agents manage their own schedules via MCP tools. Density limits prevent runaway schedule creation (max 5 firings/24h, 30/7d).
- **Agent-authored skills via symlink** — agents can create their own skills and subagents without write access to `.claude/` (which would let them escalate permissions).
- **Docker isolation** — container provides filesystem boundaries. No macOS Seatbelt sandbox needed. See [`docs/sandbox-investigation/`](docs/sandbox-investigation/) for the security analysis.

## Included MCP Servers

| Server | Description |
|--------|-------------|
| [`mcps/git/`](mcps/git/) | Workspace-scoped git operations with path containment validation (15 tools) |
| [`mcps/leetcode-stats/`](mcps/leetcode-stats/) | LeetCode GraphQL API client — stats, cloud code, submissions (4 tools, with tests) |
| [`mcps/nextcloud/`](mcps/nextcloud/) | Nextcloud file sharing via WebDAV with TTL-based ephemeral cleanup (4 tools) |
| [`mcps/hass/`](mcps/hass/) | Home Assistant — a deliberately narrow allowlist of typed wrappers around specific entities and services (3 tools) |
| [`mcps/file-download/`](mcps/file-download/) | Least-privilege URL → workspace fetch: SSRF-blocked, size-capped, path-validated (1 tool) |

## Example Agents

The [`example/`](example/) directory contains a complete starter configuration with three agents:

- **Main** — general-purpose assistant with automatic action item capture
- **Journalist** — news agent with beat coverage, editorial standards, and daily brief memory
- **Shopping Assistant** — product research agent prioritizing quality and evidence-based recommendations

## Tech Stack

Python 3.12 · discord.py · Claude Code CLI · FastMCP · APScheduler · Docker

## Quick Start

```bash
# Copy example config to your instance directory
cp -r example/ ~/claub/

# Fill in your Discord channel IDs in ~/claub/config/agents.yaml
# Set your bot token
echo 'DISCORD_BOT_TOKEN=your-token' > .env
echo 'CLAUB_DATA_PATH=~/claub' >> .env

# Build and start
docker compose up -d

# Authenticate Claude CLI (one-time)
docker exec -it claude-claub-1 claude
```

See [`CLAUDE.md`](CLAUDE.md) for full documentation on configuration, deployment, and architecture.

## Channel Commands

Type these in an agent's Discord channel:

| Command | Effect |
|---|---|
| `/clear` | Stops the channel's agent process and clears its session — the next message starts fresh |
| `/clear {agent}` | Same, but targets a specific agent by name |
| `/stop` | Stops the channel's agent process but **keeps** the session |
| `/compact` | Compacts the session (summarizes history to free context, same session). Posts a start notice, then a completion notice |
| `/model` | Show the current model for this channel's agent |
| `/model {name}` | Switch models (`sonnet`, `opus`, or a full model ID). Persists across `/clear` until reset |
| `/model reset` | Revert to the `agents.yaml` / CLI default model |

## Tests

```bash
cd bot
uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py
```

162 tests covering process management, scheduling, density validation, MCP tools, message chunking, routing, session persistence, and the Playwright bridge.

## License

[MIT](LICENSE)
