# Claub — Discord Bot for Claude Code CLI

> **Note for the dev-time Claude Code instance:** This file documents the **Claub project** — a bot that spawns its own Claude CLI processes at runtime. References to "agents", "HOME", "permissions", "MCP configs", and "sessions" below describe how the **bot's** Claude processes are configured, **not** how you (the Claude Code instance helping develop this project) should behave. Do not adopt the bot's isolated HOME, permission settings, or agent prompts as your own.

A Discord bot that bridges Discord channels to Claude Code CLI sessions. Each agent gets its own channel and a persistent streaming process, with optional cron schedules.

## Quick Start

```bash
# Set your Discord bot token in bot/.envrc
echo 'export DISCORD_BOT_TOKEN="your-token"' > bot/.envrc

# Create ~/.claub with config, home, workspaces, mcps, and data
mkdir -p ~/.claub/{config/agents,home/.claude,workspaces,mcps,data}

# Set up the isolated HOME symlinks
cd ~/.claub/home/.claude
ln -s ../../config/agents agents
ln -s ../../config/CLAUDE.md CLAUDE.md
ln -s ../../config/settings.json settings.json
# Authenticate separately for the bot's isolated HOME
HOME=~/.claub/home claude  # follow login flow, then exit

# Add your config files (agents.yaml, CLAUDE.md, settings.json, mcp.json, agents/*.md)
# See Configuration section below

# Install deps and register the launchd service
cd /path/to/claub/bot && uv sync && cd ..
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
    ├─ Router ──► maps channel ID → agent name
    │
    ├─ Agent Processes (AgentProcess, one per agent)
    │   └─ Long-running `claude --agent {name} --input-format stream-json --output-format stream-json`
    │   └─ Communicates via stdin/stdout JSON events
    │   └─ Started lazily on first message, supervised — auto-restarts on crash
    │
    └─ Scheduler (APScheduler)
        └─ Fires any agent on cron schedules (including main)
```

All Claude processes run with an **isolated HOME** (`~/.claub/home/`) — separate from the user's real `~/.claude`. Credentials live in `~/.claub/home/.claude/.credentials.json` (its own file, not symlinked).

## Project Structure

```
bot/                              # Python package (discord bot)
  pyproject.toml                  # deps: discord.py, apscheduler, pyyaml, python-dotenv
  src/claude_assistant/
    main.py                       # Entry point — loads .envrc, resolves paths, starts bot
    config.py                     # Parses agents.yaml → AssistantConfig dataclasses
    discord_bot.py                # AssistantBot — routing, message handling, lifecycle
    claude_process.py             # AgentProcess — persistent stream-json process per agent
    router.py                     # Channel ID → agent name mapping
    scheduler.py                  # APScheduler cron wrapper
    session.py                    # SessionStore — atomic JSON persistence of session IDs
    chunker.py                    # Splits long messages for Discord's 2000-char limit
  tests/                          # pytest + pytest-asyncio

scripts/                          # Service management
  run.sh                          # Wrapper for launchd — loads env, runs bot
  ctl.sh                          # Service control (install/start/stop/restart/logs/status)
  com.asempruch.claub.plist       # launchd plist template (paths filled at install)

~/.claub/                         # User instance (not in repo)
  config/                         # All user-editable configuration
    agents.yaml                   # Agent definitions, channel IDs, cron schedules
    mcp.json                      # Shared MCP server config (e.g. Playwright)
    settings.json                 # Claude tool permissions (allow list)
    CLAUDE.md                     # Global agent guidelines (applies to all agents)
    agents/                       # Agent system prompts and per-agent MCP configs
      main.md                     # Main agent system prompt
      journalist.md               # Journalist agent system prompt
      journalist.mcp.json         # Optional per-agent MCP config
  home/.claude/                   # Isolated HOME for Claude processes (symlinks to config/)
    agents/ → ../../config/agents/
    settings.json → ../../config/settings.json
    CLAUDE.md → ../../config/CLAUDE.md
    .credentials.json                 # Own credentials (not symlinked)
  mcps/                           # Custom MCP servers (one dir per server)
    leetcode-stats/               # Example: LeetCode GraphQL API wrapper
      pyproject.toml
      server.py
  workspaces/                     # Runtime scratch dirs per agent (auto-created)
  data/                           # sessions.json — session ID persistence
```

## Configuration

All configuration lives in `~/.claub/config/`. The `~/.claub/home/.claude/` directory symlinks to it so Claude Code can discover settings in the expected locations. Override the default path by setting the `CLAUB_HOME` environment variable.

### agents.yaml

