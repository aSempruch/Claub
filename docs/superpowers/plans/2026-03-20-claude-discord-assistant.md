# Claude Code Discord Assistant — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Discord bot that bridges Discord channels to Claude Code CLI sessions, with a persistent main agent and schedulable sub-agents.

**Architecture:** A Python/uv Discord bot spawns Claude Code CLI processes with an isolated HOME directory. The main agent runs as a long-running stream-json process; sub-agents run as one-shot `claude -p` processes. All agents maintain session continuity via `--resume`. APScheduler handles cron-style task scheduling.

**Tech Stack:** Python 3.12+, discord.py, APScheduler, PyYAML, Claude Code CLI

---

## File Structure

```
claude-assistant/
  bot/
    pyproject.toml
    src/
      claude_assistant/
        __init__.py
        config.py          # Load and validate agents.yaml
        session.py          # Session ID persistence (JSON file)
        claude_process.py   # Spawn and communicate with claude CLI
        router.py           # Map Discord channels → agents
        scheduler.py        # APScheduler cron setup
        discord_bot.py      # Discord bot, message handling, commands
        chunker.py          # Split long messages for Discord
        main.py             # Entry point
    tests/
      __init__.py
      test_config.py
      test_session.py
      test_claude_process.py
      test_router.py
      test_scheduler.py
      test_chunker.py
  claude/
    home/
      .claude/
        settings.json
        agents/
    workspaces/
      main/
        CLAUDE.md
    config/
      agents.yaml
    data/
```

---

### Task 1: Project Scaffolding

**Files:**
- Create: `bot/pyproject.toml`
- Create: `bot/src/claude_assistant/__init__.py`
- Create: `bot/src/claude_assistant/main.py`
- Create: `bot/tests/__init__.py`
- Create: `claude/home/.claude/settings.json`
- Create: `claude/workspaces/main/CLAUDE.md`
- Create: `claude/config/agents.yaml`

- [ ] **Step 1: Create bot/pyproject.toml**

```toml
[project]
name = "claude-assistant"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "discord.py>=2.4",
    "apscheduler>=3.10,<4",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
]

[project.scripts]
claude-assistant = "claude_assistant.main:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.backends"
```

- [ ] **Step 2: Create package init and entry point**

`bot/src/claude_assistant/__init__.py` — empty file

`bot/src/claude_assistant/main.py`:
```python
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("claude_assistant")


def main() -> None:
    log.info("claude-assistant starting")
    log.info("scaffolding complete — bot not yet implemented")


if __name__ == "__main__":
    main()
```

`bot/tests/__init__.py` — empty file

- [ ] **Step 3: Create claude directory structure**

`claude/home/.claude/settings.json`:
```json
{}
```

`claude/workspaces/main/CLAUDE.md`:
```markdown
You are the main assistant. You help the user with any task they ask about.
```

`claude/config/agents.yaml`:
```yaml
discord:
  main_channel_id: ""

agents: {}
```

`claude/home/.claude/agents/` — create empty directory (add `.gitkeep`). Agent `.md` files will be added here as agents are defined.

Note: Agent workspace directories (e.g., `claude/workspaces/journalist/`) are created on demand by the bot. Example agent definition files and workspace CLAUDE.md files should be created as part of Task 11 (smoke test setup).

`claude/data/` — create empty directory (add `.gitkeep`)

- [ ] **Step 4: Verify scaffolding**

Run: `cd bot && uv sync && uv run claude-assistant`
Expected: Logs "scaffolding complete" and exits

- [ ] **Step 5: Commit**

```bash
git add bot/ claude/
git commit -m "feat: scaffold project structure"
```

---

### Task 2: Config Loading

**Files:**
- Create: `bot/src/claude_assistant/config.py`
- Create: `bot/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`bot/tests/test_config.py`:
```python
import pytest
from pathlib import Path
from claude_assistant.config import load_config, AssistantConfig


def test_load_minimal_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        'discord:\n  main_channel_id: "123"\nagents: {}\n'
    )
    config = load_config(cfg_file)
    assert config.main_channel_id == "123"
    assert config.agents == {}


def test_load_config_with_agent(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        'discord:\n  main_channel_id: "123"\n'
        "agents:\n"
        "  journalist:\n"
        '    channel_id: "456"\n'
        "    schedule:\n"
        '      - cron: "0 9 * * *"\n'
        '        prompt: "check news"\n'
    )
    config = load_config(cfg_file)
    assert "journalist" in config.agents
    agent = config.agents["journalist"]
    assert agent.channel_id == "456"
    assert len(agent.schedules) == 1
    assert agent.schedules[0].cron == "0 9 * * *"
    assert agent.schedules[0].prompt == "check news"


