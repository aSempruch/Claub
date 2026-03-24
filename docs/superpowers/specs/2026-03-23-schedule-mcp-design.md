# Schedule Management MCP — Design Spec

## Overview

Give Claub agents the ability to manage their own cron schedules at runtime via an HTTP MCP endpoint embedded in the bot process. Replaces the static `agents.yaml` schedule config with a dynamic `~/.claub/data/schedules.json` file managed entirely by the bot.

## Motivation

Currently, schedules are static — defined in `agents.yaml` and loaded at bot startup. Agents can't create reminders, schedule one-time tasks, or adjust their own cadence. This feature lets agents self-manage schedules through MCP tools, with changes taking effect immediately.

## Data Model

Schedules are persisted to `~/.claub/data/schedules.json` (runtime state, alongside `sessions.json`):

```json
{
  "main": [
    {
      "id": "a1b2c3",
      "cron": "0 17 * * 1-5",
      "prompt": "Check memory/actions.md",
      "one_shot": false
    }
  ],
  "journalist": [
    {
      "id": "d4e5f6",
      "cron": "0 9 * * *",
      "prompt": "Check the latest tech news",
      "one_shot": true
    }
  ]
}
```

- Top-level keys are agent names.
- Each schedule entry has a short random ID (6-char hex), a single cron expression, a prompt, and a `one_shot` boolean.
- Current multi-cron entries in `agents.yaml` (a list of crons sharing one prompt) are migrated as separate entries — one cron per entry.
- ID collisions are checked on create — regenerate if the ID already exists.
- A missing or empty file is treated as an empty dict `{}` on load.

## MCP Tools

Three tools exposed via FastMCP streamable-http transport:

### `list_schedules()`

Returns the current agent's schedules. No parameters — agent name is extracted from the `X-Agent-Name` request header.

### `create_schedule(cron: str, prompt: str, one_shot: bool)`

Creates a new schedule for the current agent. All parameters are required — `one_shot` has no default to force a deliberate choice. Validates the cron expression using `CronTrigger.from_crontab()` (standard 5-field cron format), generates a 6-char hex ID, persists to file, and syncs APScheduler immediately. Returns the created schedule object.

### `delete_schedule(id: str)`

Removes a schedule by ID for the current agent. Persists the change and removes the APScheduler job. Returns success or "not found" if the ID doesn't belong to the current agent.

## Bot Integration

### HTTP Server

The bot starts a FastMCP streamable-http server bound to `127.0.0.1:{port}` as a background asyncio task alongside the Discord event loop. Both run in the same process. Port defaults to `9400`, configurable via `CLAUB_MCP_PORT` env var.

The server must use `uvicorn.Server.serve()` as a coroutine task — not `uvicorn.run()`, which tries to own the event loop. The MCP server must be listening before the scheduler starts or any agents are spawned.

### Schedule Loading

On startup, the bot reads `~/.claub/data/schedules.json` and syncs all entries into APScheduler. Entries for agents not present in `agents.yaml` are ignored (orphan filtering). This replaces the current `agents.yaml` schedule loading path. The `scheduler.py` callback interface (`(agent_name, prompt) -> Awaitable[None]`) remains unchanged.

### Mutations

When an MCP tool creates or deletes a schedule, the bot:

1. Updates the in-memory schedule state
2. Writes `~/.claub/data/schedules.json` atomically (write to temp file, then rename)
3. Adds or removes the corresponding APScheduler job

No polling needed — changes are immediate since the MCP handler runs inside the bot process.

An `asyncio.Lock` guards all schedule mutations to prevent concurrent MCP calls from racing.

### One-Shot Cleanup

When a one-shot job fires, the bot removes it from persistence **before** executing the callback:

1. Removes the entry from in-memory state
2. Persists the updated `~/.claub/data/schedules.json`
3. Removes the APScheduler job
4. Then executes the callback (send prompt to agent)

The prompt string lives in the callback scope, so retry logic still works even though the entry is gone from the file. This ensures a bot crash during execution won't re-fire the one-shot on restart.

### Jitter

The existing 0–5 minute jitter before firing scheduled jobs is preserved.

## Config & Wiring

### MCP Config

Added to `~/.claub/config/mcp.json` (shared across all agents):

```json
{
  "mcpServers": {
    "schedules": {
      "type": "http",
      "url": "http://localhost:9400/mcp",
      "headers": {
        "X-Agent-Name": "${CLAUB_AGENT_NAME}"
      }
    }
  }
}
```

### Agent Process Environment

`claude_process.py` sets `CLAUB_AGENT_NAME` in the subprocess environment alongside `HOME`, using the agent's name.

### Permissions

Add `mcp__schedules__*` to `~/.claub/config/settings.json` allow list so all agents can use the schedule tools without prompting.

### Localhost Only

FastMCP server binds to `127.0.0.1` — not accessible from the network.

### Shutdown

The MCP server is added to the bot's `shutdown()` sequence alongside the supervisor, idle reaper, scheduler, and agent processes.

## Migration

### Steps

1. Create `~/.claub/data/schedules.json` from current `agents.yaml` schedule entries (flatten multi-cron entries into individual entries, generate IDs, set `one_shot: false` for all existing entries).
2. Remove `schedule` keys from `agents.yaml`.
3. Remove `ScheduleEntry` and schedule parsing from `config.py`.
4. Update `scheduler.py` to load from `~/.claub/data/schedules.json` instead of `AssistantConfig`. Add public `add_job()` and `remove_job()` methods for the MCP handler to call.
5. Write a migration script to convert existing `agents.yaml` schedules to `~/.claub/data/schedules.json`.
6. Update tests to reflect the new schedule source.

### What Stays in `agents.yaml`

`channel_id`, `display_name`, `avatar_url`, `allowed_tools_additional` — static agent identity configuration.

## Dependencies

- `fastmcp` — already used in other MCP servers in the project
- `uvicorn` (or equivalent) — ASGI server to run FastMCP's streamable-http transport

## Out of Scope

- Cross-agent schedule management (agents can only see/modify their own schedules)
- Schedule update tool (use delete + create)
- Web UI for schedule management
