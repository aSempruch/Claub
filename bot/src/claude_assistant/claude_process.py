from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class AuthenticationError(RuntimeError):
    """Raised when Claude CLI returns an authentication error."""
    pass


def _check_auth_error(text: str) -> None:
    """Raise AuthenticationError if the text looks like an auth failure."""
    if "authentication_error" in text or "Invalid authentication credentials" in text:
        raise AuthenticationError(f"Claude authentication failed: {text[:200]}")


def _check_error_event(event: dict) -> None:
    """Raise on error-type JSON events from the Claude CLI."""
    if event.get("type") == "error":
        error = event.get("error", {})
        if isinstance(error, dict) and error.get("type") == "authentication_error":
            raise AuthenticationError(
                f"Claude authentication failed: {error.get('message', '')}"
            )
        msg = error.get("message", "") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"Claude error: {msg}")


class AgentProcess:
    """Long-running Claude Code process with stream-json I/O.

    Used for all agents (main and sub-agents alike). Each agent gets its own
    persistent process that stays alive between messages.
    """

    def __init__(
        self,
        home_dir: Path,
        workspace: Path,
        mcp_configs: list[Path] | None = None,
        agent_name: str | None = None,
    ) -> None:
        self.home_dir = home_dir
        self.workspace = workspace
        self.mcp_configs = mcp_configs or []
        self.agent_name = agent_name
        self._process: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
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

    async def start(self, session_id: str | None = None) -> None:
        """Start the claude process. Session ID is captured lazily from stream events."""
        async with self._lifecycle_lock:
            self._ready.clear()
            cmd = self._build_command(session_id)
            log.info("Starting agent %s: %s", self.agent_name or "unnamed", " ".join(cmd))
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
                env=self._env(),
                limit=10 * 1024 * 1024,  # 10MB — Claude stream-json can emit large lines
            )
            asyncio.create_task(self._drain_stderr())
            self._ready.set()

    async def _drain_stderr(self) -> None:
        """Log stderr from the claude process."""
        if not self._process or not self._process.stderr:
            return
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            log.debug("claude stderr: %s", line.decode().strip())

    async def send_message(self, content: str, timeout: float = 300) -> str:
        """Send a message and return the result. Timeout in seconds (default 5min)."""
        try:
            async with asyncio.timeout(30):
                await self._ready.wait()
        except TimeoutError:
            raise RuntimeError(
                f"Agent {self.agent_name} not ready within 30s"
            )
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise RuntimeError(f"Agent {self.agent_name} process not started")
        async with self._lock:
            sid = self._session_id or "default"
            msg = self._format_input(content, sid)
            self._process.stdin.write(msg.encode() + b"\n")
            await self._process.stdin.drain()

            try:
                return await asyncio.wait_for(
                    self._read_until_result(), timeout=timeout
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Agent {self.agent_name} did not respond within {timeout}s"
                )

    async def _read_until_result(self) -> str:
        if not self._process or not self._process.stdout:
            raise RuntimeError("Process not available")
        while True:
            line = await self._process.stdout.readline()
            if not line:
                raise RuntimeError("Claude process ended unexpectedly")
            try:
                event = json.loads(line.decode().strip())
            except json.JSONDecodeError:
                continue

            _check_error_event(event)

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
        async with self._lifecycle_lock:
            if self._process and self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    self._process.kill()
            self._ready.clear()