def test_load_config_missing_main_channel(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text("discord: {}\nagents: {}\n")
    with pytest.raises(ValueError, match="main_channel_id"):
        load_config(cfg_file)


def test_load_config_agent_missing_channel(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        'discord:\n  main_channel_id: "123"\n'
        "agents:\n  journalist:\n    schedule: []\n"
    )
    with pytest.raises(ValueError, match="channel_id"):
        load_config(cfg_file)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bot && uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`bot/src/claude_assistant/config.py`:
```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ScheduleEntry:
    cron: str
    prompt: str


@dataclass(frozen=True)
class AgentConfig:
    channel_id: str
    schedules: list[ScheduleEntry] = field(default_factory=list)


@dataclass(frozen=True)
class AssistantConfig:
    main_channel_id: str
    agents: dict[str, AgentConfig] = field(default_factory=dict)


def load_config(path: Path) -> AssistantConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    discord = raw.get("discord", {})
    main_channel_id = discord.get("main_channel_id")
    if not main_channel_id:
        raise ValueError("discord.main_channel_id is required")

    agents: dict[str, AgentConfig] = {}
    for name, agent_raw in (raw.get("agents") or {}).items():
        channel_id = (agent_raw or {}).get("channel_id")
        if not channel_id:
            raise ValueError(f"agents.{name}.channel_id is required")
        schedules = [
            ScheduleEntry(cron=s["cron"], prompt=s["prompt"])
            for s in (agent_raw.get("schedule") or [])
        ]
        agents[name] = AgentConfig(channel_id=channel_id, schedules=schedules)

    return AssistantConfig(main_channel_id=main_channel_id, agents=agents)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bot && uv run pytest tests/test_config.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/config.py bot/tests/test_config.py
git commit -m "feat: config loading from agents.yaml"
```

---

### Task 3: Session Persistence

**Files:**
- Create: `bot/src/claude_assistant/session.py`
- Create: `bot/tests/test_session.py`

- [ ] **Step 1: Write the failing test**

`bot/tests/test_session.py`:
```python
import pytest
from pathlib import Path
from claude_assistant.session import SessionStore


def test_get_nonexistent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    assert store.get("main") is None


def test_set_and_get(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set("main", "uuid-123")
    assert store.get("main") == "uuid-123"


def test_persistence_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store1 = SessionStore(path)
    store1.set("main", "uuid-123")

    store2 = SessionStore(path)
    assert store2.get("main") == "uuid-123"


def test_delete(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set("main", "uuid-123")
    store.delete("main")
    assert store.get("main") is None


def test_delete_nonexistent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.delete("main")  # should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bot && uv run pytest tests/test_session.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`bot/src/claude_assistant/session.py`:
```python
from __future__ import annotations

import json
import tempfile
from pathlib import Path


class SessionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, str] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text())

    def get(self, agent: str) -> str | None:
        return self._data.get(agent)

    def set(self, agent: str, session_id: str) -> None:
        self._data[agent] = session_id
        self._save()

    def delete(self, agent: str) -> None:
        self._data.pop(agent, None)
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", dir=self._path.parent, suffix=".tmp", delete=False
        )
        try:
            json.dump(self._data, tmp, indent=2)
            tmp.close()
            Path(tmp.name).replace(self._path)
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bot && uv run pytest tests/test_session.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/session.py bot/tests/test_session.py
git commit -m "feat: session ID persistence with atomic writes"
```

---

### Task 4: Message Chunker

**Files:**
- Create: `bot/src/claude_assistant/chunker.py`
- Create: `bot/tests/test_chunker.py`

- [ ] **Step 1: Write the failing test**

`bot/tests/test_chunker.py`:
```python
from claude_assistant.chunker import chunk_message

LIMIT = 2000


def test_short_message() -> None:
    assert chunk_message("hello", LIMIT) == ["hello"]


def test_exact_limit() -> None:
    msg = "a" * LIMIT
    assert chunk_message(msg, LIMIT) == [msg]


def test_splits_on_newline() -> None:
    line = "a" * 999
    msg = f"{line}\n{line}\n{line}"  # 3 lines, total > 2000
    chunks = chunk_message(msg, LIMIT)
    assert len(chunks) == 2
    for c in chunks:
        assert len(c) <= LIMIT


