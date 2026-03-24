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
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


class TestAgentProcessIntegration:
    @pytest.mark.asyncio
    async def test_start_send_stop(
        self, workspace: Path
    ) -> None:
        proc = AgentProcess(workspace=workspace)
        await proc.start()

        result = await proc.send_message(
            "say the word hello and nothing else"
        )
        assert "hello" in result.lower()

        await proc.stop()
        assert not proc.is_alive
