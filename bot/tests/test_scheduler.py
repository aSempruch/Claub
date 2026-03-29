import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from claude_assistant.firing_history import FiringHistory
from claude_assistant.schedule_store import ScheduleStore
from claude_assistant.scheduler import Scheduler


@pytest.fixture
def history(tmp_path: Path) -> FiringHistory:
    return FiringHistory(tmp_path / "firing_history.json")


@pytest.fixture
def store(tmp_path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.json")


@pytest.fixture
def callback() -> AsyncMock:
    return AsyncMock()


def test_load_from_store(store: ScheduleStore, callback: AsyncMock) -> None:
    store.create("main", cron="0 9 * * *", prompt="morning", one_shot=False)
    store.create("journalist", cron="0 17 * * *", prompt="evening", one_shot=False)
    scheduler = Scheduler(store, callback)
    assert len(scheduler.get_jobs()) == 2


def test_load_empty_store(store: ScheduleStore, callback: AsyncMock) -> None:
    scheduler = Scheduler(store, callback)
    assert len(scheduler.get_jobs()) == 0


def test_add_job(store: ScheduleStore, callback: AsyncMock) -> None:
    scheduler = Scheduler(store, callback)
    scheduler.add_job("main", "abc123", "0 9 * * *", "test prompt", one_shot=False)
    assert len(scheduler.get_jobs()) == 1


def test_remove_job(store: ScheduleStore, callback: AsyncMock) -> None:
    store.create("main", cron="0 9 * * *", prompt="test", one_shot=False)
    scheduler = Scheduler(store, callback)
    entry_id = store.list("main")[0]["id"]
    scheduler.remove_job("main", entry_id)
    assert len(scheduler.get_jobs()) == 0


def test_remove_nonexistent_job(store: ScheduleStore, callback: AsyncMock) -> None:
    scheduler = Scheduler(store, callback)
    scheduler.remove_job("main", "nonexistent")


def test_filter_orphaned_agents(store: ScheduleStore, callback: AsyncMock) -> None:
    store.create("deleted_agent", cron="0 9 * * *", prompt="orphan", one_shot=False)
    scheduler = Scheduler(store, callback, valid_agents={"main"})
    assert len(scheduler.get_jobs()) == 0


@pytest.mark.asyncio
async def test_one_shot_fires_and_deletes(store: ScheduleStore, callback: AsyncMock) -> None:
    entry = store.create("main", cron="0 9 * * *", prompt="one-time", one_shot=True)
    scheduler = Scheduler(store, callback)
    with patch("claude_assistant.scheduler.lognormal_jitter", return_value=0):
        await scheduler._run_one_shot("main", entry["id"], "0 9 * * *", "one-time")
    callback.assert_called_once()
    call_args = callback.call_args[0]
    assert call_args[0] == "main"
    assert "[scheduled" in call_args[1]
    assert store.list("main") == []


@pytest.mark.asyncio
async def test_run_records_firing(store, history, callback):
    sched = Scheduler(store, callback, history=history)
    with patch("claude_assistant.scheduler.recurring_jitter", return_value=0), \
         patch("claude_assistant.scheduler.lognormal_jitter", return_value=0):
        await sched._run("main", "abc123", "0 9 * * *", "do stuff")
    entries = history.all()
    assert len(entries) == 1
    assert entries[0]["agent"] == "main"
    assert entries[0]["cron"] == "0 9 * * *"
    assert entries[0]["one_shot"] is False


@pytest.mark.asyncio
async def test_run_one_shot_records_firing(store, history, callback):
    entry = store.create("main", cron="0 9 * * *", prompt="once", one_shot=True)
    sched = Scheduler(store, callback, history=history)
    with patch("claude_assistant.scheduler.recurring_jitter", return_value=0), \
         patch("claude_assistant.scheduler.lognormal_jitter", return_value=0):
        await sched._run_one_shot("main", entry["id"], "0 9 * * *", "once")
    entries = history.all()
    assert len(entries) == 1
    assert entries[0]["agent"] == "main"
    assert entries[0]["one_shot"] is True
    assert entries[0]["schedule_id"] == entry["id"]
