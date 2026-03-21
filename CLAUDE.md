# Claub — Discord Bot for Claude Code CLI

> **Note for the dev-time Claude Code instance:** This file documents the **Claub project** — a bot that spawns its own Claude CLI processes at runtime. References to "agents", "HOME", "permissions", "MCP configs", and "sessions" below describe how the **bot's** Claude processes are configured, **not** how you (the Claude Code instance helping develop this project) should behave. Do not adopt the bot's isolated HOME, permission settings, or agent prompts as your own.

A Discord bot that bridges Discord channels to Claude Code CLI sessions. A persistent main agent handles general conversation; sub-agents handle specialized tasks on their own channels with optional cron schedules.

## Quick Start

```bash
# Set your Discord bot token in bot/.envrc
echo 'export DISCORD_BOT_TOKEN="your-token"' > bot/.envrc

# Symlink Claude credentials into the isolated home
ln -s ~/.claude/.credentials.json home/.claude/.credentials.json

# Install deps and register the launchd service
cd bot && uv sync && cd ..
scripts/ctl.sh install
scripts/ctl.sh start
```

## Architecture

```
Discord User
    │
    ▼
AssistantBot (discord.py)
    │
    ├─ Router ──► maps channel ID → "main" or agent name
    │
    ├─ Main Agent (MainAgentProcess)
    │   └─ Long-running `claude --agent main --input-format stream-json --output-format stream-json`
    │   └─ Communicates via stdin/stdout JSON events
    │   └─ Supervised — auto-restarts on crash
    │
    ├─ Sub-Agents (SubAgentRunner)
    │   └─ One-shot `claude -p --agent {name} -- {prompt}`
    │   └─ Per-agent asyncio locks prevent concurrent runs
    │
    └─ Scheduler (APScheduler)
        └─ Fires sub-agents on cron schedules
```

All Claude processes run with an **isolated HOME** (`home/`) — separate from the user's real `~/.claude`. Credentials are symlinked from the real home.

## Project Structure

```
config/                           # All user-editable configuration
  agents.yaml                     # Agent definitions, channel IDs, cron schedules
  mcp.json                        # Shared MCP server config (e.g. Playwright)
  settings.json                   # Claude tool permissions (allow list)
  CLAUDE.md                       # Global agent guidelines (applies to all agents)
  agents/                         # Agent system prompts and per-agent MCP configs
    main.md                       # Main agent system prompt
    journalist.md                 # Journalist agent system prompt
    journalist.mcp.json           # Optional per-agent MCP config

bot/                              # Python package (discord bot)
  pyproject.toml                  # deps: discord.py, apscheduler, pyyaml, python-dotenv
  src/claude_assistant/
    main.py                       # Entry point — loads .envrc, resolves paths, starts bot
    config.py                     # Parses agents.yaml → AssistantConfig dataclasses
    discord_bot.py                # AssistantBot — routing, message handling, lifecycle
    claude_process.py             # MainAgentProcess (stream-json) + SubAgentRunner (one-shot)
    router.py                     # Channel ID → agent name mapping
    scheduler.py                  # APScheduler cron wrapper
    session.py                    # SessionStore — atomic JSON persistence of session IDs
    chunker.py                    # Splits long messages for Discord's 2000-char limit
  tests/                          # pytest + pytest-asyncio

home/.claude/                     # Isolated HOME for Claude processes (symlinks to config/)
  agents/ → ../../config/agents/
  settings.json → ../../config/settings.json
  CLAUDE.md → ../../config/CLAUDE.md
  .credentials.json → ~/.claude/.credentials.json (gitignored)

scripts/                          # Service management
  run.sh                          # Wrapper for launchd — loads env, runs bot
  ctl.sh                          # Service control (install/start/stop/restart/logs/status)
  com.asempruch.claub.plist       # launchd plist template (paths filled at install)

workspaces/                       # Runtime scratch dirs per agent (gitignored)
data/                             # sessions.json — session ID persistence (gitignored)
```

## Configuration

All configuration lives in `config/`. The `home/.claude/` directory symlinks to it so Claude Code can discover settings in the expected locations.

### agents.yaml

```yaml
discord:
  main_channel_id: "123456789"    # Required — channel the main agent listens on

agents:
  journalist:
    channel_id: "987654321"       # Required — dedicated channel for this agent
    schedule:                     # Optional — cron triggers
      - cron: "0 9 * * *"
        prompt: "Check the latest tech news"
```

> **Note:** Do not add `[scheduled]` to cron prompts in `agents.yaml` — the bot prefixes it automatically at runtime (see `scheduler.py`).

### Agent Context

Agent behavior is configured at two levels:

- **`config/CLAUDE.md`** — Global guidelines that apply to all agents: safety rules, Discord behavior, workspace usage, and the memory protocol. Loaded automatically via the isolated HOME's symlink.
- **`config/agents/{name}.md`** — Per-agent identity: role, personality, task instructions, and agent-specific memory structure (what to track, how to organize it, retention policies). Passed to Claude via `--agent {name}`.

Each agent also gets a workspace directory at `workspaces/{name}/`. These are **runtime scratch directories** — gitignored and created automatically. Agents can write files, create their own `CLAUDE.md`, or store data there as they see fit. Do not rely on workspace contents being present across fresh clones.

### Adding a New Agent

1. Add entry to `config/agents.yaml` with `channel_id` and optional `schedule`
2. Create `config/agents/{name}.md` with the agent's system prompt
3. Optionally create `config/agents/{name}.mcp.json` for agent-specific MCP servers
4. Restart the bot

### MCP Servers

