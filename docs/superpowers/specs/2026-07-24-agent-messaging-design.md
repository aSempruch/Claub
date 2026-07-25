# Agent-to-Agent Messaging — Design

**Date:** 2026-07-24
**Status:** Approved pending review

> **Amended 2026-07-24 (same branch):** the tool surface below —
> `send_message(to, …)` + `list_agents()` on one shared server at `/agents/mcp`,
> sender identified by the `X-Agent-Name` header — was replaced before merge by
> **one server per agent** mounted at `/agents/{name}/mcp`, exposing one
> `message_agent_{peer}` tool per reachable agent (sender comes from the mount
> path; `list_agents` is gone since the tool list is the roster). Everything else
> in this document — blocking semantics, wait-for graph, timeouts, watchdog
> exemption, debug isolation — is unchanged and still accurate.

## Overview

Agents in the same configured group can send messages to each other via an MCP tool.
The call is **blocking**: the receiver processes the message in its normal persistent
session (exactly as if it arrived from Discord, with a header identifying the sender
as an agent), and the receiver's response is returned to the sender as the tool
result. Nothing is posted to Discord by the exchange itself — visibility is inherited
from whatever triggered the sender's turn, so no new visibility conventions are
needed.

### Why blocking (decision record)

An async mailbox design was considered and rejected. Splitting one logical operation
("ask B, use the answer") across two turns created a cascade of new semantics: reply
relay bookkeeping, exactly-once delivery, a new turn type with its own Discord
visibility rules and `[NO_POST]`/`[POST]` conventions, synthesized error deliveries,
and ping-pong rate limiting. The blocking design eliminates all of that structurally:
the reply is a tool result inside the sender's existing turn, and the agent reasons
in one coherent chain. The cost is four small, contained safety mechanisms
(documented below), all of which live in code the bot already owns.

## Configuration

### `agents.yaml` — top-level `agent_groups`

```yaml
agent_groups:
  household: [main, career, journalist]
  research: [main, journalist]
```

- An agent's **reachable set** is the union of its co-members across all groups it
  appears in, minus itself.
- Validation at config load: every member must be a defined agent; a group with
  fewer than 2 members is a config error. Unknown/duplicate names are errors.
- Agents in no group get the MCP tools but every call returns an explanatory error.
- `AssistantConfig` gains `agent_groups: dict[str, list[str]]`.

### Instance config additions (documented, not in this repo)

- `config/mcp.json`: new `agents` server entry with
  `"url": "http://127.0.0.1:${CLAUB_MSG_PORT:-9400}/agents/mcp"` and the same
  `X-Agent-Name: ${CLAUB_AGENT_NAME}` header as the `schedules` entry (the
  env-default lets the debug CLI redirect to its own port without touching
  config).
- `config/settings.json`: add `mcp__agents__*` to `permissions.allow`.
- `config/CLAUDE.md`: short section explaining agent messages — the
  `[message from agent {name}]` header, that replies go back to the sending agent
  (not Discord), and that notifications marked "no reply expected" need no answer.
- `example/` in this repo is updated to match (agents.yaml group, mcp.json entry,
  settings.json allow entry, CLAUDE.md section).

## MCP surface

A second FastMCP app, `claub-agent-messaging`, mounted on the existing MCP port
(9400) at `/agents/mcp` alongside the schedules app at `/mcp` (unchanged, so
existing configs keep working). Both apps are served by one uvicorn via a Starlette
mount. The `agents` server key in mcp.json yields tool names:

### `mcp__agents__send_message(to: str, message: str, expect_reply: bool = True) -> str`

Blocking send. Steps:

1. Resolve sender from `X-Agent-Name`; error if missing.
2. Authorize: `to` must be in the sender's reachable set and not the sender itself.
   On failure, the error lists the sender's reachable agents.
3. Cycle/depth check against the wait-for graph (see Safety). Reject with a clear
   error, e.g. `"Error: career is currently waiting on your reply — answer
   directly instead of sending a new message."`
4. Register `waiting_on[sender] = to`, set the sender process's waiting flag.
5. Deliver via the injected `get_process` callable using the same
   send-with-restart semantics as Discord and scheduled messages (same session,
   restart-and-retry-once, `_last_activity` updates), with the message formatted
   as `[message from agent {sender}] {message}`. Persist the receiver's session
   ID afterwards via the injected `persist_session` (SessionStore in
   production, no-op in debug).
