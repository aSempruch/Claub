"""Integration tests that run against the real Claude CLI.

Skipped unless CLAUDE_INTEGRATION_TEST=1 is set.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from claude_assistant.claude_process import AgentProcess

pytestmark = pytest.mark.skipif(
    os.environ.get("CLAUDE_INTEGRATION_TEST") != "1",
    reason="Set CLAUDE_INTEGRATION_TEST=1 to run",
)


@pytest.fixture
def home_dir(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    claude_dir = home / ".claude"
    claude_dir.mkdir()
    # Copy real credentials
    real_creds = Path.home() / ".claude" / ".credentials.json"
    if real_creds.exists():
        (claude_dir / ".credentials.json").write_text(real_creds.read_text())
    return home


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


class TestAgentProcessIntegration:
    @pytest.mark.asyncio
    async def test_start_send_stop(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = AgentProcess(home_dir=home_dir, workspace=workspace)
        await proc.start()

        result = await proc.send_message(
            "say the word hello and nothing else"
        )
        assert "hello" in result.lower()

        await proc.stop()
        assert not proc.is_alive
