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

import fastmcp
from apscheduler.triggers.cron import CronTrigger
from starlette.requests import Request

from claude_assistant.schedule_store import ScheduleStore
from claude_assistant.scheduler import Scheduler

log = logging.getLogger(__name__)


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

    async with store.lock:
        entry = store.create(agent, cron=cron, prompt=prompt, one_shot=one_shot)
        scheduler.add_job(agent, entry["id"], cron, prompt, one_shot=one_shot)

    log.info("Created schedule %s for %s: %r", entry["id"], agent, prompt)
    return f"Created schedule {entry['id']}: {prompt!r} at {cron}"


async def _delete_schedule(
    agent: str,
    entry_id: str,
    store: ScheduleStore,
    scheduler: Scheduler,
) -> str:
    """Remove the entry from the store and de-register its APScheduler job.

    Returns a confirmation string, or an error message if the entry is not
    found for *agent*.
    """
    async with store.lock:
        deleted = store.delete(agent, entry_id)
        if not deleted:
            return f"Error: schedule {entry_id!r} not found for agent {agent!r}"
        scheduler.remove_job(agent, entry_id)

    log.info("Deleted schedule %s for %s", entry_id, agent)
    return f"Deleted schedule {entry_id}"


# ---------------------------------------------------------------------------
# FastMCP server factory
# ---------------------------------------------------------------------------


def create_mcp_server(store: ScheduleStore, scheduler: Scheduler) -> fastmcp.FastMCP:
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
        one_shot: bool = False,
        request: Request = fastmcp.server.dependencies.CurrentRequest(),  # type: ignore[assignment]
    ) -> str:
        """Create a new schedule for the requesting agent.

        Args:
            cron: Standard 5-field cron expression (e.g. ``0 9 * * *``).
            prompt: The message that will be sent to the agent when the
                schedule fires.  Do not include ``[scheduled]`` — the bot
                adds that prefix automatically.
            one_shot: When *True* the schedule fires exactly once and is then
                automatically removed.
        """
        agent = _get_agent(request)
        if not agent:
            return "Error: missing X-Agent-Name header"
        return await _create_schedule(agent, cron, prompt, one_shot, store, scheduler)

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
        return await _delete_schedule(agent, id, store, scheduler)

    return mcp