6. `expect_reply=True`: await the result with a **15-minute cap** and return the
   receiver's response text as the tool result.
   `expect_reply=False`: fire-and-forget — spawn the delivery as a background
   task, return `"Message sent to {to} (no reply requested)."` immediately. The
   header becomes `[notification from agent {sender} — no reply expected]` and the
   receiver's output is logged and discarded.
7. `finally`: unregister from the wait graph, clear the waiting flag.

### `mcp__agents__list_agents() -> str`

Returns the sender's reachable agents as JSON: name plus the `description` from
each agent's `.md` frontmatter (empty description if no agent file).

## Safety mechanisms

### 1. Sender inactivity-timer exemption

While A blocks on the tool call, A's claude process emits no stream-json events,
which would trip `_read_until_result`'s 300 s inactivity timeout and get A declared
wedged. Fix: `AgentProcess` gains an `awaiting_agent_reply` flag (set/cleared by the
tool handler via the bot's process registry). On inactivity timeout, the reader
checks the flag and continues waiting instead of raising. The flag is always cleared
in the handler's `finally`, and the wait cap (15 min) bounds how long the exemption
can hold. The sender's overall turn timeout (3600 s) still applies.

The idle reaper needs no change: a blocked sender holds its stream lock, so
`process.busy` already protects it (the reaper checks `busy` before even
consulting `can_stop` hooks). The existing Playwright reaper-veto is a different
subsystem — a `can_stop` shell hook curling the bridge's `/can-stop/<agent>`
endpoint, capped by `CLAUB_MAX_PIN_SECS` — and is not involved here, but it sets
the codebase precedent this mechanism follows: a keep-alive exemption bounded by
a hard cap (there 48 h of veto pinning, here the 15-minute wait cap).

### 2. Cycle and depth rejection

The bot owns a wait-for graph `_waiting_on: dict[str, str]` (sender → target).
Each agent turn performs at most one blocking send at a time, so each node has at
most one outgoing edge; multiple senders may wait on one target (they queue on its
stream lock). On a send request X→Y: walk downstream from Y; if the walk reaches X,
reject (cycle). Also compute X's upstream chain depth (who is waiting on X,
transitively) and reject if the resulting chain would exceed **depth 3**.

Check-and-register happens with no `await` in between (single-threaded asyncio), so
the simultaneous case A→B while B→A resolves deterministically: the second
registrant sees the cycle, gets an immediate error, finishes its turn, and its lock
frees for the queued message.

### 3. Wait cap with shielded delivery

The 15-minute cap applies to the *sender's wait*, not the receiver's turn. The
delivery coroutine is shielded and runs to completion in the background even if the
wait times out — cancelling `send_message` mid-stream would desync the receiver's
stream-json pipe (unread events left in it). On timeout the sender receives an
error result stating the message was delivered but the reply took too long and was
discarded; the receiver's session ID is still persisted when its turn finishes.

### 4. CLI-side MCP tool timeout

`AgentProcess._env()` sets `MCP_TOOL_TIMEOUT` (milliseconds) to a value above the
wait cap (20 min) via `setdefault`, so Claude Code doesn't kill the pending tool
call before the bot's own cap fires.

## Discord visibility

- The receiver's response to an agent message goes only to the sender as the tool
  result — never to the receiver's channel.
- The sender's turn output follows whatever rules already govern that turn: a
  user-triggered turn posts normally (the reply arrives in context as part of
  answering the user); a scheduled turn follows the existing `[NO_POST]`
  convention. **No new visibility rules are introduced.**

## Debug CLI support (debug isolation propagates)

The debug CLI (`claude_assistant.debug_agent`) currently passes the production
MCP configs verbatim, so a debugged agent calling `send_message` would hit the
live bot's MCP server on :9400 and pollute the *real* receiver's session. Agent
messaging must instead be fully debug-isolated:

- **Callback-injected server factory** (same pattern `create_mcp_server`
  already uses for schedules): `create_messaging_server(groups, get_process,
  persist_session)`. The wait graph lives behind the factory. Production passes
  the bot's registry-backed get-or-start and `SessionStore.set`; the debug CLI
  passes `_build_process(debug=True)`-backed supply (fresh session,
  `--no-session-persistence`, started lazily, stopped on CLI exit) and a no-op
  persister. No separate "hub" abstraction.
