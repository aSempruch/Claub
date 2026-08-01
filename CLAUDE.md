# Claub — Discord Bot for Claude Code CLI

A Discord bot that bridges Discord channels to Claude Code CLI sessions. Each agent gets its own channel and a persistent streaming process, with optional cron schedules. Runs in Docker.

> This file is [Claude Code context](https://docs.anthropic.com/en/docs/claude-code/memory#claudemd) — the always-loaded orientation map. Subsystem detail lives in skills (see below). [`README.md`](README.md) is the public overview and first-time setup; [`example/`](example/) is a working config.

## Detailed Guides (skills, loaded on demand)

| Skill | Use when |
|---|---|
| `add-claub-agent` | Adding/editing an agent, or deciding which config level behavior belongs in |
| `claub-playwright` | Browser profiles, the bridge daemon, file uploads, zoom/captcha issues |
| `claub-mcp-servers` | Working on an existing MCP (Nextcloud, HASS, Google, git, file-download) |
| `build-mcp-server` | Building a **new** MCP server |
| `claub-schedules` | schedules.json, density limits, one-shot schedules, firing history |
| `claub-logs` | Reading `docker compose logs`, or driving an agent with the debug CLI |
| `lauren-deployment` / `stas-deployment` | The other two instances on this host |

Design specs for larger features live in `docs/superpowers/specs/`.

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
    └─ MCP Server (FastMCP, localhost:9400) — schedules at /mcp, agent messaging at /agents/{name}/mcp
        └─ Agents manage their own schedules via list/create/delete tools
        └─ Agents in the same group message each other via message_agent_{peer} tools

Host-side daemons (outside the container, launchd):
    playwright-bridge :9500 — one browser MCP process per agent
    exec-bridge       :9501 — spawns a throwaway claub-exec container per sandbox call
```

Claude CLI uses its native `~/.claude/` path inside the container. Settings and CLAUDE.md are copied from `/claub/config/` into `~/.claude/` at container startup by the entrypoint script. Agent definitions are passed programmatically via the `--agents` CLI flag (not copied into `~/.claude/agents/`). Credentials persist in a named Docker volume (`claude-home`).

## Project Structure

```
bot/                              # Python package (discord bot)
  pyproject.toml                  # deps: discord.py, apscheduler, pyyaml, python-dotenv
  src/claude_assistant/
    main.py                       # Entry point — resolves paths, starts bot
    config.py                     # Parses agents.yaml → AssistantConfig dataclasses
    discord_bot.py                # AssistantBot — routing, message handling, lifecycle
    claude_process.py             # AgentProcess — persistent stream-json process per agent
    debug_agent.py                # Debug CLI — talk to an agent with its production config
    router.py                     # Channel ID → agent name mapping
    scheduler.py                  # APScheduler cron wrapper
    session.py                    # SessionStore — atomic JSON persistence of session IDs
    chunker.py                    # Splits long messages for Discord's 2000-char limit
    mcp_server.py                 # FastMCP HTTP server — schedules + agent messaging
    schedule_store.py             # ScheduleStore — atomic JSON persistence of schedules
  tests/                          # pytest + pytest-asyncio

README.md                         # Public-facing project overview and first-time setup
Dockerfile                        # Python 3.12 + Node.js + uv + Claude CLI
entrypoint.sh                     # Copies config into ~/.claude/, uv syncs MCPs, starts bot
docker-compose.yml                # Service definition with volumes — ALL docker compose commands run from this project root

mcps/                             # MCP servers baked into the image at /app/mcps/
  git/                            # Workspace-scoped git operations
  alpaca/                         # Rail-guarded Alpaca paper-trading slice for the trader agent
  leetcode-stats/                 # LeetCode GraphQL client + in-progress solve monitoring
  nextcloud/                      # Nextcloud file sharing via WebDAV
  hass/                           # Home Assistant — narrow typed wrappers
  file-download/                  # Least-privilege URL → workspace fetch
  sandbox/                        # Throwaway container exec via the host exec bridge

docker/exec-sandbox/              # claub-exec image (built by hand, never in compose)
scripts/playwright-bridge/        # Host-side daemon managing per-agent browser MCPs
scripts/exec-bridge/              # Host-side daemon spawning sandbox containers
example/                          # Starter configuration (sanitized)
docs/                             # Specs, plans, investigations

/claub/                           # Instance root inside container (bind-mounted from host)
  config/                         # All user-editable configuration
    agents.yaml                   # Agent definitions, channel IDs, groups, hooks
    mcp.json                      # Shared MCP server config
    settings.json                 # Claude tool permissions (allow list)
    CLAUDE.md                     # Global agent guidelines (applies to all agents)
    agents/{name}.md              # Agent system prompt
    agents/{name}.mcp.json        # Optional per-agent MCP config
  mcps/                           # Instance MCP servers (extend the baked-in ones)
  workspaces/{name}/              # Runtime scratch dirs per agent (auto-created)
    .claude-skills/               # Real dir; .claude/skills/ symlinks here
  data/                           # sessions.json, schedules.json, firing_history.json
```

## Configuration

All configuration lives in `/claub/config/` inside the container (bind-mounted from the host, e.g. `~/docker/claub/config/`). The entrypoint copies `settings.json` and `CLAUDE.md` into `~/.claude/` at container startup. Agent `.md` files are read by the bot and passed to each Claude CLI process via the `--agents` JSON flag, so each process only knows about its own agent definition. The base path is set via `CLAUB_HOME=/claub` (configurable).

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

An `agents.main` entry is required. Agent names must match `[A-Za-z0-9_-]+` — they become MCP tool names and URL path segments. Schedules are **not** configured here; they're managed at runtime via MCP.

**Agent behavior is configured at three levels** (global rules → agent identity → self-modifiable workspace config). Getting this split right matters — see the `add-claub-agent` skill.

### Agent Messaging

Agents listed together in a top-level `agent_groups` entry can message each other via `mcp__agents__message_agent_{peer}` — one tool per reachable peer. The call **blocks**: the receiver processes the message in its normal session and the reply returns as the tool result; nothing is posted to Discord.

Each agent gets its own messaging MCP mounted at `/agents/{name}/mcp`, so sender identity comes from the mount path rather than a header. All mounts share one wait-for graph, so cycle detection spans the instance. Safety: cycle/depth rejection, a 15-min sender wait cap, sender watchdog exemption while blocked, and `MCP_TOOL_TIMEOUT` raised above the cap.

Full design: [`docs/superpowers/specs/2026-07-24-agent-messaging-design.md`](docs/superpowers/specs/2026-07-24-agent-messaging-design.md).

### Schedules

Managed dynamically at runtime via the embedded MCP server (`mcp__schedules__*`), persisted to `/claub/data/schedules.json`, synced to APScheduler immediately on mutation. Creation is globally rate-limited (5 firings/24h, 30/7d across all agents). See the `claub-schedules` skill.

### MCP Servers

MCP servers give agents access to external tools without granting arbitrary code execution. Configs are merged from two levels, both passed via `--mcp-config`:

- **Shared** — `/claub/config/mcp.json`, passed to all agents.
- **Per-agent** — `/claub/config/agents/{name}.mcp.json`.

Servers baked into the image live at `/app/mcps/`; instance servers at `/claub/mcps/` (auto-`uv sync`'d by the entrypoint). See the `claub-mcp-servers` and `build-mcp-server` skills.

Playwright is the exception — it runs **host-side**, one process per agent, managed by a bridge daemon via lifecycle hooks. See the `claub-playwright` skill.

### Sandboxed Execution

Agents opted in via `allowed_tools_additional: ["mcp__sandbox__*"]` get
`mcp__sandbox__run(command)` and `mcp__sandbox__install(packages)` — arbitrary
shell in a throwaway `claub-exec` container that holds no secrets and mounts
only the calling agent's workspace (same path, `/claub/workspaces/{agent}`).
Spawned host-side by `scripts/exec-bridge/` (launchd, port 9501), mirroring the
Playwright bridge — the Docker socket never enters the bot container. Two
endpoints carry the trust boundary: `/exec` takes a command and always runs
`--network none`; `/install` takes package names only, never a command, and the
bridge validates the names and builds the argv itself. The
`.claude/` dir is mounted read-only so injected code cannot write
`settings.local.json` to escalate. Image built by hand:
`docker build -t claub-exec docker/exec-sandbox/`. Design:
`docs/superpowers/specs/2026-07-26-sandboxed-exec-design.md`.

### LeetCode Solve Monitoring

`mcp__leetcode-stats__start_monitoring(problem)` records **how** a problem was solved,
not just the final code: a detached process polls the LeetCode cloud-saved editor
buffer and writes a timestamped timeline (each save with its diff, the long pauses,
every submission verdict) to `leetcode-sessions/` in the agent's workspace,
mirroring the ICF emulator's session shape. `stop_monitoring()` ends it;
`get_monitoring_results()` reads it back — including while still running.

The **render** groups saves into edit runs rather than showing one row per autosave.
LeetCode autosaves every 5-10s, so a save boundary reflects the sampler, not the
solver: a run breaks when the sign of the size change flips, giving `WROTE` and
`DELETED` runs each shown as a real diff. Time-, submission-, and magnitude-based
segmentation were all tried against recorded sessions and all failed — see the
module docstring in `mcps/leetcode-stats/report.py`. Deletions matter most: everything
added and kept is recoverable from the final snapshot, so abandoned approaches are the
only content lost if the render drops them.

The monitor is deliberately **decoupled from agent lifetime** — it survives the agent
being reaped or restarted. Liveness is a `flock` on a container-local file (the kernel
releases it on death, so a stale lock cannot exist); identity is an `argv[0]` tag
matched exactly by a `/proc` scan. One session at a time. Design:
`docs/superpowers/specs/2026-07-28-leetcode-session-monitoring-design.md`.

### Trading Agent (paper)

The `trader` agent paper-trades US equities through `mcps/alpaca/` — a narrow,
rail-guarded slice of Alpaca (12 tools, not the official 60-tool server). Hard
rails live in code, not prompts: long-only, US stocks/ETFs only, max 10% of
equity per symbol, max 3 orders/day, a drawdown circuit breaker (buys halt >15%
below the high-water mark), a kill switch (`ALPACA_TRADING_DISABLED=1`), and a
paper guard (server refuses to start live without `ALPACA_LIVE_CONFIRMED`).
For an instant stop with no restart and no env edit, `touch
/claub/data/alpaca/DISABLED` — the server refuses every order while that
sentinel file exists, and `rm` re-enables.
`broker.py` is a vendor-agnostic protocol; `alpaca_impl.py` is the only file
importing the SDK, so a broker swap is one new file. Every order is appended to
`/claub/data/alpaca/trades.jsonl` by the server; `get_performance_report`
computes the scoreboard (vs SPY total return and vs buy-and-hold-of-buys) in
code because the research behind the design found benchmark selection is where
LLM-trading evaluations fool themselves. Paper→live is a deliberate env flip.
Design: `docs/superpowers/specs/2026-07-31-trading-agent-design.md`.

### Lifecycle Hooks

`AgentProcess` runs shell commands around the `claude` subprocess. `on_start` hooks run **before** `claude` is exec'd (so any MCP server they spawn is ready when Claude handshakes); `on_stop` hooks run **after** it exits. Hooks are sequential with a 15 s default timeout; failure or timeout logs a warning but does not abort agent lifecycle.

Configured in `agents.yaml`. Both top-level and per-agent fields are accepted; **per-agent hooks are additive on top of global** (same precedent as `allowed_skills`). Each hook is a shell string, so `$CLAUB_AGENT_NAME` interpolates naturally:

```yaml
on_start:
  - "curl -fsS --max-time 20 -X POST http://host.docker.internal:9500/start/$CLAUB_AGENT_NAME || true"

agents:
  career:
    channel_id: "..."
    on_start:
      - "echo career-specific warm-up"
```

Trailing `|| true` is a common pattern: if the hook target is temporarily down, the agent still starts rather than erroring.

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

This list is the **hard ceiling** — a tool missing from it is silently unavailable, and self-authored skills/subagents can only narrow it, never expand it. The container provides isolation, so no macOS Seatbelt sandbox is needed; agents run with `--permission-mode acceptEdits` and are constrained by the container's filesystem boundaries.

## Message Flow

1. User sends message in Discord
2. Router checks channel ID → agent name (or ignores if unknown channel)
3. If the message has Discord attachments, the bot downloads each to `/tmp/claub-attachments/{agent}/{message_id}/{sanitized_filename}` and appends a footer to the message text listing the paths, MIME types, and sizes. Failures are surfaced as `(failed: …)` lines in the footer rather than aborting the send. Files live in container `/tmp` and are wiped when the container is rebuilt (a plain `docker compose restart` does not clear them); agents move them into their workspace if they want to keep them.
4. Bot gets or starts the agent's persistent stream-json process (lazy startup)
5. Message sent to process via stdin, response read from stdout events until `type: result`
6. On process error: restart and retry once. On auth error: notify user.
7. Response chunked at newline boundaries (max 2000 chars) and sent back

Channel commands (`/clear`, `/stop`, `/compact`, `/model`) are documented in [`README.md`](README.md#channel-commands).

## Session Persistence

Session state is stored in `/claub/data/sessions.json` (agent name → `{session_id, model}`; legacy bare-string files migrate automatically). On startup or message send, the bot passes `--resume {session_id}` to maintain conversation context. If resume fails, the session is cleared (including any `/model` override) and a fresh one starts.

## Service Management

The bot runs as a Docker container via `docker compose`. It auto-restarts on crash (`restart: unless-stopped`).

```bash
docker compose up -d          # Start (build if needed)
docker compose up -d --build  # Rebuild image and start
docker compose restart        # Restart (picks up config changes)
docker compose stop           # Stop the container
docker compose logs --tail 50 # Last 50 lines of logs
docker compose logs -f        # Tail logs live
docker exec -it claude-claub-1 bash   # Shell inside the container
```

## Deploying Changes

**Bot code changes** (anything in `bot/`) — rebuild the image:

```bash
docker compose up -d --build
```

**Config changes** (agent prompts, settings.json, CLAUDE.md, mcp.json) — just restart; the entrypoint re-copies config at startup:

```bash
docker compose restart
```

## Authentication

Credentials persist in the `claude-home` named Docker volume. Authenticate once after first deploy (repeat if auth expires):

```bash
docker exec -it claude-claub-1 claude
# Follow login flow, then exit
```

## Development

```bash
cd bot
uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py
```

Integration tests (require real Claude CLI auth):

```bash
CLAUDE_INTEGRATION_TEST=1 uv run --extra dev pytest tests/test_integration.py -v
```

To drive a configured agent by hand for debugging, use the debug CLI — see the `claub-logs` skill.

### Key Design Decisions

- **All-streaming**: Every agent (including main) runs as a persistent `AgentProcess` using stream-json I/O. No one-shot processes.
- **Lazy startup**: Agent processes start on first message or scheduled trigger, not eagerly at boot. Supervisor restarts dead processes.
- **Stream lock inside AgentProcess**: Internal asyncio lock serializes send/receive on the stream-json pipe. No bot-level locks needed.
- **Lifecycle lock**: Separate lock in AgentProcess protects start/stop/restart transitions from racing with the supervisor.
- **Idle reaper**: Kills agent processes after 10 minutes of inactivity. Prevents stale processes from holding expiring OAuth tokens, which caused auth races when multiple long-lived processes shared the same credentials file. A `_reaped` set prevents the supervisor from immediately restarting intentionally killed processes.
- **Docker-first**: The container runs Claude CLI with its native `~/.claude/` path. No HOME override hack.
- **Entrypoint config copy**: `entrypoint.sh` copies settings.json and CLAUDE.md from `/claub/config/` into `~/.claude/` on every container start. Plain copies, no symlinks.
- **Separated instance from source**: Bot code is baked into the image; user config and runtime state are bind-mounted into `/claub/` (via `CLAUB_HOME`).
- **Inline agent definitions**: Agent `.md` files are parsed by the bot and passed via the `--agents` JSON flag. Each process only sees its own agent — no `disallowedTools` hack needed to prevent cross-agent invocation. Built-in agents (Explore, Plan, etc.) remain available.
- **Embedded MCP server**: FastMCP runs inside the bot process on localhost. Changes take effect immediately — no file polling or restart. One-shot schedules are deleted from persistence *before* execution to prevent duplicate firing on crash recovery.
- **Agent-authored skills via symlink**: Workspaces get real `.claude-skills/` and `.claude-agents/` dirs, with `.claude/skills/` and `.claude/agents/` symlinked to them. Claude Code blocks writes that resolve into `.claude/`, but reads through the symlinks fine — so agents can author their own skills and subagents without gaining write access to `.claude/settings.json` (which would let them escalate permissions). Created by `_ensure_authoring_symlink()` in `discord_bot.py` during `_start_agent`, which also migrates pre-existing real `.claude/{name}/` contents.

## Branching

Development happens on feature branches. The `main` branch is the stable trunk.
