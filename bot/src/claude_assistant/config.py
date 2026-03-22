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
    display_name: str | None = None
    avatar_url: str | None = None
    allowed_tools_additional: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AssistantConfig:
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    allowed_user_ids: set[str] = field(default_factory=set)


def load_config(path: Path) -> AssistantConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    agents: dict[str, AgentConfig] = {}
    for name, agent_raw in (raw.get("agents") or {}).items():
        channel_id = (agent_raw or {}).get("channel_id")
        if not channel_id:
            raise ValueError(f"agents.{name}.channel_id is required")
        schedules = [
            ScheduleEntry(cron=s["cron"], prompt=s["prompt"])
            for s in (agent_raw.get("schedule") or [])
        ]
        agents[name] = AgentConfig(
            channel_id=channel_id,
            schedules=schedules,
            display_name=(agent_raw or {}).get("display_name"),
            avatar_url=(agent_raw or {}).get("avatar_url"),
            allowed_tools_additional=(agent_raw or {}).get("allowed_tools_additional") or [],
        )

    if "main" not in agents:
        raise ValueError("agents.main is required")

    allowed_user_ids = set(raw.get("allowed_user_ids") or [])

    return AssistantConfig(agents=agents, allowed_user_ids=allowed_user_ids)
