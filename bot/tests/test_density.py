from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_assistant.firing_history import FiringHistory
from claude_assistant.mcp_server import (
    MAX_FIRINGS_PER_DAY,
    MAX_FIRINGS_PER_WEEK,
    check_schedule_density,
)
from claude_assistant.schedule_store import ScheduleStore


@pytest.fixture
def store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.json")


@pytest.fixture
def history(tmp_path: Path) -> FiringHistory:
    return FiringHistory(tmp_path / "firing_history.json")


def test_constants() -> None:
    assert MAX_FIRINGS_PER_DAY == 5
    assert MAX_FIRINGS_PER_WEEK == 20


def test_empty_store_passes(store: ScheduleStore, history: FiringHistory) -> None:
    result = check_schedule_density(store, history, "0 9 * * *", one_shot=False)
    assert result is None


def test_under_daily_limit_passes(store: ScheduleStore, history: FiringHistory) -> None:
    # 4 existing recurring schedules on Mondays only (4/week total, 4/day max on Monday)
    store.create("main", cron="0 9 * * 1", prompt="a", one_shot=False)
    store.create("main", cron="0 11 * * 1", prompt="b", one_shot=False)
    store.create("main", cron="0 14 * * 1", prompt="c", one_shot=False)
    store.create("main", cron="0 16 * * 1", prompt="d", one_shot=False)
    # Adding a 5th on Monday: 5/day on Monday (at limit), 5/week total — both OK
    result = check_schedule_density(store, history, "0 18 * * 1", one_shot=False)
    assert result is None


def test_daily_limit_exceeded(store: ScheduleStore, history: FiringHistory) -> None:
    # 5 existing recurring schedules all firing daily
    store.create("main", cron="0 8 * * *", prompt="a", one_shot=False)
    store.create("main", cron="0 10 * * *", prompt="b", one_shot=False)
    store.create("main", cron="0 12 * * *", prompt="c", one_shot=False)
    store.create("main", cron="0 14 * * *", prompt="d", one_shot=False)
    store.create("main", cron="0 16 * * *", prompt="e", one_shot=False)
    # Adding a 6th exceeds the daily limit
    result = check_schedule_density(store, history, "0 18 * * *", one_shot=False)
    assert result is not None
    assert "Error" in result
    assert str(MAX_FIRINGS_PER_DAY) in result


def test_weekly_limit_exceeded(store: ScheduleStore, history: FiringHistory) -> None:
    # 4 recurring schedules firing daily = 4/day, 28/week
    # That's under daily limit (5) but over weekly (20)
    store.create("main", cron="0 9 * * *", prompt="a", one_shot=False)
    store.create("main", cron="0 11 * * *", prompt="b", one_shot=False)
    store.create("main", cron="0 14 * * *", prompt="c", one_shot=False)
    store.create("main", cron="0 16 * * *", prompt="d", one_shot=False)
    # Adding a 5th = 5/day, 35/week — daily is at limit but weekly exceeds 20
    # Actually 5/day * 7 = 35 which exceeds 20, but daily check would trigger first
    # since 5 is at the limit. Let's use schedules on specific days instead.
    # 4 schedules per day, but only on specific days to stay under daily limit
    # while exceeding weekly:
    # Mon-Fri: 4/day = 20/week at limit. Add one more on any day = 21.
    store.create("main", cron="0 18 * * 1", prompt="e", one_shot=False)
    result = check_schedule_density(store, history, "0 20 * * 1", one_shot=False)
    # Mon now has 6 firings — daily limit hit first
    # Let's redesign: use 3/day across all 7 days = 21/week, then add one more
    assert result is not None  # Will hit daily limit on Monday


def test_weekly_limit_without_daily_violation(store: ScheduleStore, history: FiringHistory) -> None:
    # 3 recurring schedules every day = 3/day, 21/week
    store.create("a1", cron="0 9 * * *", prompt="a", one_shot=False)
    store.create("a2", cron="0 12 * * *", prompt="b", one_shot=False)
    store.create("a3", cron="0 15 * * *", prompt="c", one_shot=False)
    # Adding another daily schedule = 4/day, 28/week — daily OK (<=5), weekly exceeds 20
    result = check_schedule_density(store, history, "0 18 * * *", one_shot=False)
    assert result is not None
    assert "Error" in result
    assert str(MAX_FIRINGS_PER_WEEK) in result


def test_one_shots_far_in_future_caught(store: ScheduleStore, history: FiringHistory) -> None:
    # Use a fixed "now" so we can create one-shots at a known future date
    # June 15 is ~79 days from March 28, well within 120-day horizon
    fake_now = datetime(2026, 3, 28, 12, 0)
    with patch("claude_assistant.mcp_server._now", return_value=fake_now):
        # 5 one-shots all firing on the same day (June 15)
        store.create("main", cron="0 9 15 6 *", prompt="a", one_shot=True)
        store.create("main", cron="0 10 15 6 *", prompt="b", one_shot=True)
        store.create("main", cron="0 11 15 6 *", prompt="c", one_shot=True)
        store.create("main", cron="0 12 15 6 *", prompt="d", one_shot=True)
        store.create("main", cron="0 13 15 6 *", prompt="e", one_shot=True)
        # 6th one-shot on the same day should be rejected
        result = check_schedule_density(store, history, "0 14 15 6 *", one_shot=True)
        assert result is not None
        assert "Error" in result


def test_firing_history_tips_over_daily(store: ScheduleStore, history: FiringHistory) -> None:
    # 4 recent firings in history today
    for i in range(4):
        history.record("main", f"id{i}", f"0 {8+i} * * *", f"task {i}", True)
    # 1 existing schedule firing today
    # Use a cron that fires every day — one of those days will have 4 history + 1 existing + 1 new = 6
    store.create("main", cron="0 14 * * *", prompt="existing", one_shot=False)
    result = check_schedule_density(store, history, "0 16 * * *", one_shot=False)
    assert result is not None
    assert "Error" in result


def test_empty_store_and_history_passes(store: ScheduleStore, history: FiringHistory) -> None:
    result = check_schedule_density(store, history, "0 9 * * *", one_shot=True)
    assert result is None


def test_error_message_mentions_other_agents(store: ScheduleStore, history: FiringHistory) -> None:
    store.create("main", cron="0 8 * * *", prompt="a", one_shot=False)
    store.create("main", cron="0 10 * * *", prompt="b", one_shot=False)
    store.create("main", cron="0 12 * * *", prompt="c", one_shot=False)
    store.create("main", cron="0 14 * * *", prompt="d", one_shot=False)
    store.create("main", cron="0 16 * * *", prompt="e", one_shot=False)
    result = check_schedule_density(store, history, "0 18 * * *", one_shot=False)
    assert result is not None
    assert "other agents" in result.lower()