```yaml
agents:
  main:
    channel_id: "123456789"       # Required — every agent needs a channel
    schedule:                     # Optional — cron triggers work for any agent
      - cron: "0 8 * * *"
        prompt: "Review my open action items"
  journalist:
    channel_id: "987654321"
    schedule:
      - cron: "0 9 * * *"
        prompt: "Check the latest tech news"
```

An `agents.main` entry is required.

> **Note:** Do not add `[scheduled]` to cron prompts in `agents.yaml` — the bot prefixes it automatically at runtime (see `scheduler.py`).

### Agent Context — The Three-Level Split

Agent behavior is configured at three levels. Getting this split right matters — putting the wrong thing at the wrong level leads to either rigid agents that can't adapt or unstable agents that lose their identity.

**Level 1: Global** (`~/.claub/config/CLAUDE.md`) — Rules that apply to **all** agents: safety, Discord behavior, workspace usage, memory protocol. Loaded automatically via the isolated HOME's symlink.

**Level 2: Agent identity** (`~/.claub/config/agents/{name}.md`) — The agent's **stable core**: personality, communication style, role definition, capabilities, and memory structure. This is **who the agent is** — it should rarely change. Think of it as the agent's DNA. Does NOT include:
- Specific targets, criteria, or parameters that the user might adjust (those go in workspace CLAUDE.md)
- Scheduled task instructions (those go in the cron `prompt` in agents.yaml)
- Runtime state or progress tracking (that goes in memory)

**Level 3: Living config** (`~/.claub/workspaces/{name}/CLAUDE.md`) — The **fluid details** the agent works with day-to-day: current targets, search criteria, focus areas, topic lists, thresholds. The agent can self-modify this file when the user asks to shift focus (e.g., "stop covering crypto", "raise my comp target to $180k"). The agent `.md` should reference this file and tell the agent it exists.

**Rule of thumb:** If you'd change it by editing the agent's personality, it's Level 2. If you'd change it by telling the agent "from now on, focus on X instead of Y", it's Level 3.

Each agent also gets a workspace directory at `~/.claub/workspaces/{name}/`. These are **runtime scratch directories** created automatically. Agents can write files or store data there as they see fit.

### Adding a New Agent

1. Add entry to `~/.claub/config/agents.yaml` with `channel_id` and optional `schedule` (cron prompts should contain the task instructions, not the agent `.md`)
2. Create `~/.claub/config/agents/{name}.md` — stable identity only (personality, style, capabilities, memory structure). Must include YAML frontmatter with `name` and `description`. Reference the workspace CLAUDE.md so the agent knows to read it.
3. Create `~/.claub/workspaces/{name}/CLAUDE.md` — fluid details the agent works with (targets, criteria, focus areas). The agent can self-modify this file.
4. Optionally create `~/.claub/config/agents/{name}.mcp.json` for agent-specific MCP servers
5. Restart the bot

### MCP Servers

MCP servers give agents access to external tools (APIs, browsers, etc.) without granting arbitrary code execution. Two levels:

**Shared** (`~/.claub/config/mcp.json`) — passed to all agents:

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

**Per-agent** (`~/.claub/config/agents/{name}.mcp.json`) — additional MCPs for a specific agent only. Both files are passed via `--mcp-config` when the agent runs.

#### Custom MCP Servers

Use the `/build-mcp-server` skill for the full guide on building, wiring, and testing custom MCP servers for agents.

### Permissions & Sandboxing (~/.claub/config/settings.json)

Tool allow-list and OS-level sandbox for all agents:

```json
{
  "permissions": {
    "allow": ["mcp__playwright__*", "WebFetch", "WebSearch"]
  },
  "sandbox": {
    "enabled": true,
    "autoAllowBashIfSandboxed": false,
    "allowUnsandboxedCommands": false,
    "filesystem": {
      "allowWrite": [
        "/private/tmp/claub",
        "/Users/you/.claub/workspaces",
        "/Users/you/.claub/home/.cache/uv",
        "/Users/you/.claub/home/.local/share/uv"
      ],
      "denyRead": [
        "/Users/you/Desktop", "/Users/you/Documents", "/Users/you/Downloads",
        "/Users/you/.claude", "/Users/you/.ssh", "/Users/you/.aws",
        "/Users/you/.gnupg", "/Users/you/.config/gh", "/Users/you/.netrc",
        "/Users/you/.npmrc", "/Users/you/.zshrc", "/Users/you/.zsh_history",
        "/Users/you/.bash_history", "/Users/you/Library/Keychains"
      ]
    }
  }
}
```

The sandbox uses macOS Seatbelt to enforce restrictions at the OS level — all child processes (including scripts run via `uv run`) are equally constrained. Key properties:

