---
name: claub-logs
description: Use when reading Claub bot logs, diagnosing container issues, understanding agent lifecycle events, or troubleshooting schedule/MCP/auth problems from docker compose logs output
---

# Reading Claub Logs

## Log Format

All logs follow: `TIMESTAMP LOGGER_NAME LEVEL MESSAGE`

```
2026-03-24 09:00:01,234 claude_assistant INFO Starting claude-assistant
```

- **Logger:** `claude_assistant` (all modules) and `claude_assistant.claude_process` (set to DEBUG)
- **Output:** stderr only — visible via `docker compose logs`
- **Timestamps:** Container TZ is `America/New_York`

## Lifecycle: What Normal Startup Looks Like

```
claude_assistant INFO Starting claude-assistant          # main.py — bot process begins
claude_assistant INFO Discord connected as BotName#1234  # discord_bot.py — Discord ready
claude_assistant INFO Starting MCP server on 127.0.0.1:9400  # discord_bot.py — schedule MCP up
claude_assistant INFO Scheduler started with 3 jobs      # scheduler.py — cron loaded
```

Agent processes start **lazily** (first message or scheduled trigger), not at boot.

## Agent Process Logs

### Starting

```
claude_assistant INFO Starting agent main: claude --agent main --input-format stream-json ...
```
Shows full CLI command. Appears on first message to an agent or when supervisor restarts it.

### Stderr Passthrough (DEBUG)

```
claude_assistant.claude_process DEBUG claude stderr: ...
```
Raw stderr from the Claude CLI subprocess. Useful for diagnosing CLI-level issues (auth prompts, model errors, tool failures). Only visible because `claude_process` logger is set to DEBUG.

### Idle Reaper

```
claude_assistant INFO Reaping idle agent main (idle 600s)
```
Agent killed after 10 min inactivity. **This is normal** — prevents stale OAuth tokens. The process restarts on next message. If you see a reap followed immediately by a supervisor restart, the `_reaped` set should prevent it — if it doesn't, that's a bug.

### Supervisor Restarts

```
claude_assistant WARNING Agent main died, restarting...
```
Background supervisor detected a dead process. Automatic recovery. If followed by:

```
claude_assistant EXCEPTION Failed to restart agent main
```
...the restart itself failed (check for auth issues or CLI crash).

### Retry Without Resume

```
claude_assistant INFO Retrying main without --resume
```
First start attempt (with `--resume SESSION_ID`) failed — likely stale/invalid session. Bot clears session and retries fresh. Normal recovery path.

## Agent Lock Contention

```
claude_assistant INFO Agent journalist waiting for agent lock
```
Only one agent talks to Claude CLI at a time (global lock prevents credential races). This log means another agent is currently active. If you see long gaps between this and the response, another agent's request is taking a while.

## Authentication Errors

```
claude_assistant ERROR Claude authentication failed for main
```
or for scheduled tasks:
```
claude_assistant ERROR Claude authentication failed for scheduled agent journalist
```
**Action:** Run `docker exec -it claude-claub-1 claude` and re-authenticate.

## Schedule Logs

### Scheduled Task Firing

```
claude_assistant INFO Scheduled task for main — delaying 12s    # jitter (lognormal distribution)
claude_assistant INFO Scheduled task firing for main             # actually executing now
```
Jitter prevents multiple agents from hitting Claude simultaneously.

### One-Shot Schedule

```
claude_assistant INFO One-shot schedule a1b2c3 for main — firing and removing
```
Schedule with `one_shot: true` — fires once, then auto-deletes from persistence.

### Orphaned Schedules

```
claude_assistant WARNING Skipping orphaned schedules for agent old_agent
```
Schedule data exists for an agent not in `agents.yaml`. Happens after removing an agent without cleaning `schedules.json`. Harmless but worth cleaning up.

### MCP Schedule Mutations

```
claude_assistant INFO Created schedule a1b2c3 for main: 'Check the news'
claude_assistant INFO Deleted schedule a1b2c3 for main
```
Agent used MCP tools to manage its own schedules.

## Scheduled Task Failures

```
claude_assistant ERROR Channel not found for agent ghost
```
Agent's `channel_id` in `agents.yaml` doesn't match any Discord channel the bot can see.

```
claude_assistant EXCEPTION Scheduled task failed for main
```
RuntimeError during execution — check the traceback that follows.

## Agent Opt-Out

```
claude_assistant INFO Agent main opted out of posting
```
Agent's response started with `[NO_POST]` — it decided the scheduled task had nothing worth posting. Normal for agents with conditional posting logic.

## Startup Errors

```
claude_assistant ERROR DISCORD_BOT_TOKEN environment variable is required
```
Missing `.env` file or empty token. Bot won't start.

```
claude_assistant ERROR Config not found: /claub/config/agents.yaml
```
Config bind mount issue. Check `docker-compose.yml` volumes.

## Shutdown

```
claude_assistant INFO Shutting down...
claude_assistant INFO Supervisor loop cancelled
claude_assistant INFO Idle reaper cancelled
```
Clean shutdown sequence. All three lines should appear together.

## Modules With No Logging

These modules operate silently — failures surface in calling code:
- `config.py` — YAML parsing (errors bubble to main.py)
- `router.py` — channel routing (misroutes cause no log, just no response)
- `session.py` / `schedule_store.py` — atomic JSON persistence
- `chunker.py` — message splitting

## Talking to an Agent Directly

When logs aren't enough, drive the agent yourself. The debug CLI (`claude_assistant.debug_agent`) uses the agent's **full production config** — agent definition, per-agent MCP configs, allowed/disallowed tools, model, effort — without touching `sessions.json`. It drops `--resume` and adds `--no-session-persistence`, so every run is a fresh session.

```bash
docker exec claude-claub-1 uv run --project /app/bot \
  python -m claude_assistant.debug_agent {name} -p "your prompt here"
```

Omit `-p` for interactive stdin mode (manual back-and-forth).

To isolate whether a problem is in the CLI itself vs. the bot's wiring, drop to raw `claude`, which skips all bot config:

```bash
docker exec claude-claub-1 claude -p "say hello" --no-session-persistence
```

If the raw call works and the debug CLI doesn't, the bug is in config/wiring. If both fail, it's the CLI or auth.

## Quick Troubleshooting

| Symptom | Look For |
|---------|----------|
| Agent not responding | `waiting for agent lock` (contention) or no start log (routing issue) |
| Agent keeps restarting | `WARNING Agent X died` in a loop — check stderr DEBUG lines |
| Stale conversation | `Retrying without --resume` not appearing — session may be corrupt, use `/clear` |
| Schedule not firing | `Skipping orphaned schedules` or missing `Scheduler started with N jobs` |
| Auth expired | `ERROR Claude authentication failed` — re-auth in container |
| No logs at all | Container not running or wrong `docker compose` project |