def test_long_line_force_split() -> None:
    msg = "a" * 3000  # single line longer than limit
    chunks = chunk_message(msg, LIMIT)
    assert len(chunks) == 2
    assert len(chunks[0]) == LIMIT
    assert len(chunks[1]) == 1000


def test_empty_message() -> None:
    assert chunk_message("", LIMIT) == [""]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bot && uv run pytest tests/test_chunker.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`bot/src/claude_assistant/chunker.py`:
```python
from __future__ import annotations


def chunk_message(text: str, limit: int = 2000) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # Try to split on a newline within the limit
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            # No newline found, force split at limit
            split_at = limit
        else:
            split_at += 1  # include the newline in current chunk

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    return chunks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bot && uv run pytest tests/test_chunker.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/chunker.py bot/tests/test_chunker.py
git commit -m "feat: Discord message chunker with newline-aware splitting"
```

---

### Task 5: Claude Process Manager — Main Agent

**Files:**
- Create: `bot/src/claude_assistant/claude_process.py`
- Create: `bot/tests/test_claude_process.py`

- [ ] **Step 1: Write the failing test**

`bot/tests/test_claude_process.py`:
```python
import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from claude_assistant.claude_process import MainAgentProcess, SubAgentRunner


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


class TestMainAgentProcess:
    @pytest.mark.asyncio
    async def test_build_command_first_run(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = MainAgentProcess(home_dir=home_dir, workspace=workspace)
        cmd = proc._build_command(session_id=None)
        assert "claude" in cmd
        assert "--input-format" in cmd
        assert "stream-json" in cmd
        assert "--resume" not in cmd

    @pytest.mark.asyncio
    async def test_build_command_resume(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = MainAgentProcess(home_dir=home_dir, workspace=workspace)
        cmd = proc._build_command(session_id="uuid-123")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "uuid-123"

    @pytest.mark.asyncio
    async def test_format_input_message(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = MainAgentProcess(home_dir=home_dir, workspace=workspace)
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
        proc = MainAgentProcess(home_dir=home_dir, workspace=workspace)
        event = {"type": "system", "subtype": "init", "session_id": "abc-123"}
        assert proc._parse_session_id(event) == "abc-123"

    @pytest.mark.asyncio
    async def test_parse_result_event(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = MainAgentProcess(home_dir=home_dir, workspace=workspace)
        event = {"type": "result", "subtype": "success", "result": "hello"}
        assert proc._is_result_event(event)
        assert proc._extract_result(event) == "hello"

    @pytest.mark.asyncio
    async def test_parse_non_result_event(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = MainAgentProcess(home_dir=home_dir, workspace=workspace)
        event = {"type": "assistant", "message": {}}
        assert not proc._is_result_event(event)


class TestSubAgentRunner:
    @pytest.mark.asyncio
    async def test_build_command_first_run(
        self, home_dir: Path, tmp_path: Path
    ) -> None:
        ws = tmp_path / "workspaces" / "journalist"
        ws.mkdir(parents=True)
        runner = SubAgentRunner(
            agent_name="journalist", home_dir=home_dir, workspace=ws
        )
        cmd = runner._build_command(session_id=None, prompt="check news")
        assert "--agent" in cmd
        idx = cmd.index("--agent")
        assert cmd[idx + 1] == "journalist"
        assert "--resume" not in cmd
        assert "check news" in cmd

    @pytest.mark.asyncio
    async def test_build_command_resume(
        self, home_dir: Path, tmp_path: Path
    ) -> None:
        ws = tmp_path / "workspaces" / "journalist"
        ws.mkdir(parents=True)
        runner = SubAgentRunner(
            agent_name="journalist", home_dir=home_dir, workspace=ws
        )
        cmd = runner._build_command(session_id="uuid-456", prompt="check news")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "uuid-456"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bot && uv run pytest tests/test_claude_process.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`bot/src/claude_assistant/claude_process.py`:
```python
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class MainAgentProcess:
    """Long-running Claude Code process with stream-json I/O."""

    def __init__(self, home_dir: Path, workspace: Path) -> None:
        self.home_dir = home_dir
        self.workspace = workspace
        self._process: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None

    def _build_command(self, session_id: str | None) -> list[str]:
        cmd = [
            "claude",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
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
        """Start the claude process. Returns the session_id from the init event."""
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
        # Read until we get the init event with session_id
        self._session_id = await self._read_until_init()
        return self._session_id

    async def _read_until_init(self) -> str | None:
        """Read events until we get the system/init event."""
        assert self._process and self._process.stdout
        while True:
            line = await asyncio.wait_for(
                self._process.stdout.readline(), timeout=30
            )
            if not line:
                return None
            try:
                event = json.loads(line.decode().strip())
            except json.JSONDecodeError:
                continue
            sid = self._parse_session_id(event)
            if sid:
                return sid

    async def send_message(self, content: str, timeout: float = 300) -> str:
        """Send a message and return the result. Timeout in seconds (default 5min)."""
        assert self._process and self._process.stdin and self._process.stdout
        sid = self._session_id or "default"
        msg = self._format_input(content, sid)
        self._process.stdin.write(msg.encode() + b"\n")
        await self._process.stdin.drain()

        # Read until result event (with timeout)
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
        self, agent_name: str, home_dir: Path, workspace: Path
    ) -> None:
        self.agent_name = agent_name
        self.home_dir = home_dir
        self.workspace = workspace

    def _build_command(
        self, session_id: str | None, prompt: str
    ) -> list[str]:
        cmd = [
            "claude", "-p",
            "--agent", self.agent_name,
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(prompt)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bot && uv run pytest tests/test_claude_process.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/claude_process.py bot/tests/test_claude_process.py
git commit -m "feat: Claude process manager for main and sub-agents"
```

---

### Task 6: Message Router

**Files:**
- Create: `bot/src/claude_assistant/router.py`
- Create: `bot/tests/test_router.py`

- [ ] **Step 1: Write the failing test**

`bot/tests/test_router.py`:
```python
from claude_assistant.config import AssistantConfig, AgentConfig
from claude_assistant.router import Router


def _config() -> AssistantConfig:
    return AssistantConfig(
        main_channel_id="100",
        agents={
            "journalist": AgentConfig(channel_id="200"),
            "researcher": AgentConfig(channel_id="300"),
        },
    )


def test_route_main_channel() -> None:
    router = Router(_config())
    assert router.route("100") == ("main", None)


def test_route_agent_channel() -> None:
    router = Router(_config())
    assert router.route("200") == ("agent", "journalist")
    assert router.route("300") == ("agent", "researcher")


def test_route_unknown_channel() -> None:
    router = Router(_config())
    assert router.route("999") == (None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bot && uv run pytest tests/test_router.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`bot/src/claude_assistant/router.py`:
```python
from __future__ import annotations

from claude_assistant.config import AssistantConfig


class Router:
    def __init__(self, config: AssistantConfig) -> None:
        self._main_channel = config.main_channel_id
        self._agent_channels: dict[str, str] = {}  # channel_id -> agent_name
        for name, agent in config.agents.items():
            self._agent_channels[agent.channel_id] = name

    def route(self, channel_id: str) -> tuple[str | None, str | None]:
        """Returns (route_type, agent_name).

        route_type is "main", "agent", or None (ignore).
        """
        if channel_id == self._main_channel:
            return ("main", None)
        agent = self._agent_channels.get(channel_id)
        if agent:
            return ("agent", agent)
        return (None, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bot && uv run pytest tests/test_router.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/router.py bot/tests/test_router.py
git commit -m "feat: message router mapping channels to agents"
```

---

### Task 7: Scheduler

**Files:**
- Create: `bot/src/claude_assistant/scheduler.py`
- Create: `bot/tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

`bot/tests/test_scheduler.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from claude_assistant.config import AssistantConfig, AgentConfig, ScheduleEntry
from claude_assistant.scheduler import Scheduler


def _config_with_schedule() -> AssistantConfig:
    return AssistantConfig(
        main_channel_id="100",
        agents={
            "journalist": AgentConfig(
                channel_id="200",
                schedules=[
                    ScheduleEntry(cron="0 9 * * *", prompt="check news"),
                    ScheduleEntry(cron="0 17 * * *", prompt="evening summary"),
                ],
            ),
        },
    )


def test_scheduler_registers_jobs() -> None:
    callback = AsyncMock()
    scheduler = Scheduler(_config_with_schedule(), callback)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 2


def test_scheduler_no_schedules() -> None:
    config = AssistantConfig(
        main_channel_id="100",
        agents={"journalist": AgentConfig(channel_id="200")},
    )
    callback = AsyncMock()
    scheduler = Scheduler(config, callback)
    assert len(scheduler.get_jobs()) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bot && uv run pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`bot/src/claude_assistant/scheduler.py`:
```python
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from claude_assistant.config import AssistantConfig

log = logging.getLogger(__name__)

# callback signature: (agent_name, prompt) -> None
ScheduleCallback = Callable[[str, str], Awaitable[None]]


class Scheduler:
    def __init__(
        self, config: AssistantConfig, callback: ScheduleCallback
    ) -> None:
        self._scheduler = AsyncIOScheduler()
        self._callback = callback

        for agent_name, agent_config in config.agents.items():
            for i, entry in enumerate(agent_config.schedules):
                self._scheduler.add_job(
                    self._run,
                    trigger=CronTrigger.from_crontab(entry.cron),
                    args=[agent_name, entry.prompt],
                    id=f"{agent_name}_schedule_{i}",
                    name=f"{agent_name}: {entry.prompt[:50]}",
                )

    async def _run(self, agent_name: str, prompt: str) -> None:
        log.info("Scheduled task firing for %s", agent_name)
        await self._callback(agent_name, prompt)

    def get_jobs(self) -> list:
        return self._scheduler.get_jobs()

    def start(self) -> None:
        self._scheduler.start()
        log.info("Scheduler started with %d jobs", len(self.get_jobs()))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bot && uv run pytest tests/test_scheduler.py -v`
Expected: All 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/scheduler.py bot/tests/test_scheduler.py
git commit -m "feat: APScheduler-based cron scheduler"
```

---

### Task 8: Discord Bot — Skeleton and Routing

**Files:**
- Create: `bot/src/claude_assistant/discord_bot.py`
- Create: `bot/tests/test_discord_bot.py`

This task creates the bot skeleton with routing, reset command, and tests. The full message handling is added in subsequent steps.

- [ ] **Step 1: Write the failing tests**

`bot/tests/test_discord_bot.py`:
```python
import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from claude_assistant.discord_bot import AssistantBot
from claude_assistant.config import AssistantConfig, AgentConfig, ScheduleEntry


@pytest.fixture
def config() -> AssistantConfig:
    return AssistantConfig(
        main_channel_id="100",
        agents={
            "journalist": AgentConfig(
                channel_id="200",
                schedules=[ScheduleEntry(cron="0 9 * * *", prompt="news")],
            ),
        },
    )


@pytest.fixture
def bot(config: AssistantConfig, tmp_path: Path) -> AssistantBot:
    return AssistantBot(
        config=config,
        home_dir=tmp_path / "home",
        workspaces_dir=tmp_path / "workspaces",
        session_store=MagicMock(),
    )


def test_bot_creates_router(bot: AssistantBot) -> None:
    assert bot.router.route("100") == ("main", None)
    assert bot.router.route("200") == ("agent", "journalist")
    assert bot.router.route("999") == (None, None)


def test_bot_creates_agent_locks(bot: AssistantBot) -> None:
    assert "journalist" in bot._agent_locks
    assert isinstance(bot._agent_locks["journalist"], asyncio.Lock)


@pytest.mark.asyncio
async def test_handle_reset_main(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 100
    msg.channel.send = AsyncMock()
    msg.content = "/reset"
    bot._main_process = MagicMock()
    bot._main_process.stop = AsyncMock()
    # Patch _start_main_agent to avoid spawning real process
    bot._start_main_agent = AsyncMock()
    await bot._handle_reset(msg, "/reset")
    bot.sessions.delete.assert_called_with("main")
    bot._start_main_agent.assert_called_once()


@pytest.mark.asyncio
async def test_handle_reset_agent(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 200
    msg.channel.send = AsyncMock()
    msg.content = "/reset journalist"
    await bot._handle_reset(msg, "/reset journalist")
    bot.sessions.delete.assert_called_with("journalist")


@pytest.mark.asyncio
async def test_handle_reset_unknown_agent(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 200
    msg.channel.send = AsyncMock()
    await bot._handle_reset(msg, "/reset nonexistent")
    msg.channel.send.assert_called_with("Unknown agent: nonexistent")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd bot && uv run pytest tests/test_discord_bot.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write bot skeleton**

`bot/src/claude_assistant/discord_bot.py`:
```python
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
    ) -> None:
        self.config = config
        self.home_dir = home_dir
        self.workspaces_dir = workspaces_dir
        self.sessions = session_store
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
            home_dir=self.home_dir, workspace=workspace
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bot && uv run pytest tests/test_discord_bot.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/discord_bot.py bot/tests/test_discord_bot.py
git commit -m "feat: Discord bot with routing, supervisor loop, signal handling"
```

---

### Task 9: Entry Point and Wiring

**Files:**
- Modify: `bot/src/claude_assistant/main.py`

- [ ] **Step 1: Update main.py**

`bot/src/claude_assistant/main.py`:
```python
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from claude_assistant.config import load_config
from claude_assistant.discord_bot import AssistantBot
from claude_assistant.session import SessionStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("claude_assistant")


def _resolve_paths() -> tuple[Path, Path, Path, Path]:
    """Resolve project paths relative to this file or env vars."""
    project_root = Path(os.environ.get(
        "CLAUDE_ASSISTANT_ROOT",
        Path(__file__).resolve().parents[3],  # bot/src/claude_assistant -> project root
    ))
    claude_dir = project_root / "claude"
    return (
        claude_dir / "config" / "agents.yaml",
        claude_dir / "home",
        claude_dir / "workspaces",
        claude_dir / "data" / "sessions.json",
    )


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        log.error("DISCORD_BOT_TOKEN environment variable is required")
        sys.exit(1)

    config_path, home_dir, workspaces_dir, sessions_path = _resolve_paths()

    if not config_path.exists():
        log.error("Config not found: %s", config_path)
        sys.exit(1)

    config = load_config(config_path)
    sessions = SessionStore(sessions_path)

    bot = AssistantBot(
        config=config,
        home_dir=home_dir,
        workspaces_dir=workspaces_dir,
        session_store=sessions,
    )

    log.info("Starting claude-assistant")
    asyncio.run(bot.run(token))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify import chain works**

Run: `cd bot && uv run python -c "from claude_assistant.main import main; print('imports ok')"`
Expected: "imports ok"

- [ ] **Step 3: Commit**

```bash
git add bot/src/claude_assistant/main.py
git commit -m "feat: wire up entry point with config, sessions, and bot"
```

---

### Task 10: Integration Test with Real Claude CLI

**Files:**
- Create: `bot/tests/test_integration.py`

This test runs against the real Claude CLI to verify the stream-json protocol works end-to-end. It is skipped in CI (no Claude auth).

- [ ] **Step 1: Write integration test**

`bot/tests/test_integration.py`:
```python
"""Integration tests that run against the real Claude CLI.

Skipped unless CLAUDE_INTEGRATION_TEST=1 is set.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from claude_assistant.claude_process import MainAgentProcess, SubAgentRunner

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


class TestMainAgentIntegration:
    @pytest.mark.asyncio
    async def test_start_send_stop(
        self, home_dir: Path, workspace: Path
    ) -> None:
        proc = MainAgentProcess(home_dir=home_dir, workspace=workspace)
        sid = await proc.start()
        assert sid is not None

        result = await proc.send_message(
            "say the word hello and nothing else"
        )
        assert "hello" in result.lower()

        await proc.stop()
        assert not proc.is_alive


class TestSubAgentIntegration:
    @pytest.mark.asyncio
    async def test_run_and_resume(
        self, home_dir: Path, workspace: Path
    ) -> None:
        # Would need an agent defined in home_dir/.claude/agents/
        # Skip for now unless agent exists
        pytest.skip("Requires agent definition setup")
```

- [ ] **Step 2: Run unit tests to make sure nothing broke**

Run: `cd bot && uv run pytest tests/ -v --ignore=tests/test_integration.py`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add bot/tests/test_integration.py
git commit -m "feat: integration test scaffolding for real Claude CLI"
```

---

### Task 11: End-to-End Smoke Test

- [ ] **Step 1: Set up Claude auth in isolated home**

Run:
```bash
cd claude-assistant
HOME=claude/home claude auth login
```

Follow the auth flow. Verify with:
```bash
HOME=claude/home claude -p --no-session-persistence "say hello"
```

- [ ] **Step 2: Create a test Discord bot**

1. Go to Discord Developer Portal
2. Create a new application
3. Create a bot user, copy the token
4. Enable `MESSAGE CONTENT` intent
5. Invite to a test server with Send Messages + Read Message History permissions

- [ ] **Step 3: Configure agents.yaml with real channel IDs**

Update `claude/config/agents.yaml` with real Discord channel IDs from your test server.

- [ ] **Step 4: Run the bot**

```bash
cd bot
DISCORD_BOT_TOKEN="your-token" uv run claude-assistant
```

- [ ] **Step 5: Test in Discord**

1. Send a message in `#main` — verify response
2. Send `/reset` in `#main` — verify reset confirmation
3. Verify session persistence: restart bot, send follow-up message, confirm context preserved

- [ ] **Step 6: Commit any fixes**

```bash
git add -A
git commit -m "fix: adjustments from smoke testing"
```
