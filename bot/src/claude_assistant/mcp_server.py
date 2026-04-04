"""FastMCP server exposing schedule management tools to agents.

Each tool extracts the requesting agent's name from the ``X-Agent-Name`` HTTP
header (set by the bot when it spawns agent processes).  All mutations are
guarded by ``store.lock`` so concurrent requests don't corrupt the schedule
file.

Usage::

    from claude_assistant.mcp_server import create_mcp_server

    server = create_mcp_server(store, scheduler)
    asgi_app = server.http_app()   # mount into uvicorn / starlette
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import fastmcp
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter
from starlette.requests import Request

from claude_assistant.firing_history import FiringHistory
from claude_assistant.schedule_store import ScheduleStore
from claude_assistant.scheduler import Scheduler

log = logging.getLogger(__name__)

# Optional callback: (agent_name, message) -> Awaitable[None]
NotifyCallback = Callable[[str, str], Awaitable[None]] | None

# ---------------------------------------------------------------------------
# Schedule density constants
# ---------------------------------------------------------------------------

MAX_FIRINGS_PER_DAY = 5
MAX_FIRINGS_PER_WEEK = 30
DENSITY_HORIZON_DAYS = 120


def _now() -> datetime:
    """Return current time. Extracted for test patching."""
    return datetime.now()


def check_schedule_density(
    store: ScheduleStore,
    history: FiringHistory,
    new_cron: str,
    one_shot: bool,
    max_per_day: int = MAX_FIRINGS_PER_DAY,
    max_per_week: int = MAX_FIRINGS_PER_WEEK,
    horizon_days: int = DENSITY_HORIZON_DAYS,
) -> str | None:
    """Check if adding a new schedule would exceed daily or weekly firing limits.

    Combines projected future fire times from all existing schedules (plus the
    proposed new one) with recent firing history. Only rejects if the *new*
    schedule's fires actually appear in an overloaded window — pre-existing
    violations from history don't block unrelated new schedules.
    """
    now = _now()
    horizon = now + timedelta(days=horizon_days)

    def _project(cron_expr: str, is_one_shot: bool) -> list[datetime]:
        times: list[datetime] = []
        it = croniter(cron_expr, now)
        if is_one_shot:
            nxt = it.get_next(datetime)
            if nxt < horizon:
                times.append(nxt)
        else:
            while True:
                nxt = it.get_next(datetime)
                if nxt >= horizon:
                    break
                times.append(nxt)
        return times

    # Project existing schedules + history (tagged False)
    existing_fires: list[datetime] = []
    for agent_entries in store.all().values():
        for e in agent_entries:
            existing_fires.extend(_project(e["cron"], e["one_shot"]))
    for firing in history.recent(days=7):
        if firing.get("skipped"):
            continue
        existing_fires.append(datetime.fromisoformat(firing["fired_at"]))

    # Project new schedule (tagged True)
    new_fires = _project(new_cron, one_shot)

    # Merge with origin tags and sort by time
    tagged = [(t, False) for t in existing_fires] + [(t, True) for t in new_fires]
    tagged.sort(key=lambda x: x[0])

    fire_times = [t for t, _ in tagged]
    is_new = [flag for _, flag in tagged]
    n = len(fire_times)

    # Sliding window checks using two-pointer for O(n).
    # Only flag violations in windows that contain at least one new fire,
    # so pre-existing over-limit windows don't block unrelated new schedules.
    day_delta = timedelta(hours=24)
    week_delta = timedelta(days=7)

    day_end = 0
    week_end = 0
    new_in_day = 0
    new_in_week = 0

    for i in range(n):
        t = fire_times[i]

        # Advance day pointer to first element outside [t, t+24h)
        while day_end < n and fire_times[day_end] < t + day_delta:
            if is_new[day_end]:
                new_in_day += 1
            day_end += 1
        day_count = day_end - i
        if day_count > max_per_day and new_in_day > 0:
            return (
                f"Error: adding this schedule would cause {day_count} firings "
                f"within 24h of {t.strftime('%Y-%m-%d %H:%M')} (max {max_per_day}). "
                f"This count includes schedules from all agents globally — some may "
                f"belong to other agents you can't control. Consider spreading your "
                f"schedules further apart or removing existing ones first."
            )

        # Advance week pointer to first element outside [t, t+7d)
        while week_end < n and fire_times[week_end] < t + week_delta:
            if is_new[week_end]:
                new_in_week += 1
            week_end += 1
        week_count = week_end - i
        if week_count > max_per_week and new_in_week > 0:
            return (
                f"Error: adding this schedule would cause {week_count} firings "
                f"within 7 days of {t.strftime('%Y-%m-%d %H:%M')} (max {max_per_week}). "
                f"This count includes schedules from all agents globally — some may "
                f"belong to other agents you can't control. Consider spreading your "
                f"schedules further apart or removing existing ones first."
            )

        # Element i leaves both windows as i advances to next iteration
        if is_new[i]:
            new_in_day -= 1
            new_in_week -= 1

    return None


# ---------------------------------------------------------------------------
# Business-logic helpers (pure async functions, easy to unit-test directly)
# ---------------------------------------------------------------------------


async def _list_schedules(agent: str, store: ScheduleStore) -> str:
    """Return a JSON-encoded list of schedules for *agent*."""
    entries = store.list(agent)
    return json.dumps(entries)


async def _create_schedule(
    agent: str,
    cron: str,
    prompt: str,
    one_shot: bool,
    store: ScheduleStore,
    scheduler: Scheduler,
    notify: NotifyCallback = None,
    history: FiringHistory | None = None,
) -> str:
    """Validate *cron*, persist the entry, register the APScheduler job.

    Returns a human-readable confirmation string containing the new entry ID,
    or an error message if *cron* is invalid.
    """
    # Validate cron expression before touching persistent state
    try:
        CronTrigger.from_crontab(cron)
    except Exception as exc:
        return f"Error: invalid cron expression {cron!r} — {exc}"

    # Density check
    if history is not None:
        density_error = check_schedule_density(store, history, cron, one_shot)
        if density_error:
            return density_error

    async with store.lock:
        entry = store.create(agent, cron=cron, prompt=prompt, one_shot=one_shot)
        scheduler.add_job(agent, entry["id"], cron, prompt, one_shot=one_shot)

    log.info("Created schedule %s for %s: %r", entry["id"], agent, prompt)
    shot_label = "one-shot " if one_shot else ""
    msg = f"Schedule created: {shot_label}`{cron}` — {prompt}"
    if notify and not one_shot:
        await notify(agent, msg)
    return f"Created schedule {entry['id']}: {prompt!r} at {cron}"


async def _delete_schedule(
    agent: str,
    entry_id: str,
    store: ScheduleStore,
    scheduler: Scheduler,
    notify: NotifyCallback = None,
) -> str:
    """Remove the entry from the store and de-register its APScheduler job.

    Returns a confirmation string, or an error message if the entry is not
    found for *agent*.
    """
    # Grab entry details before deleting (for notification)
    entries = store.list(agent)
    entry_info = next((e for e in entries if e["id"] == entry_id), None)

    async with store.lock:
        deleted = store.delete(agent, entry_id)
        if not deleted:
            return f"Error: schedule {entry_id!r} not found for agent {agent!r}"
        scheduler.remove_job(agent, entry_id)

    log.info("Deleted schedule %s for %s", entry_id, agent)
    if notify and entry_info:
        msg = f"Schedule deleted: `{entry_info['cron']}` — {entry_info['prompt']}"
        await notify(agent, msg)
    return f"Deleted schedule {entry_id}"


# ---------------------------------------------------------------------------
# FastMCP server factory
# ---------------------------------------------------------------------------


def create_mcp_server(
    store: ScheduleStore,
    scheduler: Scheduler,
    notify: NotifyCallback = None,
    history: FiringHistory | None = None,
) -> fastmcp.FastMCP:
    """Create and return a FastMCP server with schedule management tools.

    The returned server exposes three tools:

    * ``list_schedules`` — list schedules for the requesting agent
    * ``create_schedule`` — create a new recurring or one-shot schedule
    * ``delete_schedule`` — remove a schedule by ID

    The agent name is read from the ``X-Agent-Name`` HTTP request header.
    """
    mcp = fastmcp.FastMCP(name="claub-schedule-manager")

    def _get_agent(request: Request) -> str:
        return request.headers.get("x-agent-name", "")

    @mcp.tool()
    async def list_schedules(request: Request = fastmcp.server.dependencies.CurrentRequest()) -> str:  # type: ignore[assignment]
        """List all schedules for the requesting agent."""
        agent = _get_agent(request)
        if not agent:
            return "Error: missing X-Agent-Name header"
        return await _list_schedules(agent, store)

    @mcp.tool()
    async def create_schedule(
        cron: str,
        prompt: str,
        one_shot: bool = True,
        request: Request = fastmcp.server.dependencies.CurrentRequest(),  # type: ignore[assignment]
    ) -> str:
        """Create a new schedule for the requesting agent.

        **Prefer one-shot schedules.** Instead of setting a fixed recurring
        schedule, create a one-shot for the next time you want to act, then
        schedule the *next* one-shot at the end of that task (varying the
        time naturally). This makes your behavior feel like a real assistant
        who checks in when it makes sense, not a cron job firing at the
        same time every day. Recurring schedules (one_shot=False) should
        only be used when strict periodicity is genuinely required.

        **Note:** Recurring schedules (one_shot=False) may occasionally
        skip a firing. This is by design — do not treat a missed firing
        as an error or attempt to compensate for it.

        Args:
            cron: Standard 5-field cron expression (e.g. ``30 9 * * 1``).
            prompt: The message that will be sent to the agent when the
                schedule fires.  Do not include ``[scheduled]`` — the bot
                adds that prefix automatically.
            one_shot: When *True* (the default) the schedule fires exactly
                once and is then automatically removed. Set to *False* only
                when strict recurring periodicity is required.
        """
        agent = _get_agent(request)
        if not agent:
            return "Error: missing X-Agent-Name header"
        return await _create_schedule(agent, cron, prompt, one_shot, store, scheduler, notify, history)

    @mcp.tool()
    async def delete_schedule(
        id: str,
        request: Request = fastmcp.server.dependencies.CurrentRequest(),  # type: ignore[assignment]
    ) -> str:
        """Delete a schedule by ID for the requesting agent.

        Args:
            id: The schedule entry ID returned by ``create_schedule`` or
                ``list_schedules``.
        """
        agent = _get_agent(request)
        if not agent:
            return "Error: missing X-Agent-Name header"
        return await _delete_schedule(agent, id, store, scheduler, notify)

    return mcp
