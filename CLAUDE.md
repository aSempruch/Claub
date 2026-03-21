# Claub — Discord Bot for Claude Code CLI

A Discord bot that bridges Discord channels to Claude Code CLI sessions. A persistent main agent handles general conversation; sub-agents handle specialized tasks on their own channels with optional cron schedules.

## Quick Start

```bash
# Set your Discord bot token in bot/.envrc
echo 'export DISCORD_BOT_TOKEN="your-token"' > bot/.envrc

# Symlink Claude credentials into the isolated home
ln -s ~/.claude/.credentials.json claude/home/.claude/.credentials.json

# Install and run
cd bot && uv sync && uv run claude-assistant
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
    │   └─ Long-running `claude --input-format stream-json --output-format stream-json`
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

All Claude processes run with an **isolated HOME** (`claude/home/`) — separate from the user's real `~/.claude`. Credentials are symlinked from the real home.

## Project Structure

```
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

claude/                           # Claude CLI configuration
  config/
    agents.yaml                   # Agent definitions, channel IDs, cron schedules
    mcp.json                      # MCP server config (e.g. Playwright)
  home/.claude/                   # Isolated HOME for Claude processes
    settings.json                 # Tool permissions (allow list)
    agents/                       # Agent system prompts (markdown)
      main.md
      journalist.md
    .credentials.json             # Symlink → ~/.claude/.credentials.json
  workspaces/                     # Runtime scratch dirs per agent (gitignored)
  data/
    sessions.json                 # Session ID persistence (gitignored)
```

## Configuration

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

### Agent Context

Each agent's identity is defined by a markdown file in `claude/home/.claude/agents/` (e.g. `main.md`, `journalist.md`). These are passed to Claude via `--agent {name}` and serve as the agent's system prompt. They are tracked in git as part of the project configuration.

Each agent also gets a workspace directory at `claude/workspaces/{name}/`. These are **runtime scratch directories** — gitignored and created automatically. Agents can write files, create their own `CLAUDE.md`, or store data there as they see fit. Do not rely on workspace contents being present across fresh clones.

### Adding a New Agent

1. Add entry to `claude/config/agents.yaml` with `channel_id` and optional `schedule`
2. Create `claude/home/.claude/agents/{name}.md` with the agent's system prompt
3. Restart the bot

### MCP Servers (claude/config/mcp.json)

Passed to all Claude processes via `--mcp-config`. Example with Playwright using system Chrome:

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

### Permissions (claude/home/.claude/settings.json)

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

Session IDs are stored in `claude/data/sessions.json` (agent name → session UUID). On startup or message send, the bot passes `--resume {session_id}` to maintain conversation context. If resume fails, the session is cleared and a fresh one starts.

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

### Key Design Decisions

- **Lazy init**: Main agent process starts without blocking on init event — session ID captured from first response
- **Asyncio lock on stdout**: Prevents concurrent reads from stream-json stdout
- **`--` separator**: Sub-agent prompts use `--` to prevent argparse from consuming the prompt as a flag argument
- **Isolated HOME**: All Claude processes use `claude/home/` — credentials symlinked, settings/agents/permissions self-contained
- **acceptEdits permission mode**: All processes run with `--permission-mode acceptEdits`

### Dependencies

Core: `discord.py`, `apscheduler`, `pyyaml`, `python-dotenv`
Dev: `pytest`, `pytest-asyncio`
Build: `hatchling`
