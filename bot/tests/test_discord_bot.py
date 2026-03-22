import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from claude_assistant.discord_bot import AssistantBot
from claude_assistant.config import AssistantConfig, AgentConfig, ScheduleEntry


@pytest.fixture
def config() -> AssistantConfig:
    return AssistantConfig(
        agents={
            "main": AgentConfig(channel_id="100"),
            "journalist": AgentConfig(
                channel_id="200",
                schedules=[ScheduleEntry(cron="0 9 * * *", prompt="news")],
            ),
        },
    )


@pytest.fixture
def bot(config: AssistantConfig, tmp_path: Path) -> AssistantBot:
    return AssistantBot(
        config=config,
        home_dir=tmp_path / "home",
        workspaces_dir=tmp_path / "workspaces",
        session_store=MagicMock(),
    )


def test_bot_creates_router(bot: AssistantBot) -> None:
    assert bot.router.route("100") == "main"
    assert bot.router.route("200") == "journalist"
    assert bot.router.route("999") is None


def test_bot_starts_with_no_processes(bot: AssistantBot) -> None:
    assert bot._processes == {}


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
    await bot._handle_reset(msg, "/clear")
    bot.sessions.delete.assert_called_with("main")
    mock_process.stop.assert_called_once()
    assert "main" not in bot._processes


@pytest.mark.asyncio
async def test_handle_reset_agent(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 200
    msg.channel.send = AsyncMock()
    msg.content = "/clear journalist"
    await bot._handle_reset(msg, "/clear journalist")
    bot.sessions.delete.assert_called_with("journalist")


@pytest.mark.asyncio
async def test_handle_reset_unknown_agent(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 200
    msg.channel.send = AsyncMock()
    await bot._handle_reset(msg, "/clear nonexistent")
    msg.channel.send.assert_called_with("Unknown agent: nonexistent")
