# Claude Code Discord Assistant — Design Spec

## Overview

A Python/uv Discord bot that bridges Discord to Claude Code sessions, providing a persistent AI assistant with specialized sub-agents. Each agent maintains conversational continuity across messages and scheduled tasks.

## Goals

- Persistent conversational assistant accessible via Discord
- Specialized agents with their own personalities, tools, and workspaces
- Cron-style scheduling for automated agent tasks
- Full isolation from the user's personal Claude Code config
- Leverage native Claude Code features (agents, MCPs, context compression) rather than reinventing them

## Project Structure

```
claude-assistant/
  bot/                              # Python/uv project (discord.py)
    pyproject.toml
    src/
      claude_assistant/
        __init__.py
        bot.py                      # Discord bot entry point
        router.py                   # Maps channels to agents
        session.py                  # Manages claude processes + session IDs
        scheduler.py                # Cron-style task scheduling
        config.py                   # Loads assistant config
  claude/
    home/                           # HOME env var target for claude processes
      .claude/
        settings.json               # Claude Code settings (MCPs, model, etc.)
        agents/                     # Native Claude Code agent definitions
          journalist.md
          researcher.md
    workspaces/                     # Per-agent working directories
      main/
        CLAUDE.md
      journalist/
        CLAUDE.md
      researcher/
        CLAUDE.md
    config/
      agents.yaml                   # Bot config: channel mappings + schedules
    data/
      sessions.json                 # Session ID persistence (JSON file)
```

## Config Isolation

Claude Code resolves `~/.claude/` from `$HOME`. The bot sets `HOME=<project>/claude/home` on all spawned `claude` processes. This gives the assistant system its own settings, agents, MCPs, and sessions without interfering with the user's personal `~/.claude/` config.

Initial setup requires: `HOME=<project>/claude/home claude auth`

## Agent Definitions

Agents have two layers of configuration:

### 1. Claude Code Agent Files

**Main agent** — has no agent definition file. Its personality and instructions live entirely in `workspaces/main/CLAUDE.md`. It runs as a bare `claude` process (no `--agent` flag), which gives it the default Claude Code system prompt plus the CLAUDE.md context.

**Sub-agents** — defined as native Claude Code `.md` files in `home/.claude/agents/<name>.md`. These define personality, tools, and model.

```markdown
---
name: journalist
description: News research and summarization agent
tools: Read, Write, Grep, Glob, Bash, WebSearch, WebFetch
model: sonnet
---

You are a journalist agent. You research news, track stories, and write summaries.

Your workspace is your current working directory. Use it to store notes, drafts, and reference material.
```

### 2. Bot Config (`claude/config/agents.yaml`)

Maps agents to Discord channels and defines schedules. Agent names must match filenames in `home/.claude/agents/`.

```yaml
discord:
  main_channel_id: "1234567890"       # Discord channel ID for main agent

agents:
  journalist:
    channel_id: "1234567891"          # Discord channel ID
    schedule:
      - cron: "0 9 * * *"
        prompt: "Check the news and write a summary of today's top stories"
  researcher:
    channel_id: "1234567892"
    schedule:
      - cron: "0 */6 * * *"
        prompt: "Check for updates on tracked topics and report findings"
```

Channel mappings use Discord channel IDs (not names) to avoid ambiguity.

## Main Agent

The main agent is a long-running Claude Code process with streaming I/O.

### Process Management

- First run: `claude --input-format stream-json --output-format stream-json --verbose`
- Subsequent runs: `claude --resume <session_id> --input-format stream-json --output-format stream-json --verbose`
- Environment: `HOME=<project>/claude/home`
- Working directory: `claude/workspaces/main/`
- Discord messages from the main channel are piped to stdin as stream-json
- Responses are collected until a `result` event is received, then posted to Discord as a single message (chunked if over 2000 chars)

### Stream-JSON Protocol

**Input format** — messages are sent to stdin as newline-delimited JSON:

```json
{"type": "user", "message": {"role": "user", "content": "user's message"}, "session_id": "default", "parent_tool_use_id": null}
```

For the first message, use `"session_id": "default"`. For subsequent messages, use the `session_id` captured from the `system/init` event.

**Output events** — stdout emits newline-delimited JSON with these key event types:

| Event | Description |
|-------|-------------|
| `{"type": "system", "subtype": "init", "session_id": "..."}` | First event, contains session ID |
| `{"type": "assistant", "message": {...}}` | Claude's response content |
| `{"type": "result", "subtype": "success", "result": "..."}` | Turn complete, contains final text |
| `{"type": "system", "subtype": "hook_started/hook_response"}` | Hook events (filter out) |

The bot reads output lines until a `result` event signals the turn is complete.

### Session ID Extraction

The `system/init` event (first event on process start) contains the `session_id`. The bot captures this and persists it to `sessions.json` for use with `--resume` on restart.

### Session Persistence