**Shared** (`config/mcp.json`) — passed to all agents:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest", "--browser", "chrome"]
    }
  }
}
```

**Per-agent** (`config/agents/{name}.mcp.json`) — additional MCPs for a specific agent only. Both files are passed via `--mcp-config` when the agent runs.

### Permissions (config/settings.json)

Tool allow-list for all agents:

```json
{
  "permissions": {
    "allow": ["mcp__playwright__*", "WebFetch", "WebSearch"]
  }
}
```

## Message Flow

1. User sends message in Discord
2. Router checks channel ID → `("main", None)` or `("agent", "journalist")`
3. **Main channel**: message sent to long-running process via stream-json stdin, response read from stdout events until `type: result`
4. **Agent channel**: acquire per-agent lock, run `claude -p --agent {name} -- {message}`, parse JSON output
5. Response chunked at newline boundaries (max 2000 chars) and sent back

### Commands

- `/reset` in main channel — stops main process, clears session, restarts
- `/reset {agent}` — clears sub-agent session (next message starts fresh)

## Session Persistence

Session IDs are stored in `data/sessions.json` (agent name → session UUID). On startup or message send, the bot passes `--resume {session_id}` to maintain conversation context. If resume fails, the session is cleared and a fresh one starts.

## Service Management

The bot runs as a macOS launchd service (`com.asempruch.claub`). It starts on login and auto-restarts on crash. The service is already installed — after code changes, just restart:

```bash
scripts/ctl.sh restart   # The command you'll use 99% of the time
```

Other commands (rarely needed):

```bash
scripts/ctl.sh status    # Print service state
scripts/ctl.sh logs      # Last 50 lines of stdout + stderr
scripts/ctl.sh logs -f   # Tail logs live
scripts/ctl.sh stop      # Unload the service
scripts/ctl.sh start     # Load and start the service
scripts/ctl.sh install   # Re-template plist (only after changing the plist template)
scripts/ctl.sh uninstall # Stop and remove the plist entirely
```

Logs are at `~/Library/Logs/claub/` (`stdout.log` and `stderr.log`).

## Deploying Changes

After completing a feature or bug fix (not every individual edit, but once the work is done), restart the bot so changes take effect:

```bash
scripts/ctl.sh restart
```

This applies to changes in bot code (`bot/`), agent prompts (`config/agents/`), global config (`config/CLAUDE.md`), and permissions (`config/settings.json`).

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

To test how agents behave, permissions, MCP access, etc. without running the full bot, use the isolated HOME:

```bash
# Quick auth check
HOME=home claude -p --no-session-persistence "say hello"
```

To run an agent exactly as the bot would (same HOME, workspace, MCP, and permissions), set `REPO` and `cd` into the agent's workspace. The bot spawns each process with `cwd` set to the workspace:

```bash
REPO=$(pwd)  # run from repo root

# Interactive main agent session
cd $REPO/workspaces/main
HOME=$REPO/home claude --agent main --permission-mode acceptEdits \
  --mcp-config $REPO/config/mcp.json --no-session-persistence

# One-shot sub-agent (e.g. journalist)
cd $REPO/workspaces/journalist
HOME=$REPO/home claude -p --agent journalist --permission-mode acceptEdits \
  --mcp-config $REPO/config/mcp.json --no-session-persistence \
  -- "check the latest AI news"
```

If auth fails, re-symlink credentials: `ln -sf ~/.claude/.credentials.json home/.claude/.credentials.json`

### Key Design Decisions

- **Lazy init**: Main agent process starts without blocking on init event — session ID captured from first response
- **Asyncio lock on stdout**: Prevents concurrent reads from stream-json stdout
- **`--` separator**: Sub-agent prompts use `--` to prevent argparse from consuming the prompt as a flag argument
- **Isolated HOME**: All Claude processes use `home/` — credentials symlinked, settings/agents/permissions self-contained
- **acceptEdits permission mode**: All processes run with `--permission-mode acceptEdits`
- **Config symlinks**: All user-editable config lives in `config/`. `home/.claude/` symlinks to it so Claude Code finds settings in expected locations.

### Agent Memory System

Agents are long-running assistants (not dev tools) that may operate for months. Memory is file-based, stored in each agent's workspace at `workspaces/{name}/memory/`. The system is designed to stay useful over time without unbounded growth.

Memory guidelines live within the broader agent configuration system (see "Agent Context" above). `config/CLAUDE.md` defines global rules that apply to all agents — safety, Discord behavior, workspace usage, and the memory protocol. `config/agents/{name}.md` defines each agent's role, personality, task instructions, and agent-specific memory structure. When editing memory guidelines, preserve this split: global rules enforce mechanical discipline; agent-specific rules define *what* to remember and *how long* to keep it.

**Design principles to enforce:**
- **Mandatory startup read**: Every agent reads `memory/index.md` before doing any work, every session. No conditional checks.
- **Write-time pruning**: Every memory write must include a review of the index. Remove outdated entries, merge overlapping ones. This is the primary defense against memory bloat.
- **Compaction awareness**: Claude's context compaction can silently drop conversation history. Anything important must be written to memory files promptly — not deferred to end-of-session.
- **Current state wins**: When memory conflicts with observed reality, trust what's there now and update the memory. Stale entries that persist cause compounding errors.
- **Bounded growth**: Index should stay under ~50 entries. Agent-specific rules should define retention periods (e.g., journalist keeps 7 days of briefs). Memory without a pruning policy will degrade agent performance over time.

### Dependencies

Core: `discord.py`, `apscheduler`, `pyyaml`, `python-dotenv`
Dev: `pytest`, `pytest-asyncio`
Build: `hatchling`

## Branching

Development happens on the `develop` branch. The `main` branch holds the clean initial commit.
