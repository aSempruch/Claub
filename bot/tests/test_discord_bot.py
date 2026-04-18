import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from claude_assistant.discord_bot import AssistantBot
from claude_assistant.config import AssistantConfig, AgentConfig


@pytest.fixture
def config() -> AssistantConfig:
    return AssistantConfig(
        agents={
            "main": AgentConfig(channel_id="100"),
            "journalist": AgentConfig(channel_id="200"),
        },
    )


@pytest.fixture
def bot(config: AssistantConfig, tmp_path: Path) -> AssistantBot:
    from claude_assistant.schedule_store import ScheduleStore
    return AssistantBot(
        config=config,
        workspaces_dir=tmp_path / "workspaces",
        session_store=MagicMock(),
        schedule_store=ScheduleStore(tmp_path / "schedules.json"),
    )


def test_bot_creates_router(bot: AssistantBot) -> None:
    assert bot.router.route("100") == "main"
    assert bot.router.route("200") == "journalist"
    assert bot.router.route("999") is None


def test_bot_starts_with_no_processes(bot: AssistantBot) -> None:
    assert bot._processes == {}


def test_merged_hooks_additive() -> None:
    from claude_assistant.discord_bot import _merged_hooks

    cfg = AssistantConfig(
        agents={
            "main": AgentConfig(channel_id="1", on_start=["echo a"]),
        },
        on_start=["echo g1", "echo g2"],
    )
    assert _merged_hooks(cfg, "main", "on_start") == ["echo g1", "echo g2", "echo a"]
    assert _merged_hooks(cfg, "main", "on_stop") == []


def test_merged_hooks_global_only() -> None:
    from claude_assistant.discord_bot import _merged_hooks

    cfg = AssistantConfig(
        agents={"main": AgentConfig(channel_id="1")},
        on_stop=["echo stop"],
    )
    assert _merged_hooks(cfg, "main", "on_stop") == ["echo stop"]


def test_merged_hooks_unknown_agent_returns_global() -> None:
    from claude_assistant.discord_bot import _merged_hooks

    cfg = AssistantConfig(
        agents={"main": AgentConfig(channel_id="1")},
        on_start=["echo g"],
    )
    assert _merged_hooks(cfg, "nobody", "on_start") == ["echo g"]


@pytest.mark.asyncio
async def test_handle_reset_main(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 100
    msg.channel.send = AsyncMock()
    msg.content = "/clear"
    # Put a mock process in place
    mock_process = MagicMock()
    mock_process.stop = AsyncMock()
    bot._processes["main"] = mock_process
    await bot._handle_reset(msg)
    bot.sessions.delete.assert_called_with("main")
    mock_process.stop.assert_called_once()
    assert "main" not in bot._processes


@pytest.mark.asyncio
async def test_handle_reset_agent(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 200
    msg.channel.send = AsyncMock()
    msg.content = "/clear"
    await bot._handle_reset(msg)
    bot.sessions.delete.assert_called_with("journalist")


@pytest.mark.asyncio
async def test_handle_reset_unknown_channel(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 999
    msg.channel.send = AsyncMock()
    await bot._handle_reset(msg)
    bot.sessions.delete.assert_not_called()
