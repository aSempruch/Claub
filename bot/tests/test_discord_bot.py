import asyncio
import pytest
from pathlib import Path
from typing import Any
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


def test_setup_hook_is_assigned_not_on_ready(bot: AssistantBot) -> None:
    """Init must live in setup_hook (fires once) not on_ready (fires per reconnect).

    Regression: previously init was in on_ready and every Discord reconnect
    spawned a duplicate supervisor/reaper/scheduler and tried to re-bind the
    MCP server port, crashing the bot.
    """
    assert bot._client.setup_hook.__name__ == "setup_hook"
    # The Client base method is `pass`-only; ours has real init code.
    import inspect
    src = inspect.getsource(bot._client.setup_hook)
    assert "Scheduler(" in src
    assert "_supervisor_task" in src
    assert "_start_mcp_server" in src


@pytest.mark.asyncio
async def test_on_ready_does_not_duplicate_init(bot: AssistantBot) -> None:
    """Calling on_ready repeatedly (simulating reconnects) must not duplicate state."""
    # Run setup_hook once (simulates discord.py login)
    with patch.object(bot, "_start_mcp_server", new=AsyncMock()):
        await bot._client.setup_hook()

    sup1 = bot._supervisor_task
    reaper1 = bot._idle_reaper_task
    sched1 = bot._scheduler
    mcp1 = bot._mcp_server_task

    # Fire on_ready three times (simulating reconnect storm).
    # @client.event registers the handler by setattr(client, 'on_ready', coro).
    on_ready = bot._client.on_ready
    for _ in range(3):
        await on_ready()

    # No new tasks/scheduler should have been created
    assert bot._supervisor_task is sup1
    assert bot._idle_reaper_task is reaper1
    assert bot._scheduler is sched1
    assert bot._mcp_server_task is mcp1

    # Cleanup
    sup1.cancel()
    reaper1.cancel()
    mcp1.cancel()
    bot._scheduler.stop()
    for t in (sup1, reaper1, mcp1):
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass


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
    bot.sessions.clear_session.assert_called_with("main")
    bot.sessions.delete.assert_not_called()
    mock_process.stop.assert_called_once()
    assert "main" not in bot._processes


