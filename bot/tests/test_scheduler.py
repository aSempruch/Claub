import pytest
from unittest.mock import AsyncMock, MagicMock
from claude_assistant.config import AssistantConfig, AgentConfig, ScheduleEntry
from claude_assistant.scheduler import Scheduler


def _config_with_schedule() -> AssistantConfig:
    return AssistantConfig(
        main_channel_id="100",
        agents={
            "journalist": AgentConfig(
                channel_id="200",
                schedules=[
                    ScheduleEntry(cron="0 9 * * *", prompt="check news"),
                    ScheduleEntry(cron="0 17 * * *", prompt="evening summary"),
                ],
            ),
        },
    )


def test_scheduler_registers_jobs() -> None:
    callback = AsyncMock()
    scheduler = Scheduler(_config_with_schedule(), callback)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 2


def test_scheduler_no_schedules() -> None:
    config = AssistantConfig(
        main_channel_id="100",
        agents={"journalist": AgentConfig(channel_id="200")},
    )
    callback = AsyncMock()
    scheduler = Scheduler(config, callback)
    assert len(scheduler.get_jobs()) == 0
