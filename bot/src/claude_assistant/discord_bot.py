from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import signal
import time
from pathlib import Path

import discord

from claude_assistant.attachments import download_attachments
from claude_assistant.chunker import chunk_message
from claude_assistant.claude_process import AgentProcess, AuthenticationError
from claude_assistant.config import AssistantConfig, parse_agent_file
from claude_assistant.file_sender import extract_files
from claude_assistant.firing_history import FiringHistory
from claude_assistant.mcp_server import create_mcp_server
from claude_assistant.router import Router
from claude_assistant.schedule_store import ScheduleStore
from claude_assistant.scheduler import Scheduler
from claude_assistant.session import SessionStore

log = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _safe_typing(channel: discord.abc.Messageable):
    """channel.typing() that doesn't drop the message if Discord rejects the typing call.

    Discord can return 429 (error 40062) on /typing for a specific channel even when the
    rest of the API works. The typing indicator is cosmetic, so swallow HTTPException
    from entering the context and proceed without the indicator.
    """
    cm = channel.typing()
    entered = False
    try:
        await cm.__aenter__()
        entered = True
    except discord.HTTPException as e:
        log.warning("typing indicator unavailable for channel %s: %s", channel.id, e)
    try:
        yield
    finally:
        if entered:
            await cm.__aexit__(None, None, None)


def _merged_hooks(config: AssistantConfig, agent_name: str, attr: str) -> list[str]:
    """Concatenate global and per-agent hook lists (global first)."""
    global_hooks: list[str] = list(getattr(config, attr, []) or [])
    agent_cfg = config.agents.get(agent_name)
    agent_hooks: list[str] = list(getattr(agent_cfg, attr, []) or []) if agent_cfg else []
    return global_hooks + agent_hooks


def _ensure_authoring_symlink(workspace: Path, name: str) -> None:
    """Set up agent self-authoring: real dir at .claude-{name}/, symlink at .claude/{name}/.

    Agents write to .claude-{name}/ (not blocked by Claude Code, which would block
    writes through a symlink targeting .claude/). Claude Code still discovers content
    by reading .claude/{name}/ which symlinks to the real dir.

    Used for both skills and agents (subagents) so agents can author their own without
    being granted write access to .claude/ itself (which would let them edit settings.json).
    """
    real_dir = workspace / f".claude-{name}"
    real_dir.mkdir(parents=True, exist_ok=True)
    claude_dir = workspace / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    link = claude_dir / name
    if link.is_dir() and not link.is_symlink():
        # Migrate existing real .claude/{name}/ contents
        import shutil
        for item in link.iterdir():
            dest = real_dir / item.name
            if not dest.exists():
                shutil.move(str(item), str(dest))
        link.rmdir()
    if not link.is_symlink():
        link.symlink_to(f"../.claude-{name}")


def build_agent_process(
    *,
    name: str,
    workspace: Path,
    config: AssistantConfig,
    mcp_configs: list[Path],
    agent_definition: dict[str, str] | None,
    disallowed_skills: list[str],
    model_override: str | None = None,
    debug: bool = False,
) -> AgentProcess:
    """Build an AgentProcess with production-matching flags.

    Used by both the Discord bot's normal startup path and the debug CLI so
    the command construction stays in one place.
    """
    agent_config = config.agents.get(name)
    model = model_override or (agent_config.model if agent_config and agent_config.model else config.model)
    effort = (agent_config.effort if agent_config and agent_config.effort else config.effort)
    compact_pct = (agent_config.compact_pct if agent_config and agent_config.compact_pct else config.compact_pct)
    return AgentProcess(
        workspace=workspace,
        mcp_configs=mcp_configs,
        agent_name=name,
        agent_definition=agent_definition,
        allowed_tools_additional=agent_config.allowed_tools_additional if agent_config else [],
        model=model,
        disallowed_skills=disallowed_skills,
        effort=effort,
        compact_pct=compact_pct,
        debug=debug,
        on_start_hooks=_merged_hooks(config, name, "on_start"),
        on_stop_hooks=_merged_hooks(config, name, "on_stop"),
        can_stop_hooks=_merged_hooks(config, name, "can_stop"),
    )