@pytest.mark.asyncio
async def test_handle_reset_agent(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 200
    msg.channel.send = AsyncMock()
    msg.content = "/clear"
    await bot._handle_reset(msg)
    bot.sessions.clear_session.assert_called_with("journalist")


@pytest.mark.asyncio
async def test_handle_reset_unknown_channel(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 999
    msg.channel.send = AsyncMock()
    await bot._handle_reset(msg)
    bot.sessions.clear_session.assert_not_called()


@pytest.mark.asyncio
async def test_handle_agent_message_appends_attachment_footer(
    bot: AssistantBot, tmp_path: Path
) -> None:
    """When a Discord message has attachments, the bot downloads them and
    appends a footer to the content passed to the agent process."""
    captured: dict[str, str] = {}

    async def fake_send_with_restart(agent_name: str, content: str) -> str:
        captured["agent"] = agent_name
        captured["content"] = content
        return "ok"

    # FakeAttachment mirrors discord.Attachment surface; writes payload on save.
    class FakeAttachment:
        def __init__(self) -> None:
            self.filename = "hi.txt"
            self.size = 5
            self.content_type = "text/plain"

        async def save(self, fp: Any) -> int:
            Path(fp).write_bytes(b"hello")
            return 5

    msg = MagicMock()
    msg.channel.id = 100  # routes to "main"
    msg.channel.send = AsyncMock()
    # Need a real async-context-manager for `async with channel.typing()`
    typing_cm = AsyncMock()
    typing_cm.__aenter__ = AsyncMock(return_value=None)
    typing_cm.__aexit__ = AsyncMock(return_value=None)
    msg.channel.typing = MagicMock(return_value=typing_cm)
    msg.id = 555
    msg.attachments = [FakeAttachment()]

    # Patch session lookup to a no-op
    bot.sessions.get = MagicMock(return_value=None)
    # Patch the attachments base dir to tmp_path so we don't write to /tmp
    import claude_assistant.attachments as _att
    real_download = _att.download_attachments
    async def patched_download(m, a):
        return await real_download(m, a, base_dir=tmp_path)
    with patch("claude_assistant.discord_bot.download_attachments", side_effect=patched_download):
        with patch.object(bot, "_send_with_restart", side_effect=fake_send_with_restart):
            with patch.object(bot, "_send_chunked", new=AsyncMock()):
                await bot._handle_agent_message(msg, "main", "hello agent")

    assert captured["agent"] == "main"
    assert captured["content"].startswith("hello agent")
    assert "[Attachments" in captured["content"]
    assert "hi.txt" in captured["content"]
    # File was actually written to the patched base_dir
    assert (tmp_path / "main" / "555" / "hi.txt").read_bytes() == b"hello"


# --- _deliver_result: resilient Discord delivery (timeout + retry + no silent loss) ---
# Regression: during the 2026-07-16 Discord API incident, a webhook send hung
# forever inside discord.py with no timeout; the agent's reply was silently
# discarded and the handler task stayed parked. Delivery must be bounded,
# retried once, and loudly logged on total failure — never raised, never silent.


@pytest.mark.asyncio
async def test_deliver_result_sends_once_on_success(bot: AssistantBot) -> None:
    channel = MagicMock()
    with patch.object(bot, "_send_chunked", new=AsyncMock()) as send:
        await bot._deliver_result(channel, "main", "hello")
    send.assert_awaited_once_with(channel, "main", "hello")


@pytest.mark.asyncio
async def test_deliver_result_retries_once_after_failure(bot: AssistantBot) -> None:
    channel = MagicMock()
    send = AsyncMock(side_effect=[RuntimeError("discord 500"), None])
    with patch.object(bot, "_send_chunked", new=send):
        await bot._deliver_result(channel, "main", "hello")
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_deliver_result_times_out_hung_send_and_retries(bot: AssistantBot) -> None:
    channel = MagicMock()
    calls = []

    async def hang_then_succeed(*args: Any) -> None:
        calls.append(args)
        if len(calls) == 1:
            await asyncio.sleep(60)  # simulates webhook call wedged in discord.py

    with patch.object(bot, "_send_chunked", new=hang_then_succeed):
        with patch.object(bot, "DELIVER_TIMEOUT_S", 0.05):
            await bot._deliver_result(channel, "main", "hello")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_deliver_result_logs_error_when_all_attempts_fail(
    bot: AssistantBot, caplog: pytest.LogCaptureFixture
) -> None:
    channel = MagicMock()
    send = AsyncMock(side_effect=RuntimeError("discord down"))
    with patch.object(bot, "_send_chunked", new=send):
        with caplog.at_level("ERROR"):
            # Must not raise — a lost reply is logged, not propagated
            await bot._deliver_result(channel, "main", "x" * 1790)
    assert send.await_count == 2
    assert any(
        "main" in r.message and "1790" in r.message
        for r in caplog.records if r.levelname == "ERROR"
    )


def test_build_agent_process_model_precedence(tmp_path: Path) -> None:
    from claude_assistant.discord_bot import build_agent_process

    cfg = AssistantConfig(
        agents={"main": AgentConfig(channel_id="1", model="sonnet")},
        model="haiku",
    )
    common: dict[str, Any] = dict(
        name="main", workspace=tmp_path, config=cfg,
        mcp_configs=[], agent_definition=None, disallowed_skills=[],
    )
    assert build_agent_process(**common, model_override="opus").model == "opus"
    assert build_agent_process(**common).model == "sonnet"

    cfg_no_agent_model = AssistantConfig(
        agents={"main": AgentConfig(channel_id="1")}, model="haiku",
    )
    common["config"] = cfg_no_agent_model
    assert build_agent_process(**common).model == "haiku"
    assert build_agent_process(**common, model_override="opus").model == "opus"


def _model_msg(channel_id: int, content: str) -> MagicMock:
    msg = MagicMock()
    msg.channel.id = channel_id
    msg.channel.send = AsyncMock()
    msg.content = content
    return msg


def _live_process(model: str | None = None, busy: bool = False) -> MagicMock:
    p = MagicMock()
    p.stop = AsyncMock()
    p.wait_until_idle = AsyncMock()
    p.is_alive = True
    p.busy = busy
    p.model = model
    return p


@pytest.mark.asyncio
async def test_handle_model_set(bot: AssistantBot) -> None:
    bot.sessions.get_model.return_value = None
    mock_process = _live_process()
    bot._processes["main"] = mock_process

    msg = _model_msg(100, "/model opus")
    await bot._handle_model(msg)

    bot.sessions.set_model.assert_called_with("main", "opus")
    reply = msg.channel.send.call_args.args[0]
    assert "`opus`" in reply
    assert "next message" in reply
    # The restart is lazy (_get_or_start_process), so the command tears nothing down.
    mock_process.stop.assert_not_called()
    assert bot._processes["main"] is mock_process


@pytest.mark.asyncio
async def test_handle_model_while_busy_leaves_the_turn_alone(bot: AssistantBot) -> None:
    """Regression: /model used to stop() mid-turn *and* mark the agent reaped, so
    _send_with_restart re-raised instead of retrying — the in-flight reply was lost."""
    bot.sessions.get_model.return_value = None
    mock_process = _live_process(busy=True)
    bot._processes["main"] = mock_process

    msg = _model_msg(100, "/model opus")
    await bot._handle_model(msg)

    bot.sessions.set_model.assert_called_with("main", "opus")
    mock_process.stop.assert_not_called()
    assert bot._processes["main"] is mock_process
    assert "main" not in bot._reaped
    reply = msg.channel.send.call_args.args[0]
    assert "after the current turn" in reply


@pytest.mark.asyncio
async def test_get_or_start_process_reuses_process_when_model_matches(
    bot: AssistantBot,
) -> None:
    bot.sessions.get_model.return_value = "opus"
    process = _live_process(model="opus")
    bot._processes["main"] = process

    with patch.object(bot, "_restart_process", new=AsyncMock()) as restart:
        assert await bot._get_or_start_process("main") is process
    restart.assert_not_awaited()
    process.wait_until_idle.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_start_process_drains_turn_before_applying_model_change(
    bot: AssistantBot,
) -> None:
    bot.sessions.get_model.return_value = "opus"
    old = _live_process(model="sonnet", busy=True)  # predates the /model change
    bot._processes["main"] = old
    replacement = _live_process(model="opus")

    order: list[str] = []
    old.wait_until_idle = AsyncMock(side_effect=lambda: order.append("drained"))

    async def _restart(name: str) -> MagicMock:
        order.append("restarted")
        return replacement

    with patch.object(bot, "_restart_process", new=AsyncMock(side_effect=_restart)):
        assert await bot._get_or_start_process("main") is replacement

    # Order is the whole point: restarting before the turn drains is what kills it.
    assert order == ["drained", "restarted"]


@pytest.mark.asyncio
async def test_handle_model_reset(bot: AssistantBot) -> None:
    bot.sessions.get_model.return_value = "opus"
    msg = _model_msg(100, "/model reset")
    await bot._handle_model(msg)
    bot.sessions.clear_model.assert_called_with("main")
    bot.sessions.set_model.assert_not_called()


@pytest.mark.asyncio
async def test_handle_model_show_override(bot: AssistantBot) -> None:
    bot.sessions.get_model.return_value = "opus"
    msg = _model_msg(100, "/model")
    await bot._handle_model(msg)
    bot.sessions.set_model.assert_not_called()
    bot.sessions.clear_model.assert_not_called()
    reply = msg.channel.send.call_args.args[0]
    assert "`opus`" in reply and "override" in reply


@pytest.mark.asyncio
async def test_handle_model_show_default(bot: AssistantBot) -> None:
    bot.sessions.get_model.return_value = None
    msg = _model_msg(100, "/model")
    await bot._handle_model(msg)
    reply = msg.channel.send.call_args.args[0]
    assert "CLI default" in reply


@pytest.mark.asyncio
async def test_handle_model_unknown_channel(bot: AssistantBot) -> None:
    msg = _model_msg(999, "/model opus")
    await bot._handle_model(msg)
    bot.sessions.set_model.assert_not_called()
    msg.channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_dispatches_model(bot: AssistantBot) -> None:
    bot.sessions.get_model.return_value = None
    msg = _model_msg(100, "/model opus")
    await bot._handle_message(msg)
    bot.sessions.set_model.assert_called_with("main", "opus")


@pytest.mark.asyncio
async def test_handle_compact_sends_raw_command_and_two_messages(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 100
    msg.channel.send = AsyncMock()
    msg.content = "/compact"
    with patch.object(
        bot, "_send_with_restart", new=AsyncMock(return_value="")
    ) as mock_send:
        await bot._handle_compact(msg)
    # Sends the literal slash command with raw=True so the time prefix
    # doesn't corrupt it.
    mock_send.assert_awaited_once_with("main", "/compact", raw=True)
    # Two channel messages: a start notice and a completion notice.
    sent = [c.args[0] for c in msg.channel.send.await_args_list]
    assert any("Compacting" in s for s in sent)
    assert "Compaction complete." in sent


@pytest.mark.asyncio
async def test_handle_compact_surfaces_cli_noop_message(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 100
    msg.channel.send = AsyncMock()
    msg.content = "/compact"
    with patch.object(
        bot, "_send_with_restart",
        new=AsyncMock(return_value="Not enough messages to compact."),
    ):
        await bot._handle_compact(msg)
    sent = [c.args[0] for c in msg.channel.send.await_args_list]
    assert "Not enough messages to compact." in sent


@pytest.mark.asyncio
async def test_handle_compact_reports_queued_when_busy(bot: AssistantBot) -> None:
    """Mid-turn /compact queues on the FIFO stream lock — say so rather than
    posting "Compacting…" and going silent for the length of the turn."""
    bot._processes["main"] = _live_process(busy=True)
    msg = MagicMock()
    msg.channel.id = 100
    msg.channel.send = AsyncMock()
    msg.content = "/compact"
    with patch.object(
        bot, "_send_with_restart", new=AsyncMock(return_value="")
    ) as mock_send:
        await bot._handle_compact(msg)

    sent = [c.args[0] for c in msg.channel.send.await_args_list]
    assert any("mid-turn" in s for s in sent)
    assert not any("Compacting `main`" in s for s in sent)
    mock_send.assert_awaited_once_with("main", "/compact", raw=True)


@pytest.mark.asyncio
async def test_handle_compact_unknown_channel(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 999
    msg.channel.send = AsyncMock()
    msg.content = "/compact"
    with patch.object(bot, "_send_with_restart", new=AsyncMock()) as mock_send:
        await bot._handle_compact(msg)
    mock_send.assert_not_awaited()
    msg.channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_dispatches_compact(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 100
    msg.channel.send = AsyncMock()
    msg.content = "/compact"
    with patch.object(bot, "_handle_compact", new=AsyncMock()) as mock_compact:
        await bot._handle_message(msg)
    mock_compact.assert_awaited_once_with(msg)


@pytest.mark.asyncio
async def test_start_failure_drops_model_override(bot: AssistantBot, tmp_path: Path) -> None:
    bot.sessions.get.return_value = None
    bot.sessions.get_model.return_value = "not-a-model"
    with patch(
        "claude_assistant.discord_bot.build_agent_process"
    ) as mock_build:
        mock_build.return_value.start = AsyncMock(side_effect=RuntimeError("bad model"))
        await bot._start_agent("main")
    bot.sessions.clear_model.assert_called_with("main")


# --- Webhook username model tag: "Journalist [opus]" ---


def _webhook_bot(tmp_path: Path, *, agent_model: str | None = None,
                 global_model: str | None = None) -> AssistantBot:
    from claude_assistant.schedule_store import ScheduleStore
    cfg = AssistantConfig(
        agents={"main": AgentConfig(channel_id="100", display_name="Journalist",
                                    model=agent_model)},
        model=global_model,
    )
    return AssistantBot(
        config=cfg,
        workspaces_dir=tmp_path / "workspaces",
        session_store=MagicMock(),
        schedule_store=ScheduleStore(tmp_path / "schedules.json"),
    )


async def _webhook_username(bot: AssistantBot) -> str:
    webhook = MagicMock()
    webhook.send = AsyncMock()
    with patch.object(bot, "_get_or_create_webhook", new=AsyncMock(return_value=webhook)):
        await bot._webhook_send(MagicMock(), "main", "hi")
    return webhook.send.call_args.kwargs["username"]


@pytest.mark.asyncio
async def test_webhook_username_tags_session_override(tmp_path: Path) -> None:
    bot = _webhook_bot(tmp_path, agent_model="sonnet")
    bot.sessions.get_model.return_value = "opus"
    assert await _webhook_username(bot) == "Journalist [opus]"


@pytest.mark.asyncio
async def test_webhook_username_tags_config_model_verbatim(tmp_path: Path) -> None:
    bot = _webhook_bot(tmp_path, agent_model="claude-opus-4-8")
    bot.sessions.get_model.return_value = None
    assert await _webhook_username(bot) == "Journalist [claude-opus-4-8]"


@pytest.mark.asyncio
async def test_webhook_username_tags_global_model(tmp_path: Path) -> None:
    bot = _webhook_bot(tmp_path, global_model="haiku")
    bot.sessions.get_model.return_value = None
    assert await _webhook_username(bot) == "Journalist [haiku]"


@pytest.mark.asyncio
async def test_webhook_username_plain_when_no_model_anywhere(tmp_path: Path) -> None:
    bot = _webhook_bot(tmp_path)
    bot.sessions.get_model.return_value = None
    assert await _webhook_username(bot) == "Journalist"


# Regression: with discord.py's default wait=False the API 204s on accepting the
# webhook execution rather than on creating the message, so consecutive chunks of
# one reply race and can be persisted out of order. On 2026-07-29 a 2-chunk reply
# reached Discord tail-first, reading as an unrelated message.
@pytest.mark.asyncio
async def test_webhook_send_waits_for_message_creation(tmp_path: Path) -> None:
    bot = _webhook_bot(tmp_path)
    bot.sessions.get_model.return_value = None
    webhook = MagicMock()
    webhook.send = AsyncMock()
    with patch.object(bot, "_get_or_create_webhook", new=AsyncMock(return_value=webhook)):
        await bot._webhook_send(MagicMock(), "main", "hi")
    assert webhook.send.call_args.kwargs["wait"] is True


@pytest.mark.asyncio
async def test_webhook_username_truncated_to_discord_limit(tmp_path: Path) -> None:
    bot = _webhook_bot(tmp_path, agent_model="m" * 100)
    bot.sessions.get_model.return_value = None
    username = await _webhook_username(bot)
    assert len(username) == 80
    assert username.startswith("Journalist [m")


@pytest.mark.asyncio
async def test_deliver_agent_message_sends_and_persists(tmp_path):
    from unittest.mock import AsyncMock
    from claude_assistant.config import AgentConfig, AssistantConfig
    from claude_assistant.discord_bot import AssistantBot
    from claude_assistant.schedule_store import ScheduleStore
    from claude_assistant.session import SessionStore
    from types import SimpleNamespace

    config = AssistantConfig(
        agents={
            "main": AgentConfig(channel_id="1"),
            "journalist": AgentConfig(channel_id="2"),
        },
        agent_groups={"g": ["main", "journalist"]},
    )
    bot = AssistantBot(
        config,
        workspaces_dir=tmp_path / "ws",
        session_store=SessionStore(tmp_path / "sessions.json"),
        schedule_store=ScheduleStore(tmp_path / "schedules.json"),
    )
    bot._send_with_restart = AsyncMock(return_value="the reply")
    bot._processes["journalist"] = SimpleNamespace(session_id="sess-123")

    result = await bot._deliver_agent_message("journalist", "[message from agent main] hi")

    assert result == "the reply"
    bot._send_with_restart.assert_awaited_once_with(
        "journalist", "[message from agent main] hi"
    )
    assert bot.sessions.get("journalist") == "sess-123"
