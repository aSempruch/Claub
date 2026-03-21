from claude_assistant.config import AssistantConfig, AgentConfig
from claude_assistant.router import Router


def _config() -> AssistantConfig:
    return AssistantConfig(
        main_channel_id="100",
        agents={
            "journalist": AgentConfig(channel_id="200"),
            "researcher": AgentConfig(channel_id="300"),
        },
    )


def test_route_main_channel() -> None:
    router = Router(_config())
    assert router.route("100") == ("main", None)


def test_route_agent_channel() -> None:
    router = Router(_config())
    assert router.route("200") == ("agent", "journalist")
    assert router.route("300") == ("agent", "researcher")


def test_route_unknown_channel() -> None:
    router = Router(_config())
    assert router.route("999") == (None, None)
