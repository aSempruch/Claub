from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from claude_assistant.config import AssistantConfig

log = logging.getLogger(__name__)

# callback signature: (agent_name, prompt) -> None
ScheduleCallback = Callable[[str, str], Awaitable[None]]


class Scheduler:
    def __init__(
        self, config: AssistantConfig, callback: ScheduleCallback
    ) -> None:
        self._scheduler = AsyncIOScheduler()
        self._callback = callback

        for agent_name, agent_config in config.agents.items():
            for i, entry in enumerate(agent_config.schedules):
                self._scheduler.add_job(
                    self._run,
                    trigger=CronTrigger.from_crontab(entry.cron),
                    args=[agent_name, entry.prompt],
                    id=f"{agent_name}_schedule_{i}",
                    name=f"{agent_name}: {entry.prompt[:50]}",
                )

    async def _run(self, agent_name: str, prompt: str) -> None:
        log.info("Scheduled task firing for %s", agent_name)
        prefixed = f"[scheduled] {prompt}"
        await self._callback(agent_name, prefixed)

    def get_jobs(self) -> list:
        return self._scheduler.get_jobs()

    def start(self) -> None:
        self._scheduler.start()
        log.info("Scheduler started with %d jobs", len(self.get_jobs()))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