- Session IDs stored in `claude/data/sessions.json` (simple `{agent_name: session_id}` mapping)
- On restart, resumes previous session with `--resume <session_id>`
- If `--resume` fails, notifies on Discord ("Lost previous context, starting fresh") and starts a new session

### Commands

- `/reset` in `#main` — kills the main agent process, deletes its session ID, spawns fresh
- `/reset <agent>` in any channel — deletes the sub-agent's session ID; next invocation starts fresh and notifies the agent's channel

### Supervisor Loop

- Monitors the main agent process
- If it dies unexpectedly, restarts with `--resume` and posts notification to `#main`

### Sub-Agent Invocation

The main agent can invoke sub-agents inline via Claude Code's native Agent tool. This happens entirely within Claude Code — the bot doesn't orchestrate it. Results return to the main agent's conversation.

## Sub-Agents

Sub-agents run as one-shot `claude -p` processes but maintain session continuity via `--resume`.

### Invocation

Two triggers, same execution path:

1. **Discord message** — user sends message in the agent's channel
2. **Scheduled task** — cron fires, uses prompt from `agents.yaml`

Execution:
- First run (no stored session ID): `claude -p --agent <name> --output-format json "<prompt>"`
- Subsequent runs: `claude -p --agent <name> --resume <session_id> --output-format json "<prompt>"`
- Environment: `HOME=<project>/claude/home`
- Working directory: `claude/workspaces/<name>/`
- Output posted to the agent's Discord channel

### Session ID Extraction

The `claude -p` command with `--output-format json` returns a JSON object that includes `session_id`. The bot parses this from the output and stores it on first invocation.

### Concurrency

Each agent has an asyncio lock. Before invoking `claude -p` for an agent (from Discord or scheduler), the lock must be acquired. This prevents concurrent `--resume` calls on the same session.

```python
agent_locks: dict[str, asyncio.Lock]  # one per agent

async def invoke_agent(name: str, prompt: str) -> str:
    async with agent_locks[name]:
        result = await run_claude(name, prompt)
        return result
```

If a scheduled task fires while someone is chatting with the agent, it queues and executes after the current invocation finishes.

The main agent doesn't need locking — messages are serialized through its stdin pipe.

## Session Management

### Storage

A simple JSON file at `claude/data/sessions.json`:

```json
{
  "main": "session-uuid-1",
  "journalist": "session-uuid-2",
  "researcher": "session-uuid-3"
}
```

Updated atomically (write to temp file, rename) to avoid corruption.

### Lifecycle

1. On first invocation of an agent (no entry in `sessions.json`), run without `--resume`
2. Extract session ID from Claude's output, store in `sessions.json`
3. All subsequent invocations use `--resume <session_id>`
4. If `--resume` fails, notify on Discord, run without `--resume`, store new session ID
5. `/reset <agent>` deletes the entry from `sessions.json`

## Scheduling

APScheduler with `CronTrigger` for each entry in `agents.yaml`.

When a cron fires:
1. Read prompt from schedule config
2. Acquire agent's asyncio lock
3. Run `claude -p --agent <name> --resume <session_id> "<prompt>"`
4. Post output to agent's Discord channel
5. Release lock

Scheduled output always goes to the agent's dedicated channel.

## Message Routing

| Source | Destination |
|--------|-------------|
| Message in main channel | Piped to main agent's streaming process |
| Message in agent channel | `claude -p --agent <name> --resume <id> "<msg>"` |
| Message in unmapped channel | Ignored |
| Scheduled task output | Posted to agent's configured channel |

### Discord Details

- Bot responds in the same channel the message came in
- Long responses chunked at 2000 characters, split on newline boundaries to avoid breaking markdown/code blocks
- Typing indicator shown while waiting for response
- Messages from other bots ignored
- Required Discord intents: `message_content`, `guilds`
- Required permissions: Send Messages, Read Message History

## Context Compaction

Relies entirely on Claude Code's built-in context compression. No custom compaction system.

If a session becomes unrecoverable (resume fails), the bot notifies on Discord and starts fresh.

## Graceful Shutdown

On SIGTERM/SIGINT:
1. Stop the scheduler (no new tasks fire)
2. Wait for any in-flight sub-agent invocations to complete (with a timeout)
3. Cleanly terminate the main agent's streaming process
4. Save any pending session state

This ensures `--resume` works cleanly on next startup.

## Error Handling

| Scenario | Response |
|----------|----------|
| Main agent process dies | Supervisor restarts with `--resume`, notifies main channel |
| Sub-agent `--resume` fails | Notify in agent's channel, start fresh session |
| Claude CLI not found / auth expired | Post error to main channel, don't retry |
| Discord disconnection | discord.py handles reconnection automatically |
| Scheduled task fails | Post error output to agent's channel, next scheduled run retries naturally |

## Dependencies

- Python 3.12+
- discord.py
- APScheduler
- Claude Code CLI (installed and authenticated under `claude/home`)
