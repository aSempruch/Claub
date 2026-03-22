import pytest
from unittest.mock import AsyncMock, MagicMock
from claude_assistant.config import AssistantConfig, AgentConfig, ScheduleEntry
from claude_assistant.scheduler import Scheduler


def _config_with_schedule() -> AssistantConfig:
    return AssistantConfig(
        agents={
            "main": AgentConfig(
                channel_id="100",
                schedules=[
                    ScheduleEntry(cron="0 8 * * *", prompt="morning review"),
                ],
            ),
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
    assert len(jobs) == 3  # 1 main + 2 journalist


def test_scheduler_registers_main_schedule() -> None:
    callback = AsyncMock()
    scheduler = Scheduler(_config_with_schedule(), callback)
    job_names = [j.name for j in scheduler.get_jobs()]
    assert any("main" in name for name in job_names)


def test_scheduler_no_schedules() -> None:
    config = AssistantConfig(
        agents={
            "main": AgentConfig(channel_id="100"),
            "journalist": AgentConfig(channel_id="200"),
        },
    )
    callback = AsyncMock()
    scheduler = Scheduler(config, callback)
    assert len(scheduler.get_jobs()) == 0
