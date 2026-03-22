from __future__ import annotations

from claude_assistant.config import AssistantConfig


class Router:
    def __init__(self, config: AssistantConfig) -> None:
        self._channels: dict[str, str] = {}  # channel_id -> agent_name
        for name, agent in config.agents.items():
            self._channels[agent.channel_id] = name

    def route(self, channel_id: str) -> str | None:
        """Returns agent name for the channel, or None if unrecognized."""
        return self._channels.get(channel_id)
