from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ScheduleEntry:
    cron: str
    prompt: str


@dataclass(frozen=True)
class AgentConfig:
    channel_id: str
    schedules: list[ScheduleEntry] = field(default_factory=list)


@dataclass(frozen=True)
class AssistantConfig:
    main_channel_id: str
    agents: dict[str, AgentConfig] = field(default_factory=dict)


def load_config(path: Path) -> AssistantConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    discord = raw.get("discord", {})
    main_channel_id = discord.get("main_channel_id")
    if not main_channel_id:
        raise ValueError("discord.main_channel_id is required")

    agents: dict[str, AgentConfig] = {}
    for name, agent_raw in (raw.get("agents") or {}).items():
        channel_id = (agent_raw or {}).get("channel_id")
        if not channel_id:
            raise ValueError(f"agents.{name}.channel_id is required")
        schedules = [
            ScheduleEntry(cron=s["cron"], prompt=s["prompt"])
            for s in (agent_raw.get("schedule") or [])
        ]
        agents[name] = AgentConfig(channel_id=channel_id, schedules=schedules)

    return AssistantConfig(main_channel_id=main_channel_id, agents=agents)
