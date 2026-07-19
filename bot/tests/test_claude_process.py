import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from claude_assistant.claude_process import AgentProcess, _apply_reply_sentinel


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspaces" / "main"
    ws.mkdir(parents=True)
    return ws


class TestAgentProcess:
    @pytest.mark.asyncio
    async def test_build_command_first_run(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace)
        cmd = proc._build_command(session_id=None)
        assert "claude" in cmd
        assert "--input-format" in cmd
        assert "stream-json" in cmd
        assert "--resume" not in cmd

    @pytest.mark.asyncio
    async def test_build_command_resume(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace)
        cmd = proc._build_command(session_id="uuid-123")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "uuid-123"

    @pytest.mark.asyncio
    async def test_build_command_with_agent_name_no_definition(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace, agent_name="journalist")
        cmd = proc._build_command(session_id=None)
        assert "--agent" in cmd
        idx = cmd.index("--agent")
        assert cmd[idx + 1] == "journalist"
        assert "--agents" not in cmd

    @pytest.mark.asyncio
    async def test_build_command_with_agent_definition(
        self, workspace: Path
    ) -> None:
        defn = {"description": "A news bot", "prompt": "You are a journalist."}
        proc = AgentProcess(
            workspace=workspace,
            agent_name="journalist",
            agent_definition=defn,
        )
        cmd = proc._build_command(session_id=None)
        assert "--agents" in cmd
        idx = cmd.index("--agents")
        agents_json = json.loads(cmd[idx + 1])
        assert "journalist" in agents_json
        assert agents_json["journalist"]["description"] == "A news bot"
        assert agents_json["journalist"]["prompt"] == "You are a journalist."
        assert "--agent" in cmd
        assert cmd[cmd.index("--agent") + 1] == "journalist"

    @pytest.mark.asyncio
    async def test_format_input_message(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace)
        msg = proc._format_input("hello", session_id="default")
        parsed = json.loads(msg)
        assert parsed["type"] == "user"
        assert parsed["message"]["role"] == "user"
        assert parsed["message"]["content"] == "hello"
        assert parsed["session_id"] == "default"
        assert parsed["parent_tool_use_id"] is None

    @pytest.mark.asyncio
    async def test_on_start_hooks_run_with_agent_env(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        marker = tmp_path / "marker.txt"
        hook = f'echo "$CLAUB_AGENT_NAME" > {marker}'
        proc = AgentProcess(
            workspace=workspace,
            agent_name="journalist",
            on_start_hooks=[hook],
        )
        await proc._run_hooks(proc.on_start_hooks, phase="on_start")
        assert marker.read_text().strip() == "journalist"

    @pytest.mark.asyncio
    async def test_on_stop_hooks_run(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        marker = tmp_path / "stopped.txt"
        hook = f"echo stopped > {marker}"
        proc = AgentProcess(
            workspace=workspace,
            agent_name="main",
            on_stop_hooks=[hook],
        )
        await proc._run_hooks(proc.on_stop_hooks, phase="on_stop")
        assert marker.read_text().strip() == "stopped"

    @pytest.mark.asyncio
    async def test_hook_failure_is_non_fatal(
        self, workspace: Path, caplog
    ) -> None:
        import logging
        caplog.set_level(logging.WARNING)
        proc = AgentProcess(
            workspace=workspace,
            agent_name="main",
            on_start_hooks=["false"],
        )
        await proc._run_hooks(proc.on_start_hooks, phase="on_start")
        assert any("on_start hook failed" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_hook_timeout(
        self, workspace: Path, caplog
    ) -> None:
        import logging
        caplog.set_level(logging.WARNING)
        proc = AgentProcess(
            workspace=workspace,
            agent_name="main",
            on_start_hooks=["sleep 30"],
            hook_timeout=0.5,
        )
        await proc._run_hooks(proc.on_start_hooks, phase="on_start")
        assert any("timed out" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_hooks_default_empty(self, workspace: Path) -> None:
        proc = AgentProcess(workspace=workspace, agent_name="main")
        assert proc.on_start_hooks == []
        assert proc.on_stop_hooks == []

    @pytest.mark.asyncio
    async def test_parse_init_event(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace)
        event = {"type": "system", "subtype": "init", "session_id": "abc-123"}
        assert proc._parse_session_id(event) == "abc-123"

    @pytest.mark.asyncio
    async def test_parse_result_event(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace)
        event = {"type": "result", "subtype": "success", "result": "hello"}
        assert proc._is_result_event(event)
        assert proc._extract_result(event) == "hello"

    @pytest.mark.asyncio
    async def test_parse_non_result_event(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace)
        event = {"type": "assistant", "message": {}}
        assert not proc._is_result_event(event)

    @pytest.mark.asyncio
    async def test_build_command_disallowed_skills(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(
            workspace=workspace,
            agent_name="main",
            disallowed_skills=["amazon-browse", "deploy"],
        )
        cmd = proc._build_command(session_id=None)
        idx = cmd.index("--disallowedTools")
        disallowed = cmd[idx + 1:]
        # Stop at the next flag if any
        disallowed = [x for x in disallowed if not x.startswith("--")]
        assert "Skill(amazon-browse)" in disallowed
        assert "Skill(deploy)" in disallowed
        # No Agent(...) entries — agents are isolated via --agents flag
        assert not any(x.startswith("Agent(") for x in disallowed)

    @pytest.mark.asyncio
    async def test_build_command_with_effort(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace, effort="high")
        cmd = proc._build_command(session_id=None)
        assert "--effort" in cmd
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "high"

    @pytest.mark.asyncio
    async def test_build_command_without_effort(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace)
        cmd = proc._build_command(session_id=None)
        assert "--effort" not in cmd

    @pytest.mark.asyncio
    async def test_env_includes_agent_name(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace, agent_name="journalist")
        env = proc._env()
        assert env["CLAUB_AGENT_NAME"] == "journalist"

    @pytest.mark.asyncio
    async def test_env_no_agent_name(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace)
        env = proc._env()
        assert "CLAUB_AGENT_NAME" not in env

    @pytest.mark.asyncio
    async def test_env_compact_pct(self, workspace: Path) -> None:
        proc = AgentProcess(workspace=workspace, compact_pct=75)
        env = proc._env()
        assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "75"

    @pytest.mark.asyncio
    async def test_env_no_compact_pct(self, workspace: Path) -> None:
        proc = AgentProcess(workspace=workspace)
        env = proc._env()
        assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in env

    @pytest.mark.asyncio
    async def test_env_does_not_override_home(self, workspace: Path) -> None:
        import os
        proc = AgentProcess(workspace=workspace)
        env = proc._env()
        assert env["HOME"] == os.environ["HOME"]


class TestReadUntilResult:
    """Tests for _read_until_result collecting text across multi-turn responses."""

    def _make_proc(self, workspace: Path, lines: list[str]) -> AgentProcess:
        """Create an AgentProcess with a mock stdout that yields the given JSON lines."""
        proc = AgentProcess(workspace=workspace)
        proc._process = MagicMock()
        data = b"\n".join(l.encode() for l in lines) + b"\n"
        proc._process.stdout = asyncio.StreamReader()
        proc._process.stdout.feed_data(data)
        proc._process.stdout.feed_eof()
        return proc

    @pytest.mark.asyncio
    async def test_simple_result(self, workspace: Path) -> None:
        """Result with text in result field, no assistant events."""
        lines = [
            json.dumps({"type": "result", "result": "Hello!"}),
        ]
        proc = self._make_proc(workspace, lines)
        assert await proc._read_until_result() == "Hello!"

    @pytest.mark.asyncio
    async def test_text_before_tool_call_then_empty_result(self, workspace: Path) -> None:
        """Text in assistant event, tool call, then empty result — text should be preserved."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Here is your morning brief."}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Write", "input": {}}]}}),
            json.dumps({"type": "result", "result": ""}),
        ]
        proc = self._make_proc(workspace, lines)
        result = await proc._read_until_result()
        assert "Here is your morning brief." in result

    @pytest.mark.asyncio
    async def test_text_before_tool_call_with_trailing_text(self, workspace: Path) -> None:
        """Text before tool call, then more text after — both should appear."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Here is a haiku."}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Write", "input": {}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Written to file."}]}}),
            json.dumps({"type": "result", "result": "Written to file."}),
        ]
        proc = self._make_proc(workspace, lines)
        result = await proc._read_until_result()
        assert "Here is a haiku." in result
        assert "Written to file." in result

    @pytest.mark.asyncio
    async def test_result_already_has_all_text(self, workspace: Path) -> None:
        """When result already contains the text, don't duplicate it."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello!"}]}}),
            json.dumps({"type": "result", "result": "Hello!"}),
        ]
        proc = self._make_proc(workspace, lines)
        result = await proc._read_until_result()
        assert result == "Hello!"

    @pytest.mark.asyncio
    async def test_session_id_still_captured(self, workspace: Path) -> None:
        """Init events should still set session_id."""
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-abc"}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}]}}),
            json.dumps({"type": "result", "result": "Hi"}),
        ]
        proc = self._make_proc(workspace, lines)
        await proc._read_until_result()
        assert proc._session_id == "sess-abc"

    @pytest.mark.asyncio
    async def test_multiple_text_blocks_across_turns(self, workspace: Path) -> None:
        """Multiple text blocks across multiple turns with tool calls between."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Part 1"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Write", "input": {}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Part 2"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Edit", "input": {}}]}}),
            json.dumps({"type": "result", "result": ""}),
        ]
        proc = self._make_proc(workspace, lines)
        result = await proc._read_until_result()
        assert "Part 1" in result
        assert "Part 2" in result

    @pytest.mark.asyncio
    async def test_reply_sentinel_strips_scratch(self, workspace: Path) -> None:
        """[REPLY] marker discards everything before it."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me think about this..."}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Found it.\n\n[REPLY]\nThe answer is 42."}]}}),
            json.dumps({"type": "result", "result": "Found it.\n\n[REPLY]\nThe answer is 42."}),
        ]
        proc = self._make_proc(workspace, lines)
        result = await proc._read_until_result()
        assert result == "The answer is 42."
        assert "Let me think" not in result
        assert "Found it." not in result

    @pytest.mark.asyncio
    async def test_reply_sentinel_last_occurrence_wins(self, workspace: Path) -> None:
        """If [REPLY] appears multiple times, only text after the last one is sent."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "[REPLY]\nDraft 1"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "[REPLY]\nFinal answer"}]}}),
            json.dumps({"type": "result", "result": "[REPLY]\nFinal answer"}),
        ]
        proc = self._make_proc(workspace, lines)
        result = await proc._read_until_result()
        assert result == "Final answer"
        assert "Draft 1" not in result

    @pytest.mark.asyncio
    async def test_no_sentinel_returns_full_text(self, workspace: Path) -> None:
        """Without [REPLY], current behavior is preserved — everything is sent."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "First part."}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Second part."}]}}),
            json.dumps({"type": "result", "result": "Second part."}),
        ]
        proc = self._make_proc(workspace, lines)
        result = await proc._read_until_result()
        assert "First part." in result
        assert "Second part." in result


class TestInactivityTimeout:
    """`_read_until_result` should raise if the stream stays silent too long."""

    @pytest.mark.asyncio
    async def test_silent_stream_raises(self, workspace: Path) -> None:
        proc = AgentProcess(workspace=workspace, agent_name="testagent")
        proc._process = MagicMock()
        proc._process.stdout = asyncio.StreamReader()

        with pytest.raises(RuntimeError, match="silent"):
            await proc._read_until_result(inactivity_timeout=0.05)

    @pytest.mark.asyncio
    async def test_slow_stream_within_timeout_succeeds(self, workspace: Path) -> None:
        """Events arriving slower than inactivity_timeout should still complete."""
        proc = AgentProcess(workspace=workspace, agent_name="testagent")
        proc._process = MagicMock()
        proc._process.stdout = asyncio.StreamReader()

        async def feeder():
            await asyncio.sleep(0.02)
            proc._process.stdout.feed_data(
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}).encode() + b"\n"
            )
            await asyncio.sleep(0.02)
            proc._process.stdout.feed_data(
                json.dumps({"type": "result", "result": "hi"}).encode() + b"\n"
            )
            proc._process.stdout.feed_eof()

        feeder_task = asyncio.create_task(feeder())
        try:
            result = await proc._read_until_result(inactivity_timeout=0.5)
            assert result == "hi"
        finally:
            await feeder_task


class TestSendMessageRaw:
    """send_message(raw=...) controls the first-message time prefix."""

    def _make_proc(self, workspace: Path) -> tuple[AgentProcess, list[str]]:
        proc = AgentProcess(workspace=workspace, agent_name="testagent")
        proc._process = MagicMock()
        proc._ready.set()
        proc._first_message = True
        written: list[str] = []

        def capture(data: bytes) -> None:
            written.append(data.decode())

        proc._process.stdin = MagicMock()
        proc._process.stdin.write = capture
        proc._process.stdin.drain = AsyncMock()
        proc._process.stdout = asyncio.StreamReader()
        proc._process.stdout.feed_data(
            json.dumps({"type": "result", "result": "done"}).encode() + b"\n"
        )
        proc._process.stdout.feed_eof()
        return proc, written

    @pytest.mark.asyncio
    async def test_raw_skips_prefix_and_preserves_first_message(self, workspace: Path) -> None:
        proc, written = self._make_proc(workspace)
        await proc.send_message("/compact", raw=True)
        payload = json.loads(written[0])
        assert payload["message"]["content"] == "/compact"
        # The prefix is reserved for the next real message.
        assert proc._first_message is True

    @pytest.mark.asyncio
    async def test_non_raw_adds_prefix_and_consumes_first_message(self, workspace: Path) -> None:
        proc, written = self._make_proc(workspace)
        await proc.send_message("hello", raw=False)
        payload = json.loads(written[0])
        assert payload["message"]["content"].endswith("hello")
        assert payload["message"]["content"].startswith("[current time:")
        assert proc._first_message is False


class TestApplyReplySentinel:
    """Direct unit tests for the sentinel-stripping helper."""

    def test_no_sentinel(self) -> None:
        assert _apply_reply_sentinel("plain text") == "plain text"

    def test_sentinel_strips_prefix(self) -> None:
        assert _apply_reply_sentinel("scratch\n[REPLY]\nfinal") == "final"

    def test_last_occurrence_wins(self) -> None:
        assert _apply_reply_sentinel("[REPLY] draft\n[REPLY] real") == "real"

    def test_strips_surrounding_whitespace(self) -> None:
        assert _apply_reply_sentinel("x\n[REPLY]\n\n  hello  \n") == "hello"

    def test_empty_after_sentinel(self) -> None:
        assert _apply_reply_sentinel("scratch\n[REPLY]") == ""