class AssistantBot:
    def __init__(
        self,
        config: AssistantConfig,
        workspaces_dir: Path,
        session_store: SessionStore,
        schedule_store: ScheduleStore,
        firing_history: FiringHistory | None = None,
        mcp_config: Path | None = None,
        agents_dir: Path | None = None,
        mcp_port: int = 9400,
        all_skills: list[str] | None = None,
    ) -> None:
        self.config = config
        self.workspaces_dir = workspaces_dir
        self.sessions = session_store
        self.schedule_store = schedule_store
        self.firing_history = firing_history
        self.mcp_config = mcp_config
        self.agents_dir = agents_dir
        self.mcp_port = mcp_port
        self.all_skills = all_skills or []
        self.router = Router(config)

        self._processes: dict[str, AgentProcess] = {}
        self._webhooks: dict[int, discord.Webhook] = {}  # channel_id -> webhook
        self._supervisor_task: asyncio.Task | None = None
        self._idle_reaper_task: asyncio.Task | None = None
        self._mcp_server_task: asyncio.Task | None = None
        self._shutting_down = False
        self._last_activity: dict[str, float] = {}  # agent name -> timestamp
        self._reaped: set[str] = set()  # agents intentionally killed by idle reaper
        self._veto_pin_started: dict[str, float] = {}  # agent -> first-veto monotonic ts
        # Per-process reap threshold is set in AgentProcess.__init__ (lognormal, median ~40 min)
        self._max_pin_secs = float(os.environ.get("CLAUB_MAX_PIN_SECS", "172800"))

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._setup_events()

    def _setup_events(self) -> None:
        # setup_hook fires once before the gateway connects; on_ready fires
        # on every (re)connect. Putting init in setup_hook avoids spawning
        # duplicate supervisor/reaper/scheduler tasks and rebinding the MCP
        # server port on Discord reconnects.
        async def setup_hook() -> None:
            self._scheduler = Scheduler(
                self.schedule_store,
                self._handle_scheduled,
                valid_agents=set(self.config.agents.keys()),
                history=self.firing_history,
            )
            self._scheduler.start()
            self._supervisor_task = asyncio.create_task(self._supervise_all())
            self._idle_reaper_task = asyncio.create_task(self._reap_idle_processes())
            self._mcp_server_task = asyncio.create_task(self._start_mcp_server())

        self._client.setup_hook = setup_hook  # type: ignore[method-assign]

        @self._client.event
        async def on_ready() -> None:
            log.info("Discord connected as %s", self._client.user)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            if message.author == self._client.user or message.author.bot:
                return
            if self.config.allowed_user_ids and str(message.author.id) not in self.config.allowed_user_ids:
                return
            await self._handle_message(message)

    # --- Agent process lifecycle ---

    def _disallowed_skills_for(self, name: str) -> list[str]:
        """Return skills this agent is NOT allowed to use."""
        agent_config = self.config.agents.get(name)
        allowed = set(self.config.allowed_skills)
        if agent_config:
            allowed |= set(agent_config.allowed_skills)
        return [s for s in self.all_skills if s not in allowed]

    def _load_agent_definition(self, name: str) -> dict[str, str] | None:
        """Load the agent .md file from the agents dir, if it exists."""
        if not self.agents_dir:
            return None
        agent_file = self.agents_dir / f"{name}.md"
        if not agent_file.exists():
            return None
        try:
            return parse_agent_file(agent_file)
        except Exception:
            log.exception("Failed to parse agent file %s", agent_file)
            return None

    async def _start_agent(self, name: str) -> AgentProcess:
        """Start (or restart) an agent process. Returns the new process."""
        workspace = self.workspaces_dir / name
        workspace.mkdir(parents=True, exist_ok=True)
        _ensure_authoring_symlink(workspace, "skills")
        _ensure_authoring_symlink(workspace, "agents")
        process = build_agent_process(
            name=name,
            workspace=workspace,
            config=self.config,
            mcp_configs=self._mcp_configs_for(name),
            agent_definition=self._load_agent_definition(name),
            disallowed_skills=self._disallowed_skills_for(name),
            model_override=self.sessions.get_model(name),
        )
        session_id = self.sessions.get(name)
        try:
            await process.start(session_id)
        except Exception:
            log.exception("Failed to start agent %s", name)
            if session_id:
                log.info("Retrying %s without --resume", name)
                self.sessions.delete(name)
                await process.start(None)
        self._processes[name] = process
        self._reaped.discard(name)
        return process

    async def _get_or_start_process(self, name: str) -> AgentProcess:
        """Return a live process for the agent, starting one if needed."""
        process = self._processes.get(name)
        if process and process.is_alive:
            return process
        return await self._start_agent(name)

    async def _restart_process(self, name: str) -> AgentProcess:
        """Stop an existing process and start a fresh one."""
        process = self._processes.get(name)
        if process:
            await process.stop()
        return await self._start_agent(name)

    async def _supervise_all(self) -> None:
        """Background task that monitors agent processes and notifies on unexpected death."""
        try:
            while True:
                await asyncio.sleep(5)
                for name, process in list(self._processes.items()):
                    if not process.is_alive and name not in self._reaped:
                        log.warning("Agent %s died unexpectedly", name)
                        await self._notify_channel(name, f"Agent `{name}` died. Send a message to restart it.")
                        del self._processes[name]
                        self._last_activity.pop(name, None)
        except asyncio.CancelledError:
            log.info("Supervisor loop cancelled")
            return

    async def _reap_idle_processes(self) -> None:
        """Kill agent processes that have been idle longer than _idle_timeout."""
        try:
            while True:
                await asyncio.sleep(60)
                now = time.monotonic()
                for name, process in list(self._processes.items()):
                    if not process.is_alive:
                        continue
                    last = self._last_activity.get(name, 0)
                    idle_secs = now - last if last else 0
                    if not (last and idle_secs > process.reap_threshold and not process.busy):
                        self._veto_pin_started.pop(name, None)
                        continue
                    # Random delay so kill times don't land on round minutes
                    await asyncio.sleep(random.uniform(0, 60))
                    # Re-check: a message may have arrived during the delay
                    if process.busy:
                        self._veto_pin_started.pop(name, None)
                        continue
                    if not await process.can_stop():
                        pin_start = self._veto_pin_started.setdefault(name, now)
                        pinned_for = now - pin_start
                        if pinned_for <= self._max_pin_secs:
                            log.info(
                                "Reap of %s vetoed by can_stop hook (pinned %.0fs/%.0fs)",
                                name, pinned_for, self._max_pin_secs,
                            )
                            continue
                        log.warning(
                            "Reap of %s proceeding despite veto (pinned %.0fs > cap %.0fs)",
                            name, pinned_for, self._max_pin_secs,
                        )
                    self._veto_pin_started.pop(name, None)
                    log.info(
                        "Reaping idle agent %s (idle %.0fs, threshold %.0fs)",
                        name, idle_secs, process.reap_threshold,
                    )
                    self._reaped.add(name)
                    await process.stop()
                    del self._processes[name]
        except asyncio.CancelledError:
            log.info("Idle reaper cancelled")
            return

    async def _start_mcp_server(self) -> None:
        mcp = create_mcp_server(
            self.schedule_store,
            self._scheduler,
            notify=self._notify_channel,
            history=self.firing_history,
        )
        log.info("Starting MCP server on 127.0.0.1:%d", self.mcp_port)
        await mcp.run_http_async(
            host="127.0.0.1", port=self.mcp_port, log_level="warning", show_banner=False,
        )

    # --- Message handling ---

    async def _handle_message(self, message: discord.Message) -> None:
        channel_id = str(message.channel.id)
        content = message.content.strip()

        if content == "/clear":
            await self._handle_reset(message)
            return

        if content == "/stop":
            await self._handle_stop(message)
            return

        agent_name = self.router.route(channel_id)
        if agent_name is None:
            return

        await self._handle_agent_message(message, agent_name, content)

    async def _handle_agent_message(
        self, message: discord.Message, agent_name: str, content: str
    ) -> None:
        log.info(
            "Handling message for %s (chan=%s, len=%d)",
            agent_name, message.channel.id, len(content),
        )
        async with _safe_typing(message.channel):
            footer = await download_attachments(message, agent_name)
            if footer:
                content = (content + footer) if content else footer.lstrip()
            try:
                result = await self._send_with_restart(agent_name, content)
            except AuthenticationError:
                log.error("Claude authentication failed for %s", agent_name)
                await message.channel.send(
                    "Claude authentication expired. Re-authenticate with `claude` on the host and restart."
                )
                return
            except RuntimeError as e:
                log.exception("Agent %s failed after restart-and-retry", agent_name)
                await message.channel.send(f"Agent stalled: {e}")
                return

            process = self._processes.get(agent_name)
            if process and process.session_id:
                self.sessions.set(agent_name, process.session_id)

        await self._deliver_result(message.channel, agent_name, result)

    async def _send_with_restart(self, agent_name: str, content: str) -> str:
        """Send a message to an agent, restarting the process on failure."""
        self._last_activity[agent_name] = time.monotonic()
        process = await self._get_or_start_process(agent_name)
        try:
            result = await process.send_message(content)
        except AuthenticationError:
            raise
        except RuntimeError:
            if agent_name in self._reaped:
                raise
            log.exception("Agent %s error, restarting", agent_name)
            process = await self._restart_process(agent_name)
            result = await process.send_message(content)
        self._last_activity[agent_name] = time.monotonic()
        return result

    # --- Scheduled tasks ---

    async def _handle_scheduled(self, agent_name: str, prompt: str) -> None:
        agent_config = self.config.agents.get(agent_name)
        if not agent_config:
            return

        channel = self._client.get_channel(int(agent_config.channel_id))
        if not channel or not isinstance(channel, discord.TextChannel):
            log.error("Channel not found for agent %s", agent_name)
            return

        try:
            result = await self._send_with_restart(agent_name, prompt)
        except AuthenticationError:
            log.error("Claude authentication failed for scheduled agent %s", agent_name)
            await channel.send(
                "Claude authentication expired. Re-authenticate with `claude` on the host and restart."
            )
            return
        except RuntimeError as e:
            log.exception("Scheduled task failed for %s", agent_name)
            await channel.send(f"Scheduled task failed: {e}")
            return

        if "[NO_POST]" in result:
            log.info("Agent %s opted out of posting", agent_name)
            return

        process = self._processes.get(agent_name)
        if process and process.session_id:
            self.sessions.set(agent_name, process.session_id)

        await self._deliver_result(channel, agent_name, result)

    # --- Commands ---

    async def _handle_reset(self, message: discord.Message) -> None:
        agent_name = self.router.route(str(message.channel.id))
        if agent_name is None:
            return

        self._reaped.add(agent_name)
        process = self._processes.get(agent_name)
        if process:
            await process.stop()
            del self._processes[agent_name]
        self._last_activity.pop(agent_name, None)
        self.sessions.delete(agent_name)
        await message.channel.send(f"Agent `{agent_name}` reset.")

    async def _handle_stop(self, message: discord.Message) -> None:
        agent_name = self.router.route(str(message.channel.id))
        if agent_name is None:
            return

        log.info("/stop received for %s (chan=%s)", agent_name, message.channel.id)
        self._reaped.add(agent_name)
        process = self._processes.get(agent_name)
        if process:
            log.info("/stop calling process.stop() for %s", agent_name)
            await process.stop()
            del self._processes[agent_name]
            log.info("/stop process stopped for %s", agent_name)
        self._last_activity.pop(agent_name, None)
        await message.channel.send(f"Agent `{agent_name}` stopped.")

    # --- Webhook sending ---

    async def _get_or_create_webhook(
        self, channel: discord.TextChannel
    ) -> discord.Webhook:
        """Return a cached webhook for the channel, creating one if needed."""
        if channel.id in self._webhooks:
            return self._webhooks[channel.id]
        for wh in await channel.webhooks():
            if wh.name == "claub-agent":
                self._webhooks[channel.id] = wh
                return wh
        wh = await channel.create_webhook(name="claub-agent")
        self._webhooks[channel.id] = wh
        return wh

    # Per-attempt cap on delivering one agent reply to Discord. During the
    # 2026-07-16 Discord API incident a webhook send hung indefinitely inside
    # discord.py (no total timeout on its HTTP calls), silently discarding the
    # reply and parking the handler task.
    DELIVER_TIMEOUT_S = 60.0

    async def _deliver_result(
        self,
        channel: discord.TextChannel,
        agent_name: str,
        result: str,
    ) -> None:
        """Deliver an agent reply with a timeout and one retry; log loudly on loss."""
        for attempt in (1, 2):
            try:
                async with asyncio.timeout(self.DELIVER_TIMEOUT_S):
                    await self._send_chunked(channel, agent_name, result)
                return
            except Exception:
                if attempt == 1:
                    log.warning(
                        "Delivering reply for %s failed, retrying once", agent_name,
                        exc_info=True,
                    )
                else:
                    log.error(
                        "Reply from %s LOST after retry (len=%d) — Discord send failed twice",
                        agent_name, len(result),
                        exc_info=True,
                    )

    async def _send_chunked(
        self,
        channel: discord.TextChannel,
        agent_name: str,
        result: str,
    ) -> None:
        """Chunk a result and send non-empty pieces to Discord."""
        result, files = extract_files(result)
        chunks = chunk_message(result)
        if not chunks or all(not c.strip() for c in chunks):
            if files:
                # No text but have files — send files alone
                await self._send_response(channel, agent_name, "", files=files)
            else:
                log.warning("Agent %s returned empty response", agent_name)
            return
        for i, chunk in enumerate(chunks):
            if chunk.strip():
                # Attach files to the last chunk
                chunk_files = files if i == len(chunks) - 1 else None
                await self._send_response(channel, agent_name, chunk, files=chunk_files)

    async def _send_response(
        self,
        channel: discord.TextChannel,
        agent_name: str,
        content: str,
        files: list[discord.File] | None = None,
    ) -> None:
        """Send a message as the agent, using app identity or webhook per config."""
        agent_config = self.config.agents.get(agent_name)
        if not agent_config or not agent_config.display_name:
            kwargs: dict = {}
            if content:
                kwargs["content"] = content
            if files:
                kwargs["files"] = files
            if kwargs:
                await channel.send(**kwargs)
        else:
            await self._webhook_send(channel, agent_name, content, files=files)

    async def _webhook_send(
        self,
        channel: discord.TextChannel,
        agent_name: str,
        content: str,
        files: list[discord.File] | None = None,
    ) -> None:
        """Send a message as the agent via webhook with custom name/avatar."""
        agent_config = self.config.agents.get(agent_name)
        webhook = await self._get_or_create_webhook(channel)
        kwargs: dict = {}
        if content:
            kwargs["content"] = content
        if files:
            kwargs["files"] = files
        if agent_config and agent_config.display_name:
            kwargs["username"] = agent_config.display_name
        if agent_config and agent_config.avatar_url:
            kwargs["avatar_url"] = agent_config.avatar_url
        await webhook.send(**kwargs)

    # --- Utilities ---

    def _mcp_configs_for(self, agent_name: str) -> list[Path]:
        """Build MCP config list: shared + per-agent if it exists."""
        configs: list[Path] = []
        if self.mcp_config:
            configs.append(self.mcp_config)
        if self.agents_dir:
            per_agent = self.agents_dir / f"{agent_name}.mcp.json"
            if per_agent.exists():
                configs.append(per_agent)
        return configs

    async def _notify_channel(self, agent_name: str, text: str) -> None:
        agent_config = self.config.agents.get(agent_name)
        if not agent_config:
            return
        channel = self._client.get_channel(int(agent_config.channel_id))
        if channel and isinstance(channel, discord.TextChannel):
            await channel.send(text)

    # --- Lifecycle ---

    async def run(self, token: str) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))
        try:
            await self._client.start(token)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("Shutting down...")
        if self._supervisor_task:
            self._supervisor_task.cancel()
        if self._idle_reaper_task:
            self._idle_reaper_task.cancel()
        if self._mcp_server_task:
            self._mcp_server_task.cancel()
        if hasattr(self, "_scheduler"):
            self._scheduler.stop()
        # Stop all agent processes in parallel
        if self._processes:
            await asyncio.gather(
                *(p.stop() for p in self._processes.values()),
                return_exceptions=True,
            )
        await self._client.close()
