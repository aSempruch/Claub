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
        workspace: Path,
        mcp_configs: list[Path] | None = None,
        agent_name: str | None = None,
        allowed_tools_additional: list[str] | None = None,
        model: str | None = None,
        sibling_agent_names: list[str] | None = None,
    ) -> None:
        self.workspace = workspace
        self.mcp_configs = mcp_configs or []
        self.agent_name = agent_name
        self.allowed_tools_additional = allowed_tools_additional or []
        self.model = model
        self.sibling_agent_names = [
            n for n in (sibling_agent_names or []) if n != agent_name
        ]
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
        if self.allowed_tools_additional:
            cmd.extend(["--allowedTools"] + self.allowed_tools_additional)
        if self.sibling_agent_names:
            cmd.extend(
                ["--disallowedTools"]
                + [f"Agent({n})" for n in self.sibling_agent_names]
            )
        if self.model:
            cmd.extend(["--model", self.model])
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
        env.pop("DISCORD_BOT_TOKEN", None)
        if self.agent_name:
            env["CLAUB_AGENT_NAME"] = self.agent_name
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

    async def send_message(self, content: str, timeout: float = 900) -> str:
        """Send a message and return the result. Timeout in seconds (default 15min)."""
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
        """Read stream-json events until a result event arrives.

        Collects text from assistant events across all turns so that text
        produced before tool calls isn't lost — the ``result`` field in the
        final event only contains text from the *last* turn.
        """
        if not self._process or not self._process.stdout:
            raise RuntimeError("Process not available")
        collected_text: list[str] = []
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

            # Collect text blocks from assistant messages across all turns
            if event.get("type") == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        collected_text.append(block["text"].strip())

            if self._is_result_event(event):
                result_text = self._extract_result(event)
                if not collected_text:
                    return result_text
                # The result field only has text from the last turn.
                # If earlier turns produced text not in result, prepend it.
                if result_text and result_text.strip() == collected_text[-1]:
                    # Result matches only the last text block — include earlier ones
                    if len(collected_text) > 1:
                        return "\n\n".join(collected_text)
                    return result_text
                elif not result_text.strip():
                    # Result is empty but we collected text earlier
                    return "\n\n".join(collected_text)
                else:
                    # Result has content — check if it already includes everything
                    # by seeing if our first collected text appears in the result
                    if collected_text[0] in result_text:
                        return result_text
                    return "\n\n".join(collected_text + [result_text]) if result_text.strip() else "\n\n".join(collected_text)

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