- **Writes** are restricted to the workspace, `/tmp/claub`, and uv cache/data dirs. Writes anywhere else (including `/tmp`) are blocked by the kernel.
- **Reads** are open by default except for explicitly denied paths (credentials, SSH keys, personal dirs). Note: `denyRead` on a parent path also blocks writes to child paths, so we enumerate specific sensitive dirs rather than denying `/Users/you` broadly.
- **Network** from subprocesses is blocked (curl, urllib, etc. all fail). Agents can still use `WebFetch`/`WebSearch` via Claude's built-in tools.
- **`allowUnsandboxedCommands: false`** prevents the `dangerouslyDisableSandbox` escape hatch.
- To allow Bash execution (e.g. `Bash(uv run *)`), add it to `permissions.allow` — the sandbox ensures scripts can't escape. Pre-install Python via `HOME=~/.claub/home uv python install 3.13` first since `uv` needs a writable cache for initial setup.

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

Session IDs are stored in `~/.claub/data/sessions.json` (agent name → session UUID). On startup or message send, the bot passes `--resume {session_id}` to maintain conversation context. If resume fails, the session is cleared and a fresh one starts.

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

This applies to changes in bot code (`bot/`), agent prompts (`~/.claub/config/agents/`), global config (`~/.claub/config/CLAUDE.md`), and permissions (`~/.claub/config/settings.json`).

**Two repos to commit to:** Bot code lives in the project repo (`~/Claude`), instance config lives in `~/.claub` (its own git repo). After making changes, commit to whichever repo was modified — often both. For example, adding a new MCP server touches `~/.claub/mcps/` and `~/.claub/config/agents/`, while documenting the pattern touches `~/Claude/CLAUDE.md`.

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
HOME=~/.claub/home claude -p --no-session-persistence "say hello"
```

To run an agent exactly as the bot would (same HOME, workspace, MCP, and permissions), `cd` into the agent's workspace. The bot spawns each process with `cwd` set to the workspace:

```bash
# Interactive agent session (e.g. main)
cd ~/.claub/workspaces/main
HOME=~/.claub/home claude --agent main --permission-mode acceptEdits \
  --mcp-config ~/.claub/config/mcp.json --no-session-persistence

# Another agent (e.g. journalist)
cd ~/.claub/workspaces/journalist
HOME=~/.claub/home claude --agent journalist --permission-mode acceptEdits \
  --mcp-config ~/.claub/config/mcp.json --no-session-persistence
```

If auth fails, re-authenticate: `HOME=~/.claub/home claude` and follow the login flow.

### Key Design Decisions

- **All-streaming**: Every agent (including main) runs as a persistent `AgentProcess` using stream-json I/O. No one-shot processes.
- **Lazy startup**: Agent processes start on first message or scheduled trigger, not eagerly at boot. Supervisor restarts dead processes.
- **Stream lock inside AgentProcess**: Internal asyncio lock serializes send/receive on the stream-json pipe. No bot-level locks needed.
- **Lifecycle lock**: Separate lock in AgentProcess protects start/stop/restart transitions from racing with the supervisor.
- **Idle reaper**: Background task kills agent processes after 1 hour of inactivity. Prevents stale processes from holding expiring OAuth tokens, which caused auth races when multiple long-lived processes shared the same credentials file. A `_reaped` set prevents the supervisor from immediately restarting intentionally killed processes. If the idle reaper alone isn't sufficient, a global agent lock is stashed (`git stash list` — `agent-lock: serialize API calls via asyncio.Lock`) that serializes all API calls so only one agent talks to Claude at a time.
- **Isolated HOME**: All Claude processes use `~/.claub/home/` — own credentials file, settings/agents/permissions self-contained
- **acceptEdits permission mode**: All processes run with `--permission-mode acceptEdits`
- **Config symlinks**: All user-editable config lives in `~/.claub/config/`. `~/.claub/home/.claude/` symlinks to it so Claude Code finds settings in expected locations.
- **Separated instance from source**: Bot code lives in the repo; user config and runtime state live in `~/.claub/` (overridable via `CLAUB_HOME` env var).

### Agent Memory System

Agents are long-running assistants (not dev tools) that may operate for months. Memory is file-based, stored in each agent's workspace at `~/.claub/workspaces/{name}/memory/`. The system is designed to stay useful over time without unbounded growth.

Memory guidelines live within the broader agent configuration system (see "Agent Context" above). `~/.claub/config/CLAUDE.md` defines global rules that apply to all agents — safety, Discord behavior, workspace usage, and the memory protocol. `~/.claub/config/agents/{name}.md` defines each agent's role, personality, task instructions, and agent-specific memory structure. When editing memory guidelines, preserve this split: global rules enforce mechanical discipline; agent-specific rules define *what* to remember and *how long* to keep it.

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
