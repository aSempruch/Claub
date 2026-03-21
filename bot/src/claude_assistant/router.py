from __future__ import annotations

from claude_assistant.config import AssistantConfig


class Router:
    def __init__(self, config: AssistantConfig) -> None:
        self._main_channel = config.main_channel_id
        self._agent_channels: dict[str, str] = {}  # channel_id -> agent_name
        for name, agent in config.agents.items():
            self._agent_channels[agent.channel_id] = name

    def route(self, channel_id: str) -> tuple[str | None, str | None]:
        """Returns (route_type, agent_name).

        route_type is "main", "agent", or None (ignore).
        """
        if channel_id == self._main_channel:
            return ("main", None)
        agent = self._agent_channels.get(channel_id)
        if agent:
            return ("agent", agent)
        return (None, None)
