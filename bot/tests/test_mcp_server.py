"""Tests for the FastMCP schedule management server.

These tests focus on the store+scheduler integration that the tools orchestrate,
calling the underlying helper functions directly without a running HTTP server.
Full HTTP integration is tested in Task 9.
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

from apscheduler.triggers.cron import CronTrigger

from claude_assistant.firing_history import FiringHistory
from claude_assistant.schedule_store import ScheduleStore
from claude_assistant.scheduler import Scheduler
from claude_assistant.mcp_server import create_mcp_server, _list_schedules, _create_schedule, _delete_schedule


@pytest.fixture
def store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.json")


@pytest.fixture
def history(tmp_path: Path) -> FiringHistory:
    return FiringHistory(tmp_path / "firing_history.json")


@pytest.fixture
def scheduler(store: ScheduleStore) -> MagicMock:
    mock = MagicMock(spec=Scheduler)
    return mock


# --- create_mcp_server factory ---


def test_create_mcp_server_returns_fastmcp(store: ScheduleStore, scheduler: MagicMock) -> None:
    import fastmcp
    server = create_mcp_server(store, scheduler)
    assert isinstance(server, fastmcp.FastMCP)


@pytest.mark.asyncio
async def test_create_mcp_server_has_tools(store: ScheduleStore, scheduler: MagicMock) -> None:
    server = create_mcp_server(store, scheduler)
    # FastMCP v2 exposes registered tools via the async list_tools() method
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    assert "list_schedules" in tool_names
    assert "create_schedule" in tool_names
    assert "delete_schedule" in tool_names


# --- _list_schedules ---


@pytest.mark.asyncio
async def test_list_schedules_empty(store: ScheduleStore, scheduler: MagicMock) -> None:
    result = await _list_schedules("main", store)
    data = json.loads(result)
    assert data == []


@pytest.mark.asyncio
async def test_list_schedules_returns_entries(store: ScheduleStore, scheduler: MagicMock) -> None:
    store.create("main", cron="0 9 * * *", prompt="morning check", one_shot=False)
    store.create("main", cron="0 17 * * *", prompt="evening check", one_shot=True)
    result = await _list_schedules("main", store)
    data = json.loads(result)
    assert len(data) == 2
    prompts = {e["prompt"] for e in data}
    assert "morning check" in prompts
    assert "evening check" in prompts


@pytest.mark.asyncio
async def test_list_schedules_scoped_to_agent(store: ScheduleStore, scheduler: MagicMock) -> None:
    store.create("main", cron="0 9 * * *", prompt="for main", one_shot=False)
    store.create("journalist", cron="0 10 * * *", prompt="for journalist", one_shot=False)
    result = await _list_schedules("main", store)
    data = json.loads(result)
    assert len(data) == 1
    assert data[0]["prompt"] == "for main"


# --- _create_schedule ---


@pytest.mark.asyncio
async def test_create_schedule_calls_store_and_scheduler(
    store: ScheduleStore, scheduler: MagicMock
) -> None:
    result = await _create_schedule(
        agent="main",
        cron="0 9 * * *",
        prompt="morning standup",
        one_shot=False,
        store=store,
        scheduler=scheduler,
    )
    # Store should have the entry
    entries = store.list("main")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["cron"] == "0 9 * * *"
    assert entry["prompt"] == "morning standup"
    assert entry["one_shot"] is False

    # Scheduler.add_job should be called with matching args
    scheduler.add_job.assert_called_once_with(
        "main", entry["id"], "0 9 * * *", "morning standup", one_shot=False
    )

    # Return value is a confirmation string containing the id
    assert entry["id"] in result


@pytest.mark.asyncio
async def test_create_schedule_one_shot(store: ScheduleStore, scheduler: MagicMock) -> None:
    await _create_schedule(
        agent="main",
        cron="30 14 * * 5",
        prompt="friday report",
        one_shot=True,
        store=store,
        scheduler=scheduler,
    )
    entries = store.list("main")
    assert entries[0]["one_shot"] is True
    call_kwargs = scheduler.add_job.call_args
    assert call_kwargs.kwargs["one_shot"] is True


@pytest.mark.asyncio
async def test_create_schedule_scoped_to_agent(
    store: ScheduleStore, scheduler: MagicMock
) -> None:
    await _create_schedule(
        agent="journalist",
        cron="0 8 * * *",
        prompt="news brief",
        one_shot=False,
        store=store,
        scheduler=scheduler,
    )
    assert store.list("journalist") != []
    assert store.list("main") == []


@pytest.mark.asyncio
async def test_create_schedule_invalid_cron_rejected(
    store: ScheduleStore, scheduler: MagicMock
) -> None:
    result = await _create_schedule(
        agent="main",
        cron="not a cron",
        prompt="bad schedule",
        one_shot=False,
        store=store,
        scheduler=scheduler,
    )
    # No store entry created
    assert store.list("main") == []
    # Scheduler not called
    scheduler.add_job.assert_not_called()
    # Error indicated in return value
    assert "invalid" in result.lower() or "error" in result.lower()


@pytest.mark.asyncio
async def test_create_schedule_rejected_by_density(
    store: ScheduleStore, scheduler: MagicMock, history: FiringHistory
) -> None:
    # Fill up to daily limit
    store.create("main", cron="0 8 * * *", prompt="a", one_shot=False)
    store.create("main", cron="0 10 * * *", prompt="b", one_shot=False)
    store.create("main", cron="0 12 * * *", prompt="c", one_shot=False)
    store.create("main", cron="0 14 * * *", prompt="d", one_shot=False)
    store.create("main", cron="0 16 * * *", prompt="e", one_shot=False)
    # 6th should be rejected
    result = await _create_schedule(
        agent="main",
        cron="0 18 * * *",
        prompt="too many",
        one_shot=False,
        store=store,
        scheduler=scheduler,
        history=history,
    )
    assert "Error" in result
    # Store should NOT have a 6th entry
    assert len(store.list("main")) == 5
    # Scheduler should NOT be called
    scheduler.add_job.assert_not_called()


@pytest.mark.asyncio
async def test_create_schedule_invalid_cron_variants(
    store: ScheduleStore, scheduler: MagicMock
) -> None:
    for bad_cron in ["* * *", "99 9 * * *", "0 25 * * *"]:
        result = await _create_schedule(
            agent="main",
            cron=bad_cron,
            prompt="test",
            one_shot=False,
            store=store,
            scheduler=scheduler,
        )
        assert store.list("main") == [], f"Expected no entry for bad cron {bad_cron!r}"


# --- _delete_schedule ---


@pytest.mark.asyncio
async def test_delete_schedule_calls_store_and_scheduler(
    store: ScheduleStore, scheduler: MagicMock
) -> None:
    entry = store.create("main", cron="0 9 * * *", prompt="morning", one_shot=False)
    result = await _delete_schedule(
        agent="main",
        entry_id=entry["id"],
        store=store,
        scheduler=scheduler,
    )
    # Entry removed from store
    assert store.list("main") == []
    # Scheduler job removed
    scheduler.remove_job.assert_called_once_with("main", entry["id"])
    # Success indicated
    assert "deleted" in result.lower() or entry["id"] in result


@pytest.mark.asyncio
async def test_delete_schedule_not_found(store: ScheduleStore, scheduler: MagicMock) -> None:
    result = await _delete_schedule(
        agent="main",
        entry_id="aaaaaa",
        store=store,
        scheduler=scheduler,
    )
    # Scheduler should NOT be called for nonexistent entry
    scheduler.remove_job.assert_not_called()
    # Error indicated
    assert "not found" in result.lower() or "error" in result.lower()


@pytest.mark.asyncio
async def test_delete_schedule_wrong_agent(store: ScheduleStore, scheduler: MagicMock) -> None:
    entry = store.create("main", cron="0 9 * * *", prompt="test", one_shot=False)
    result = await _delete_schedule(
        agent="journalist",
        entry_id=entry["id"],
        store=store,
        scheduler=scheduler,
    )
    # Entry still in main's list
    assert len(store.list("main")) == 1
    scheduler.remove_job.assert_not_called()
    assert "not found" in result.lower() or "error" in result.lower()


# --- CronTrigger validation ---


def test_crontrigger_validates_valid_expressions() -> None:
    """Sanity check: CronTrigger.from_crontab accepts valid 5-field cron."""
    valid_expressions = [
        "0 9 * * *",
        "*/15 * * * *",
        "30 14 * * 5",
        "0 0 1 1 *",
        "0 8-18 * * 1-5",
    ]
    for expr in valid_expressions:
        trigger = CronTrigger.from_crontab(expr)
        assert trigger is not None


def test_crontrigger_rejects_invalid_expressions() -> None:
    """Sanity check: CronTrigger.from_crontab raises on bad input."""
    invalid_expressions = [
        "not a cron",
        "* * *",
        "99 9 * * *",
        "0 25 * * *",
    ]
    for expr in invalid_expressions:
        with pytest.raises(Exception):
            CronTrigger.from_crontab(expr)
