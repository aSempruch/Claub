import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from claude_assistant.claude_process import AgentProcess


@pytest.fixture
def home_dir(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    return home


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspaces" / "main"
    ws.mkdir(parents=True)
    return ws


class TestAgentProcess:
    @pytest.mark.asyncio
    async def test_build_command_first_run(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = AgentProcess(home_dir=home_dir, workspace=workspace)
        cmd = proc._build_command(session_id=None)
        assert "claude" in cmd
        assert "--input-format" in cmd
        assert "stream-json" in cmd
        assert "--resume" not in cmd

    @pytest.mark.asyncio
    async def test_build_command_resume(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = AgentProcess(home_dir=home_dir, workspace=workspace)
        cmd = proc._build_command(session_id="uuid-123")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "uuid-123"

    @pytest.mark.asyncio
    async def test_build_command_with_agent_name(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = AgentProcess(home_dir=home_dir, workspace=workspace, agent_name="journalist")
        cmd = proc._build_command(session_id=None)
        assert "--agent" in cmd
        idx = cmd.index("--agent")
        assert cmd[idx + 1] == "journalist"

    @pytest.mark.asyncio
    async def test_format_input_message(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = AgentProcess(home_dir=home_dir, workspace=workspace)
        msg = proc._format_input("hello", session_id="default")
        parsed = json.loads(msg)
        assert parsed["type"] == "user"
        assert parsed["message"]["role"] == "user"
        assert parsed["message"]["content"] == "hello"
        assert parsed["session_id"] == "default"
        assert parsed["parent_tool_use_id"] is None

    @pytest.mark.asyncio
    async def test_parse_init_event(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = AgentProcess(home_dir=home_dir, workspace=workspace)
        event = {"type": "system", "subtype": "init", "session_id": "abc-123"}
        assert proc._parse_session_id(event) == "abc-123"

    @pytest.mark.asyncio
    async def test_parse_result_event(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = AgentProcess(home_dir=home_dir, workspace=workspace)
        event = {"type": "result", "subtype": "success", "result": "hello"}
        assert proc._is_result_event(event)
        assert proc._extract_result(event) == "hello"

    @pytest.mark.asyncio
    async def test_parse_non_result_event(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = AgentProcess(home_dir=home_dir, workspace=workspace)
        event = {"type": "assistant", "message": {}}
        assert not proc._is_result_event(event)

    @pytest.mark.asyncio
    async def test_env_includes_agent_name(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = AgentProcess(home_dir=home_dir, workspace=workspace, agent_name="journalist")
        env = proc._env()
        assert env["CLAUB_AGENT_NAME"] == "journalist"

    @pytest.mark.asyncio
    async def test_env_no_agent_name(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = AgentProcess(home_dir=home_dir, workspace=workspace)
        env = proc._env()
        assert "CLAUB_AGENT_NAME" not in env
