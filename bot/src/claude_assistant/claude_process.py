from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


class AuthenticationError(RuntimeError):
    """Raised when Claude CLI returns an authentication error."""
    pass


def _check_auth_error(text: str) -> None:
    """Raise AuthenticationError if the text looks like an auth failure."""
    if "authentication_error" in text or "Invalid authentication credentials" in text:
        raise AuthenticationError(f"Claude authentication failed: {text[:200]}")


REPLY_SENTINEL = "[REPLY]"


def _apply_reply_sentinel(text: str) -> str:
    """If the agent included [REPLY], post only the text after the last occurrence.

    Lets the agent treat earlier text as scratch (planning, intermediate narration)
    and designate the final user-facing answer. Absent the marker, return text as-is.
    """
    if REPLY_SENTINEL in text:
        return text.rsplit(REPLY_SENTINEL, 1)[1].strip()
    return text


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
        agent_definition: dict[str, str] | None = None,
        allowed_tools_additional: list[str] | None = None,
        model: str | None = None,
        disallowed_skills: list[str] | None = None,
        effort: str | None = None,
        compact_pct: int | None = None,
        debug: bool = False,
        on_start_hooks: list[str] | None = None,
        on_stop_hooks: list[str] | None = None,
        can_stop_hooks: list[str] | None = None,
        hook_timeout: float = 15.0,
    ) -> None:
        self.workspace = workspace
        self.mcp_configs = mcp_configs or []
        self.agent_name = agent_name
        self.agent_definition = agent_definition
        self.allowed_tools_additional = allowed_tools_additional or []
        self.model = model
        self.disallowed_skills = disallowed_skills or []
        self.effort = effort
        self.compact_pct = compact_pct
        self.debug = debug
        self.on_start_hooks = on_start_hooks or []
        self.on_stop_hooks = on_stop_hooks or []
        self.can_stop_hooks = can_stop_hooks or []
        self.hook_timeout = hook_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._first_message = False
        # Set by the messaging MCP handler while this agent has a blocking
        # send_message call in flight — its stream is legitimately silent.
        self.awaiting_agent_reply = False
        # Lognormal idle timeout mimicking real Claude Code session lengths:
        #   median ~40 min, sigma 0.75 gives wide spread
        #   ~68%: 19 min – 85 min    ~95%: 9 min – 3 hrs
        #   cap 6 hrs to prevent extreme tail outliers
        self.reap_threshold = min(21600, 2400 * random.lognormvariate(0, 0.75))

    @property
    def busy(self) -> bool:
        """True when a send_message call is in progress (stream lock held)."""
        return self._lock.locked()

    def _build_command(self, session_id: str | None) -> list[str]:
        cmd = [
            "claude",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "acceptEdits",
        ]
        if self.agent_name and self.agent_definition:
            agents_json = json.dumps({
                self.agent_name: {
                    "description": self.agent_definition.get("description", ""),
                    "prompt": self.agent_definition.get("prompt", ""),
                }
            })
            cmd.extend(["--agents", agents_json])
            cmd.extend(["--agent", self.agent_name])
        elif self.agent_name:
            cmd.extend(["--agent", self.agent_name])
        if self.mcp_configs:
            # Strict: only these configs. Otherwise connectors synced from the
            # authenticated claude.ai account leak in, and the model reaches for
            # e.g. mcp__claude_ai_Gmail__* over the configured google_* MCP —
            # a tool the settings.json allow-list denies with no way to approve.
            cmd.append("--strict-mcp-config")
            cmd.extend(["--mcp-config"] + [str(p) for p in self.mcp_configs])
        if self.allowed_tools_additional:
            cmd.extend(["--allowedTools"] + self.allowed_tools_additional)
        disallowed = [f"Skill({s})" for s in self.disallowed_skills]
        if disallowed:
            cmd.extend(["--disallowedTools"] + disallowed)
        if self.model:
            cmd.extend(["--model", self.model])
        if self.effort:
            cmd.extend(["--effort", self.effort])
        if self.debug:
            cmd.append("--no-session-persistence")
        elif session_id:
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
        # Neutralize git's interactive editor so `rebase -i`, `commit` without
        # -m, etc. don't hang waiting on stdin.
        env["GIT_EDITOR"] = "true"
        # Background Task/Agent and Monitor would emit stream events after the
        # parent's `result`, which our one-turn-one-result reader can't surface.
        env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        if self.compact_pct is not None:
            env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(self.compact_pct)
        # Agent-to-agent sends can block up to 15 min; keep the CLI's MCP
        # tool timeout above the bot's own wait cap.
        env.setdefault("MCP_TOOL_TIMEOUT", "1200000")
        return env

    async def _run_hooks(self, hooks: list[str], phase: str) -> None:
        """Run shell hooks sequentially. Failures are logged, never raised."""
        for hook in hooks:
            log.info("Running %s hook for %s: %s", phase, self.agent_name, hook)
            try:
                hp = await asyncio.create_subprocess_shell(
                    hook,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._env(),
                )
                try:
                    _, stderr = await asyncio.wait_for(
                        hp.communicate(), timeout=self.hook_timeout
                    )
                except asyncio.TimeoutError:
                    hp.kill()
                    await hp.wait()
                    log.warning(
                        "%s hook timed out after %.1fs (agent=%s): %s",
                        phase, self.hook_timeout, self.agent_name, hook,
                    )
                    continue
                if hp.returncode != 0:
                    log.warning(
                        "%s hook failed (rc=%d, agent=%s): %s\nstderr: %s",
                        phase, hp.returncode, self.agent_name, hook,
                        stderr.decode(errors="replace").strip(),
                    )
                else:
                    log.debug(
                        "%s hook ok (agent=%s): %s", phase, self.agent_name, hook,
                    )
            except Exception:
                log.exception(
                    "%s hook raised (agent=%s): %s", phase, self.agent_name, hook,
                )

    async def can_stop(self) -> bool:
        """Run can_stop hooks. Return False (veto) if any exits non-zero or times out."""
        for hook in self.can_stop_hooks:
            try:
                hp = await asyncio.create_subprocess_shell(
                    hook,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._env(),
                )
                try:
                    _, stderr = await asyncio.wait_for(
                        hp.communicate(), timeout=self.hook_timeout
                    )
                except asyncio.TimeoutError:
                    hp.kill()
                    await hp.wait()
                    log.warning(
                        "can_stop hook timed out after %.1fs (agent=%s): %s — vetoing",
                        self.hook_timeout, self.agent_name, hook,
                    )
                    return False
                if hp.returncode != 0:
                    log.info(
                        "can_stop hook vetoed reap (rc=%d, agent=%s): %s",
                        hp.returncode, self.agent_name, hook,
                    )
                    return False
            except Exception:
                log.exception(
                    "can_stop hook raised (agent=%s): %s — vetoing",
                    self.agent_name, hook,
                )
                return False
        return True

    async def start(self, session_id: str | None = None) -> None:
        """Start the claude process. Session ID is captured lazily from stream events."""
        async with self._lifecycle_lock:
            self._ready.clear()
            await self._run_hooks(self.on_start_hooks, phase="on_start")
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
            self._first_message = True
            self._ready.set()

    async def _drain_stderr(self) -> None:
        """Log stderr from the claude process. Lines that look like errors get INFO."""
        if not self._process or not self._process.stderr:
            return
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").strip()
            lowered = text.lower()
            if any(k in lowered for k in ("error", "exception", "timeout", "traceback")):
                log.info("claude stderr (agent=%s): %s", self.agent_name, text)
            else:
                log.debug("claude stderr (agent=%s): %s", self.agent_name, text)

    async def send_message(
        self,
        content: str,
        timeout: float = 3600,
        inactivity_timeout: float = 300,
        raw: bool = False,
    ) -> str:
        """Send a message and return the result.

        Two timeouts:
        - ``timeout`` (default 60min): cap on the entire turn end-to-end.
        - ``inactivity_timeout`` (default 5min): max gap between stream-json
          events. Catches silent wedges (stuck MCP child, hung tool call)
          where the subprocess is alive but not emitting.

        ``raw`` sends ``content`` verbatim: it skips the first-message
        ``[current time: ...]`` prefix (which would corrupt a slash command
        like ``/compact``) and leaves ``_first_message`` untouched so the next
        real user message still gets its time prefix.
        """
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
            if self._first_message and not raw:
                now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
                content = f"[current time: {now}] {content}"
                self._first_message = False
            sid = self._session_id or "default"
            msg = self._format_input(content, sid)
            log.info(
                "send_message start (agent=%s, prompt_len=%d)",
                self.agent_name, len(content),
            )
            started = asyncio.get_event_loop().time()
            self._process.stdin.write(msg.encode() + b"\n")
            await self._process.stdin.drain()

            try:
                result = await asyncio.wait_for(
                    self._read_until_result(inactivity_timeout=inactivity_timeout),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                elapsed = asyncio.get_event_loop().time() - started
                log.warning(
                    "send_message overall timeout (agent=%s, elapsed=%.1fs, cap=%.0fs)",
                    self.agent_name, elapsed, timeout,
                )
                raise RuntimeError(
                    f"Agent {self.agent_name} did not respond within {timeout}s"
                )
            elapsed = asyncio.get_event_loop().time() - started
            log.info(
                "send_message done (agent=%s, elapsed=%.1fs, result_len=%d)",
                self.agent_name, elapsed, len(result),
            )
            return result

    async def _read_until_result(self, inactivity_timeout: float = 300) -> str:
        """Read stream-json events until a result event arrives.

        Collects text from assistant events across all turns so that text
        produced before tool calls isn't lost — the ``result`` field in the
        final event only contains text from the *last* turn.

        Raises ``RuntimeError`` if no event arrives within
        ``inactivity_timeout`` seconds — catches the case where claude is
        alive but stuck (e.g. on a hung MCP child) and would otherwise pin
        a Discord task until the outer ``send_message`` cap fires.
        """
        if not self._process or not self._process.stdout:
            raise RuntimeError("Process not available")
        collected_text: list[str] = []
        while True:
            try:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(),
                    timeout=inactivity_timeout,
                )
            except asyncio.TimeoutError:
                if self.awaiting_agent_reply:
                    continue
                raise RuntimeError(
                    f"Agent {self.agent_name} stream silent for {inactivity_timeout:.0f}s — "
                    "likely stuck on a tool call or MCP child"
                )
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
                    final = result_text
                elif result_text and result_text.strip() == collected_text[-1]:
                    final = "\n\n".join(collected_text) if len(collected_text) > 1 else result_text
                elif not result_text.strip():
                    final = "\n\n".join(collected_text)
                elif collected_text[0] in result_text:
                    final = result_text
                elif result_text.strip():
                    final = "\n\n".join(collected_text + [result_text])
                else:
                    final = "\n\n".join(collected_text)
                return _apply_reply_sentinel(final)

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
            await self._run_hooks(self.on_stop_hooks, phase="on_stop")
