---
name: claub-schedules
description: Use when working on Claub's scheduling — schedules.json shape, cron density limits, one-shot schedules, firing history retention, the schedules MCP tools, or diagnosing why a schedule was rejected or never fired
---

# Claub Schedules

Schedules are managed **dynamically at runtime** via the embedded MCP server — never in `agents.yaml`. Agents create, list, and delete their own cron schedules with `mcp__schedules__list_schedules`, `mcp__schedules__create_schedule`, `mcp__schedules__delete_schedule`.

## Persistence

`/claub/data/schedules.json` — machine-managed, **not hand-edited**:

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

APScheduler syncs from this file on startup and immediately on every mutation — no polling, no restart needed.

## Behavior

- **`one_shot: true`** — fires once, then auto-deletes. Deletion happens **before** execution so a crash mid-run can't cause a duplicate firing on recovery.
- **Server:** `127.0.0.1:9400`, configurable via `CLAUB_MCP_PORT`.
- **Identity:** the agent name arrives in the `X-Agent-Name` HTTP header, resolved from the `${CLAUB_AGENT_NAME}` env var set in each agent's process. (Agent *messaging* differs — it uses the mount path instead. See `docs/superpowers/specs/2026-07-24-agent-messaging-design.md`.)
- **Notification:** every schedule change posts to the agent's Discord channel.

## Density Limits

Schedule creation is **globally** rate-limited across all agents combined:

| Window | Max firings |
|---|---|
| Rolling 24 h | 5 |
| Rolling 7 d | 30 |

The check considers **both** projected future fire times (120-day horizon) **and** recent firing history. A creation that looks harmless in isolation can still be rejected because other agents already filled the window.

## Firing History

All firings log to `/claub/data/firing_history.json` for debugging. Retention via `CLAUB_SCHEDULE_HISTORY_RETENTION_DAYS` (default 30).

## Design Intent

Agents are meant to use **one-shot schedules with natural time variation** rather than rigid recurring cron. A lognormal jitter model plus beta-distributed skip probability makes check-ins feel organic instead of robotic.

## Diagnosing

| Symptom | Look for |
|---|---|
| Creation rejected | Density limit — check `firing_history.json` and other agents' projected fires |
| Schedule never fires | `Skipping orphaned schedules` (agent not in `agents.yaml`) or missing `Scheduler started with N jobs` |
| Fired twice | One-shot deletion ordering — should delete before execute |
| Fires late by seconds | Normal — jitter delay, logged as `delaying Ns` |

Log lines for all of these are catalogued in the `claub-logs` skill.
