from claude_assistant.config import AssistantConfig, AgentConfig
from claude_assistant.router import Router


def _config() -> AssistantConfig:
    return AssistantConfig(
        agents={
            "main": AgentConfig(channel_id="100"),
            "journalist": AgentConfig(channel_id="200"),
            "researcher": AgentConfig(channel_id="300"),
        },
    )


def test_route_main_channel() -> None:
    router = Router(_config())
    assert router.route("100") == "main"


def test_route_agent_channel() -> None:
    router = Router(_config())
    assert router.route("200") == "journalist"
    assert router.route("300") == "researcher"


def test_route_unknown_channel() -> None:
    router = Router(_config())
    assert router.route("999") is None
