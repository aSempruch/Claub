# Schedule Density Validation Design

**Date:** 2026-03-28
**Status:** Draft

## Problem

Agents can create unlimited cron schedules via the MCP schedule tools. Nothing prevents an agent from scheduling 20 firings in a single day, or creating a chain of one-shots that each schedule the next one 10 minutes out — effectively bypassing any per-creation limit. We need a system that caps total schedule firing volume across all agents globally.

## Solution Overview

Two new components:

1. **Firing history** — a persistent log of when schedules actually fired, used to detect one-shot chaining and provide general debugging visibility.
2. **Density validation** — a check at schedule creation time that combines projected future fire times with recent firing history, then enforces daily and weekly limits using a sliding window.

## Constants

| Constant | Value | Location |
|---|---|---|
| `MAX_FIRINGS_PER_DAY` | 5 | `mcp_server.py` |
| `MAX_FIRINGS_PER_WEEK` | 20 | `mcp_server.py` |
| `DENSITY_HORIZON_DAYS` | 120 | `mcp_server.py` |
| `FIRING_HISTORY_RETENTION_DAYS` | 30 | `firing_history.py` (default; overridable via `CLAUB_SCHEDULE_HISTORY_RETENTION_DAYS` env var) |

## Component 1: Firing History

**File:** `bot/src/claude_assistant/firing_history.py`

**Class:** `FiringHistory` — atomic JSON persistence, same pattern as `ScheduleStore`.

```python
class FiringHistory:
    def __init__(self, path: Path, retention_days: int = FIRING_HISTORY_RETENTION_DAYS):
        ...

    def record(self, agent: str, schedule_id: str, cron: str, prompt: str, one_shot: bool) -> None:
        """Append a firing entry and prune entries older than retention_days."""

    def recent(self, days: int = 7) -> list[dict]:
        """Return firings from the last N days."""

    def all(self) -> list[dict]:
        """Return all retained firings."""
```

**Storage:** `/claub/data/firing_history.json`

```json
{
  "firings": [
    {
      "agent": "journalist",
      "schedule_id": "a1b2c3",
      "cron": "0 9 * * *",
      "prompt": "Check for breaking news",
      "one_shot": false,
      "fired_at": "2026-03-28T09:12:00"
    }
  ]
}
```

**Pruning:** On every `record()` call, entries with `fired_at` older than `retention_days` from now are dropped. The `retention_days` parameter is configurable at instantiation.

**Recording points** in `scheduler.py`:
- `_run()` — after the callback completes, call `history.record(...)`
- `_run_one_shot()` — after the callback completes, call `history.record(...)`

**Wiring:** `FiringHistory` is instantiated in `main.py` alongside `ScheduleStore`, stored at `/claub/data/firing_history.json`. Retention is read from the `CLAUB_SCHEDULE_HISTORY_RETENTION_DAYS` env var (default 30). Passed to both `Scheduler` (for recording) and `create_mcp_server` (for the density check).

## Component 2: Density Validation

**Location:** Helper function in `mcp_server.py`.

```python
def check_schedule_density(
    store: ScheduleStore,
    history: FiringHistory,
    new_cron: str,
    one_shot: bool,
    max_per_day: int = MAX_FIRINGS_PER_DAY,
    max_per_week: int = MAX_FIRINGS_PER_WEEK,
    horizon_days: int = DENSITY_HORIZON_DAYS,
) -> str | None:
```

**Returns:** `None` if the new schedule is allowed, or an error string if it would exceed limits.

**Algorithm:**

1. Collect all entries from `store.all()` across all agents as `(cron, one_shot)` tuples.
2. Add `(new_cron, one_shot)` to the list.
3. Compute `now` and `horizon = now + timedelta(days=horizon_days)`.
4. Project fire times for each entry using `croniter`:
   - One-shot: single `croniter(cron, now).get_next(datetime)`. Include only if before horizon.
   - Recurring: iterate `get_next(datetime)` until past horizon, collecting all times.
5. Add recent firing history: `history.recent(days=7)` parsed into datetime objects, merged into the list.
6. Sort all fire times.
7. **Daily window:** For each time `t`, count times in `[t, t+24h)`. If count > `max_per_day`, return daily error.
8. **Weekly window:** For each time `t`, count times in `[t, t+168h)`. If count > `max_per_week`, return weekly error.
9. Return `None`.

**One-shot projection:** A one-shot has exactly one future fire time. Since it's deleted from the store after firing, it only contributes to the projected set while it exists. Past one-shot firings are captured by the firing history.

## Integration Point

In `_create_schedule` in `mcp_server.py`, between the cron syntax check and the persistent store mutation:

```python
# Validate cron syntax (existing)
try:
    CronTrigger.from_crontab(cron)
except Exception as exc:
    return f"Error: invalid cron expression {cron!r} — {exc}"

# Density check (new)
density_error = check_schedule_density(store, history, cron, one_shot)
if density_error:
    return density_error

# Persist (existing)
async with store.lock:
    entry = store.create(agent, cron=cron, prompt=prompt, one_shot=one_shot)
    ...
```

The density check reads from the store but does not mutate it, so it does not need to be inside the async lock.

## Error Messages

**Daily limit exceeded:**
> Error: adding this schedule would cause {n} firings within 24h of {timestamp} (max {max_per_day}). This count includes schedules from all agents globally — some may belong to other agents you can't control. Consider spreading your schedules further apart or removing existing ones first.

**Weekly limit exceeded:**
> Error: adding this schedule would cause {n} firings within 7 days of {timestamp} (max {max_per_week}). This count includes schedules from all agents globally — some may belong to other agents you can't control. Consider spreading your schedules further apart or removing existing ones first.

## Dependency Graph

```
main.py
  ├── creates FiringHistory(path, retention_days)
  ├── passes to Scheduler(store, callback, history, ...)
  └── passes to create_mcp_server(store, scheduler, history, ...)

mcp_server.py  (density check + MCP tools)
    ├── reads from ScheduleStore  (projected future firings)
    └── reads from FiringHistory  (past firings)

scheduler.py  (cron execution)
    └── writes to FiringHistory   (records each firing)
```

## New Dependency

`croniter` added to `bot/pyproject.toml` dependencies.

## Tests

Unit tests for `check_schedule_density`:
- Under both limits — passes
- Daily limit exceeded by recurring schedules — returns daily error
- Weekly limit exceeded while daily is fine — returns weekly error
- Cluster of one-shots far in the future — caught by daily check
- Recent firing history tips the count over — caught (one-shot chaining scenario)
- Empty store — always passes

Unit tests for `FiringHistory`:
- `record()` appends entry with correct fields
- `record()` prunes entries older than retention period
- `recent(days=N)` filters correctly
- `all()` returns everything within retention
- Atomic write — no corruption on concurrent access
