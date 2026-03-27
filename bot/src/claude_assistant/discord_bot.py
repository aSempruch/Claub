from __future__ import annotations

import asyncio
import logging
import signal
import time
from pathlib import Path

import discord

from claude_assistant.chunker import chunk_message
from claude_assistant.claude_process import AgentProcess, AuthenticationError
from claude_assistant.config import AssistantConfig
from claude_assistant.file_sender import extract_files
from claude_assistant.mcp_server import create_mcp_server
from claude_assistant.router import Router
from claude_assistant.schedule_store import ScheduleStore
from claude_assistant.scheduler import Scheduler
from claude_assistant.session import SessionStore

log = logging.getLogger(__name__)


class AssistantBot:
    def __init__(
        self,
        config: AssistantConfig,
        workspaces_dir: Path,
        session_store: SessionStore,
        schedule_store: ScheduleStore,
        mcp_config: Path | None = None,
        agents_dir: Path | None = None,
        mcp_port: int = 9400,
        all_skills: list[str] | None = None,
    ) -> None:
        self.config = config
        self.workspaces_dir = workspaces_dir
        self.sessions = session_store
        self.schedule_store = schedule_store
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
        self._agent_lock = asyncio.Lock()  # serialize all agent API calls
        self._last_activity: dict[str, float] = {}  # agent name -> timestamp
        self._reaped: set[str] = set()  # agents intentionally killed by idle reaper
        self._idle_timeout = 600  # kill idle processes after 10 minutes

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._setup_events()

    def _setup_events(self) -> None:
        @self._client.event
        async def on_ready() -> None:
            log.info("Discord connected as %s", self._client.user)
            self._supervisor_task = asyncio.create_task(self._supervise_all())
            self._idle_reaper_task = asyncio.create_task(self._reap_idle_processes())
            self._scheduler = Scheduler(self.schedule_store, self._handle_scheduled, valid_agents=set(self.config.agents.keys()))
            self._scheduler.start()
            self._mcp_server_task = asyncio.create_task(self._start_mcp_server())

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
        allowed = set(agent_config.allowed_skills) if agent_config else set()
        return [s for s in self.all_skills if s not in allowed]

    async def _start_agent(self, name: str) -> AgentProcess:
        """Start (or restart) an agent process. Returns the new process."""
        workspace = self.workspaces_dir / name
        workspace.mkdir(parents=True, exist_ok=True)
        agent_config = self.config.agents.get(name)
        process = AgentProcess(
            workspace=workspace,
            mcp_configs=self._mcp_configs_for(name),
            agent_name=name,
            allowed_tools_additional=agent_config.allowed_tools_additional if agent_config else [],
            model=self.config.model,
            sibling_agent_names=list(self.config.agents.keys()),
            disallowed_skills=self._disallowed_skills_for(name),
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
        """Background task that monitors all agent processes and restarts dead ones."""
        try:
            while True:
                await asyncio.sleep(5)
                for name, process in list(self._processes.items()):
                    if not process.is_alive and name not in self._reaped:
                        log.warning("Agent %s died, restarting...", name)
                        await self._notify_channel(name, f"Agent `{name}` died, restarting...")
                        try:
                            await self._start_agent(name)
                        except Exception:
                            log.exception("Failed to restart agent %s", name)
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
                    if last and idle_secs > self._idle_timeout:
                        log.info(
                            "Reaping idle agent %s (idle %.0fs)", name, idle_secs
                        )
                        self._reaped.add(name)
                        await process.stop()
                        del self._processes[name]
        except asyncio.CancelledError:
            log.info("Idle reaper cancelled")
            return

    async def _start_mcp_server(self) -> None:
        import uvicorn

        mcp = create_mcp_server(self.schedule_store, self._scheduler, notify=self._notify_channel)
        app = mcp.http_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.mcp_port, log_level="warning")
        self._uvicorn_server = uvicorn.Server(config)
        log.info("Starting MCP server on 127.0.0.1:%d", self.mcp_port)
        await self._uvicorn_server.serve()

    # --- Message handling ---

    async def _handle_message(self, message: discord.Message) -> None:
        channel_id = str(message.channel.id)
        content = message.content.strip()

        if content.startswith("/clear"):
            await self._handle_reset(message, content)
            return

        agent_name = self.router.route(channel_id)
        if agent_name is None:
            return

        await self._handle_agent_message(message, agent_name, content)

    async def _handle_agent_message(
        self, message: discord.Message, agent_name: str, content: str
    ) -> None:
        async with message.channel.typing():
            try:
                result = await self._send_with_restart(agent_name, content)
            except AuthenticationError:
                log.error("Claude authentication failed for %s", agent_name)
                await message.channel.send(
                    "Claude authentication expired. Re-authenticate with `claude` on the host and restart."
                )
                return

            process = self._processes.get(agent_name)
            if process and process.session_id:
                self.sessions.set(agent_name, process.session_id)

        await self._send_chunked(message.channel, agent_name, result)

    async def _send_with_restart(self, agent_name: str, content: str) -> str:
        """Send a message to an agent, restarting the process on failure.

        Serialized via _agent_lock to prevent concurrent API calls that
        race on OAuth token refresh (all agents share one credentials file).
        """
        if self._agent_lock.locked():
            log.info("Agent %s waiting for agent lock", agent_name)
        async with self._agent_lock:
            self._reaped.discard(agent_name)
            self._last_activity[agent_name] = time.monotonic()
            process = await self._get_or_start_process(agent_name)
            try:
                result = await process.send_message(content)
            except AuthenticationError:
                raise
            except RuntimeError:
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

        if result.strip().startswith("[NO_POST]"):
            log.info("Agent %s opted out of posting", agent_name)
            return

        process = self._processes.get(agent_name)
        if process and process.session_id:
            self.sessions.set(agent_name, process.session_id)

        await self._send_chunked(channel, agent_name, result)

    # --- Commands ---

    async def _handle_reset(self, message: discord.Message, content: str) -> None:
        parts = content.split()
        channel_id = str(message.channel.id)

        if len(parts) >= 2:
            agent_name = parts[1]
        else:
            agent_name = self.router.route(channel_id)
            if agent_name is None:
                return

        if agent_name not in self.config.agents:
            await message.channel.send(f"Unknown agent: {agent_name}")
            return

        process = self._processes.get(agent_name)
        if process:
            await process.stop()
            del self._processes[agent_name]
        self._last_activity.pop(agent_name, None)
        self._reaped.discard(agent_name)
        self.sessions.delete(agent_name)
        await message.channel.send(f"Agent `{agent_name}` reset.")

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
        if hasattr(self, "_uvicorn_server"):
            self._uvicorn_server.should_exit = True
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