- **Port selection via env expansion — no config rewriting.** The instance
  `mcp.json` entry uses `"url": "http://127.0.0.1:${CLAUB_MSG_PORT:-9400}/agents/mcp"`.
  Claude Code expands `${VAR:-default}` in MCP config files from the process
  environment — the mechanism the existing `X-Agent-Name: ${CLAUB_AGENT_NAME}`
  header already relies on in this deployment. Production sets nothing (default
  9400). The debug CLI starts its own messaging server on an ephemeral port and
  sets `CLAUB_MSG_PORT` in `os.environ`; since `AgentProcess._env()` copies the
  parent environment, every receiver spawned in the debug run inherits it, so
  isolation propagates transitively (A→B→C) with zero file manipulation.
- `sessions.json` is never read or written anywhere in a debug run.

## Error handling

- Delivery failure (receiver process fails even after `_send_with_restart`'s
  restart-and-retry): the tool returns an error string to the sender.
- `AuthenticationError` on the receiver: tool returns an error telling the sender
  the receiver couldn't run; the existing auth-notification path is not duplicated.
- Dead/idle receiver process: `_send_with_restart` already lazily starts it.
- Unknown `X-Agent-Name`, unknown target, self-send, not-in-group: descriptive
  error strings (no exceptions across the MCP boundary).

## Code changes

| File | Change |
|---|---|
| `bot/src/claude_assistant/config.py` | Parse + validate `agent_groups`; reachable-set helper |
| `bot/src/claude_assistant/agent_messaging.py` (new) | `create_messaging_server(groups, get_process, persist_session)` — wait graph, waiting flag, delivery, tools `send_message` / `list_agents` |
| `bot/src/claude_assistant/discord_bot.py` | Pass registry-backed callables to the factory; mount both MCP apps under one uvicorn in `_start_mcp_server` |
| `bot/src/claude_assistant/debug_agent.py` | Start debug messaging server on ephemeral port, set `CLAUB_MSG_PORT`, debug-backed callables (isolated receivers, no-op persistence), receiver cleanup on exit |
| `bot/src/claude_assistant/claude_process.py` | `awaiting_agent_reply` flag honored in `_read_until_result`; `MCP_TOOL_TIMEOUT` in `_env()` |
| `bot/tests/` | See Testing |
| `CLAUDE.md`, `example/` | Docs + sanitized config examples |

## Testing

Unit tests following the existing fake-process patterns in `bot/tests/`:

- Config: group parsing, unknown member, <2 members, multi-group union, reachable
  set excludes self.
- Authorization: not in any group, target outside reachable set, self-send,
  missing header.
- Wait graph: A→B→A rejected; A→B→C allowed; depth >3 rejected; simultaneous
  A→B / B→A — exactly one rejected; graph entries cleaned up on success, timeout,
  and exception.
- Timeout: wait cap exceeded (patched small) → sender gets timeout error, delivery
  still completes, receiver session persisted.
- Inactivity exemption: reader survives silence past `inactivity_timeout` while
  flag set; still raises when flag clear.
- Fire-and-forget: immediate return, notification header used, no result relayed.
- Header format: `[message from agent X]` prefix present exactly once.
- Debug isolation: debug callables never touch `SessionStore`; receiver
  processes built with `debug=True`; `CLAUB_MSG_PORT` set in the CLI's
  environment is inherited by spawned receivers (transitive redirection).

### End-to-end verification via the debug CLI (required testing step)

Run inside the container against two agents sharing a group in the live config:

```bash
docker exec claude-claub-1 uv run --project /app/bot \
  python -m claude_assistant.debug_agent main \
  -p "Use your agent messaging tool to ask journalist what 2+2 is, and report their reply verbatim."
```

Verify:

1. The sender's output contains the receiver's actual reply (round-trip works).
2. `/claub/data/sessions.json` is byte-identical before and after the run
   (debug isolation held for both sender and receiver).
3. Nothing was posted to any Discord channel.
4. No stray `claude` processes remain after the CLI exits (receivers cleaned up).
5. `list_agents` from the debugged sender returns the expected reachable set.

## Out of scope (deliberate)

- Rate limiting of agent messages (cycle rejection + depth cap + turn timeouts
  bound the blast radius; revisit only if real abuse appears).
- Hybrid sync-then-async fallback.
- Cross-instance messaging (other Claub deployments).
- Message queues/mailboxes, broadcast to a whole group.
