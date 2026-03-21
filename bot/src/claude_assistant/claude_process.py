from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class MainAgentProcess:
    """Long-running Claude Code process with stream-json I/O."""

    def __init__(self, home_dir: Path, workspace: Path, mcp_configs: list[Path] | None = None, agent_name: str | None = None) -> None:
        self.home_dir = home_dir
        self.workspace = workspace
        self.mcp_configs = mcp_configs or []
        self.agent_name = agent_name
        self._process: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    def _build_command(self, session_id: str | None) -> list[str]:
        cmd = [
            "claude",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "acceptEdits",
        ]
        if self.agent_name:
            cmd.extend(["--agent", self.agent_name])
        if self.mcp_configs:
            cmd.extend(["--mcp-config"] + [str(p) for p in self.mcp_configs])
        if session_id:
            cmd.extend(["--resume", session_id])
        return cmd

    def _format_input(self, content: str, session_id: str) -> str:
        return json.dumps({
            "type": "user",
            "message": {"role": "user", "content": content},
            "session_id": session_id,
            "parent_tool_use_id": None,
        })

    def _parse_session_id(self, event: dict) -> str | None:
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event.get("session_id")
        return None

    def _is_result_event(self, event: dict) -> bool:
        return event.get("type") == "result"

    def _extract_result(self, event: dict) -> str:
        return event.get("result", "")

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.home_dir)
        return env

    async def start(self, session_id: str | None = None) -> str | None:
        """Start the claude process. Returns immediately — session ID captured lazily."""
        cmd = self._build_command(session_id)
        log.info("Starting main agent: %s", " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
            env=self._env(),
        )
        asyncio.create_task(self._drain_stderr())
        self._ready.set()
        return self._session_id

    async def _drain_stderr(self) -> None:
        """Log stderr from the claude process."""
        assert self._process and self._process.stderr
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            log.debug("claude stderr: %s", line.decode().strip())

    async def send_message(self, content: str, timeout: float = 300) -> str:
        """Send a message and return the result. Timeout in seconds (default 5min)."""
        await self._ready.wait()
        assert self._process and self._process.stdin and self._process.stdout
        async with self._lock:
            sid = self._session_id or "default"
            msg = self._format_input(content, sid)
            self._process.stdin.write(msg.encode() + b"\n")
            await self._process.stdin.drain()

            # Read until result event (with timeout)
            # This also captures session_id from init events along the way
            try:
                return await asyncio.wait_for(
                    self._read_until_result(), timeout=timeout
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Claude process did not respond within {timeout}s"
                )

    async def _read_until_result(self) -> str:
        assert self._process and self._process.stdout
        while True:
            line = await self._process.stdout.readline()
            if not line:
                raise RuntimeError("Claude process ended unexpectedly")
            try:
                event = json.loads(line.decode().strip())
            except json.JSONDecodeError:
                continue

            # Capture session_id if we see it
            sid = self._parse_session_id(event)
            if sid:
                self._session_id = sid

            if self._is_result_event(event):
                return self._extract_result(event)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def stop(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._process.kill()


class SubAgentRunner:
    """One-shot claude -p runner for sub-agents."""

    def __init__(
        self, agent_name: str, home_dir: Path, workspace: Path, mcp_configs: list[Path] | None = None
    ) -> None:
        self.agent_name = agent_name
        self.home_dir = home_dir
        self.workspace = workspace
        self.mcp_configs = mcp_configs or []

    def _build_command(
        self, session_id: str | None, prompt: str
    ) -> list[str]:
        cmd = [
            "claude", "-p",
            "--agent", self.agent_name,
            "--output-format", "json",
            "--permission-mode", "acceptEdits",
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        if self.mcp_configs:
            cmd.extend(["--mcp-config"] + [str(p) for p in self.mcp_configs])
        cmd.extend(["--", prompt])
        return cmd

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.home_dir)
        return env

    async def run(
        self, prompt: str, session_id: str | None = None, timeout: float = 300
    ) -> tuple[str, str]:
        """Run the agent. Returns (result_text, session_id). Timeout in seconds."""
        cmd = self._build_command(session_id, prompt)
        log.info("Running sub-agent %s: %s", self.agent_name, " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
            env=self._env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(
                f"Sub-agent {self.agent_name} timed out after {timeout}s"
            )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Sub-agent {self.agent_name} failed (rc={proc.returncode}): "
                f"{stderr.decode()[:500]}"
            )
        output = json.loads(stdout.decode())
        return output["result"], output["session_id"]
