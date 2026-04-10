import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from claude_assistant.claude_process import AgentProcess


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
