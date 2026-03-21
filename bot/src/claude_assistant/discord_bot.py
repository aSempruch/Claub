from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

import discord

from claude_assistant.chunker import chunk_message
from claude_assistant.claude_process import MainAgentProcess, SubAgentRunner
from claude_assistant.config import AssistantConfig
from claude_assistant.router import Router
from claude_assistant.scheduler import Scheduler
from claude_assistant.session import SessionStore

log = logging.getLogger(__name__)


class AssistantBot:
    def __init__(
        self,
        config: AssistantConfig,
        home_dir: Path,
        workspaces_dir: Path,
        session_store: SessionStore,
        mcp_config: Path | None = None,
    ) -> None:
        self.config = config
        self.home_dir = home_dir
        self.workspaces_dir = workspaces_dir
        self.sessions = session_store
        self.mcp_config = mcp_config
        self.router = Router(config)

        self._agent_locks: dict[str, asyncio.Lock] = {
            name: asyncio.Lock() for name in config.agents
        }
        self._main_process: MainAgentProcess | None = None
        self._supervisor_task: asyncio.Task | None = None
        self._shutting_down = False

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._setup_events()

    def _setup_events(self) -> None:
        @self._client.event
        async def on_ready() -> None:
            log.info("Discord connected as %s", self._client.user)
            await self._start_main_agent()
            self._supervisor_task = asyncio.create_task(self._supervise_main())
            self._scheduler = Scheduler(self.config, self._handle_scheduled)
            self._scheduler.start()

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            if message.author == self._client.user or message.author.bot:
                return
            await self._handle_message(message)

    # --- Main agent lifecycle ---

    async def _start_main_agent(self) -> None:
        workspace = self.workspaces_dir / "main"
        workspace.mkdir(parents=True, exist_ok=True)
        self._main_process = MainAgentProcess(
            home_dir=self.home_dir, workspace=workspace, mcp_config=self.mcp_config
        )
        session_id = self.sessions.get("main")
        try:
            new_sid = await self._main_process.start(session_id)
            if new_sid:
                self.sessions.set("main", new_sid)
        except Exception:
            log.exception("Failed to start main agent")
            if session_id:
                log.info("Retrying without --resume")
                await self._notify_main("Lost previous context, starting fresh.")
                self.sessions.delete("main")
                new_sid = await self._main_process.start(None)
                if new_sid:
                    self.sessions.set("main", new_sid)

    async def _supervise_main(self) -> None:
        """Background task that monitors the main agent process and restarts it."""
        try:
            while True:
                await asyncio.sleep(5)
                if self._main_process and not self._main_process.is_alive:
                    log.warning("Main agent process died, restarting...")
                    await self._notify_main("Main agent process died, restarting...")
                    try:
                        await self._start_main_agent()
                    except Exception:
                        log.exception("Failed to restart main agent")
        except asyncio.CancelledError:
            log.info("Supervisor loop cancelled")
            return

    # --- Message handling ---

    async def _handle_message(self, message: discord.Message) -> None:
        channel_id = str(message.channel.id)
        content = message.content.strip()

        if content.startswith("/reset"):
            await self._handle_reset(message, content)
            return

        route_type, agent_name = self.router.route(channel_id)
        if route_type is None:
            return

        if route_type == "main":
            await self._handle_main_message(message, content)
        elif route_type == "agent" and agent_name:
            await self._handle_agent_message(message, agent_name, content)

    async def _handle_main_message(
        self, message: discord.Message, content: str
    ) -> None:
        if not self._main_process or not self._main_process.is_alive:
            await self._start_main_agent()
        assert self._main_process

        async with message.channel.typing():
            try:
                result = await self._main_process.send_message(content)
            except RuntimeError:
                log.exception("Main agent error, restarting")
                await message.channel.send("Main agent crashed, restarting...")
                await self._start_main_agent()
                assert self._main_process
                result = await self._main_process.send_message(content)

        for chunk in chunk_message(result):
            await message.channel.send(chunk)

    async def _handle_agent_message(
        self, message: discord.Message, agent_name: str, content: str
    ) -> None:
        lock = self._agent_locks.get(agent_name)
        if not lock:
            return

        workspace = self.workspaces_dir / agent_name
        workspace.mkdir(parents=True, exist_ok=True)
        runner = SubAgentRunner(
            agent_name=agent_name,
            home_dir=self.home_dir,
            workspace=workspace,
            mcp_config=self.mcp_config,
        )

        async with message.channel.typing():
            async with lock:
                session_id = self.sessions.get(agent_name)
                try:
                    result, new_sid = await runner.run(content, session_id)
                    self.sessions.set(agent_name, new_sid)
                except RuntimeError:
                    if session_id:
                        log.warning("Agent %s resume failed, retrying fresh", agent_name)
                        await message.channel.send("Lost previous context, starting fresh.")
                        self.sessions.delete(agent_name)
                        result, new_sid = await runner.run(content, None)
                        self.sessions.set(agent_name, new_sid)
                    else:
                        raise

        for chunk in chunk_message(result):
            await message.channel.send(chunk)

    # --- Scheduled tasks ---

    async def _handle_scheduled(self, agent_name: str, prompt: str) -> None:
        agent_config = self.config.agents.get(agent_name)
        if not agent_config:
            return

        channel = self._client.get_channel(int(agent_config.channel_id))
        if not channel or not isinstance(channel, discord.TextChannel):
            log.error("Channel %s not found for agent %s", agent_config.channel_id, agent_name)
            return

        lock = self._agent_locks.get(agent_name)
        if not lock:
            return

        workspace = self.workspaces_dir / agent_name
        workspace.mkdir(parents=True, exist_ok=True)
        runner = SubAgentRunner(
            agent_name=agent_name,
            home_dir=self.home_dir,
            workspace=workspace,
            mcp_config=self.mcp_config,
        )

        async with lock:
            session_id = self.sessions.get(agent_name)
            try:
                result, new_sid = await runner.run(prompt, session_id)
                self.sessions.set(agent_name, new_sid)
            except RuntimeError as e:
                if session_id:
                    self.sessions.delete(agent_name)
                    try:
                        result, new_sid = await runner.run(prompt, None)
                        self.sessions.set(agent_name, new_sid)
                    except Exception as e2:
                        await channel.send(f"Scheduled task failed: {e2}")
                        return
                else:
                    await channel.send(f"Scheduled task failed: {e}")
                    return

        for chunk in chunk_message(result):
            await channel.send(chunk)

    # --- Commands ---

    async def _handle_reset(self, message: discord.Message, content: str) -> None:
        parts = content.split()
        if len(parts) == 1:
            channel_id = str(message.channel.id)
            if channel_id != self.config.main_channel_id:
                await message.channel.send(
                    "Use /reset in the main channel, or /reset <agent> to reset a sub-agent."
                )
                return
            if self._main_process:
                await self._main_process.stop()
            self.sessions.delete("main")
            await self._start_main_agent()
            await message.channel.send("Main agent reset.")
        else:
            agent_name = parts[1]
            if agent_name not in self.config.agents:
                await message.channel.send(f"Unknown agent: {agent_name}")
                return
            self.sessions.delete(agent_name)
            await message.channel.send(
                f"Agent `{agent_name}` session cleared. Next message starts fresh."
            )

    # --- Utilities ---

    async def _notify_main(self, text: str) -> None:
        channel = self._client.get_channel(int(self.config.main_channel_id))
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
        if hasattr(self, "_scheduler"):
            self._scheduler.stop()
        # Wait for in-flight agent invocations (with timeout)
        for name, lock in self._agent_locks.items():
            if lock.locked():
                log.info("Waiting for agent %s to finish...", name)
                try:
                    async with asyncio.timeout(60):
                        async with lock:
                            pass
                except TimeoutError:
                    log.warning("Timed out waiting for agent %s", name)
        if self._main_process:
            await self._main_process.stop()
        await self._client.close()
