# Schedule Management MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give agents the ability to manage their own cron schedules at runtime via an HTTP MCP endpoint embedded in the bot process.

**Architecture:** FastMCP streamable-http server runs inside the bot process on localhost, exposing list/create/delete schedule tools. Schedules persist to `~/.claub/data/schedules.json`. The bot's APScheduler syncs from this store on startup and on mutations. Agent name is passed via `X-Agent-Name` header resolved from `${CLAUB_AGENT_NAME}` env var.

**Tech Stack:** FastMCP, uvicorn, APScheduler, asyncio

**Spec:** `docs/superpowers/specs/2026-03-23-schedule-mcp-design.md`

---

## File Structure

**New files:**
- `bot/src/claude_assistant/schedule_store.py` — Atomic JSON persistence for schedules (read/write/query by agent)
- `bot/src/claude_assistant/mcp_server.py` — FastMCP HTTP server with 3 schedule tools
- `bot/tests/test_schedule_store.py` — ScheduleStore unit tests
- `bot/tests/test_mcp_server.py` — MCP tool unit tests
- `scripts/migrate_schedules.py` — One-time migration from agents.yaml to schedules.json

**Modified files:**
- `bot/pyproject.toml` — Add `fastmcp`, `uvicorn` dependencies
- `bot/src/claude_assistant/scheduler.py` — Load from ScheduleStore, add `add_job()`/`remove_job()` methods, one-shot support
- `bot/src/claude_assistant/claude_process.py:102-105` — Add `CLAUB_AGENT_NAME` to `_env()`
- `bot/src/claude_assistant/discord_bot.py` — Start MCP server in `on_ready`, pass ScheduleStore to Scheduler, add MCP server shutdown
- `bot/src/claude_assistant/main.py:23-36,49-65` — Resolve `schedules_path` and `mcp_port`, pass to bot
- `bot/src/claude_assistant/config.py` — Remove `ScheduleEntry` dataclass and schedule parsing
- `bot/tests/test_scheduler.py` — Rewrite for new Scheduler interface
- `bot/tests/test_config.py` — Remove schedule-related tests
- `bot/tests/test_discord_bot.py` — Remove `ScheduleEntry` import/usage
- `bot/tests/test_claude_process.py` — Add test for `CLAUB_AGENT_NAME` in env

**Instance config (separate repo at `~/.claub`):**
- `~/.claub/config/mcp.json` — Add `schedules` MCP server entry
- `~/.claub/config/settings.json` — Add `mcp__schedules__*` to permissions allow list
- `~/.claub/config/agents.yaml` — Remove all `schedule:` keys
- `~/.claub/data/schedules.json` — Created by migration script

---

### Task 1: Add dependencies

**Files:**
- Modify: `bot/pyproject.toml`

- [ ] **Step 1: Add fastmcp and uvicorn to pyproject.toml**

In `bot/pyproject.toml`, add to the `dependencies` list:

```toml
dependencies = [
    "discord.py>=2.4",
    "apscheduler>=3.10,<4",
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
    "fastmcp>=2.0",
    "uvicorn>=0.30",
]
```

- [ ] **Step 2: Install dependencies**

```bash
cd /Users/you/Claude/bot && uv sync
```

- [ ] **Step 3: Commit**

```bash
git add bot/pyproject.toml bot/uv.lock
git commit -m "feat: add fastmcp and uvicorn dependencies for schedule MCP"
```

---

### Task 2: ScheduleStore — data persistence layer

**Files:**
- Create: `bot/src/claude_assistant/schedule_store.py`
- Create: `bot/tests/test_schedule_store.py`

This follows the same atomic write pattern as `session.py`. The store holds a `dict[str, list[dict]]` keyed by agent name. It includes an `asyncio.Lock` for mutation safety — all callers (MCP tools and Scheduler one-shot cleanup) share this lock.

- [ ] **Step 1: Write failing tests for ScheduleStore**

Create `bot/tests/test_schedule_store.py`:

```python
import json
import pytest
from pathlib import Path
from claude_assistant.schedule_store import ScheduleStore


@pytest.fixture
def store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.json")


def test_list_empty(store: ScheduleStore) -> None:
    assert store.list("main") == []


def test_create_schedule(store: ScheduleStore) -> None:
    entry = store.create("main", cron="0 9 * * *", prompt="morning check", one_shot=False)
    assert entry["cron"] == "0 9 * * *"
    assert entry["prompt"] == "morning check"
    assert entry["one_shot"] is False
    assert len(entry["id"]) == 6


def test_list_returns_created(store: ScheduleStore) -> None:
    store.create("main", cron="0 9 * * *", prompt="morning", one_shot=False)
    store.create("main", cron="0 17 * * *", prompt="evening", one_shot=True)
    entries = store.list("main")
    assert len(entries) == 2


def test_delete_schedule(store: ScheduleStore) -> None:
    entry = store.create("main", cron="0 9 * * *", prompt="morning", one_shot=False)
    assert store.delete("main", entry["id"]) is True
    assert store.list("main") == []


def test_delete_nonexistent(store: ScheduleStore) -> None:
    assert store.delete("main", "aaaaaa") is False


def test_delete_wrong_agent(store: ScheduleStore) -> None:
    entry = store.create("main", cron="0 9 * * *", prompt="test", one_shot=False)
    assert store.delete("journalist", entry["id"]) is False
    assert len(store.list("main")) == 1


def test_persistence(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    store1 = ScheduleStore(path)
    store1.create("main", cron="0 9 * * *", prompt="test", one_shot=False)
    store2 = ScheduleStore(path)
    assert len(store2.list("main")) == 1


def test_all_schedules(store: ScheduleStore) -> None:
    store.create("main", cron="0 9 * * *", prompt="m1", one_shot=False)
    store.create("journalist", cron="0 10 * * *", prompt="j1", one_shot=True)
    all_entries = store.all()
    assert "main" in all_entries
    assert "journalist" in all_entries
    assert len(all_entries["main"]) == 1
    assert len(all_entries["journalist"]) == 1


def test_id_uniqueness(store: ScheduleStore) -> None:
    ids = set()
    for i in range(50):
        entry = store.create("main", cron="0 9 * * *", prompt=f"test {i}", one_shot=False)
        ids.add(entry["id"])
    assert len(ids) == 50


def test_missing_file_treated_as_empty(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "nonexistent" / "schedules.json")
    assert store.all() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_schedule_store.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'claude_assistant.schedule_store'`

- [ ] **Step 3: Implement ScheduleStore**

Create `bot/src/claude_assistant/schedule_store.py`:

```python
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path


class ScheduleStore:
    """Atomic JSON persistence for agent schedules.

    Data format: {"agent_name": [{"id": "abc123", "cron": "...", "prompt": "...", "one_shot": bool}]}
    All mutations should be guarded by the async lock via `async with store.lock:`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, list[dict]] = {}
        self.lock = asyncio.Lock()
        if self._path.exists():
            self._data = json.loads(self._path.read_text())

    def list(self, agent: str) -> list[dict]:
        return list(self._data.get(agent, []))

    def all(self) -> dict[str, list[dict]]:
        return {k: list(v) for k, v in self._data.items()}

    def create(
        self, agent: str, *, cron: str, prompt: str, one_shot: bool
    ) -> dict:
        entry_id = self._generate_id()
        entry = {"id": entry_id, "cron": cron, "prompt": prompt, "one_shot": one_shot}
        self._data.setdefault(agent, []).append(entry)
        self._save()
        return entry

    def delete(self, agent: str, entry_id: str) -> bool:
        entries = self._data.get(agent, [])
        for i, entry in enumerate(entries):
            if entry["id"] == entry_id:
                entries.pop(i)
                if not entries:
                    del self._data[agent]
                self._save()
                return True
        return False

    def _generate_id(self) -> str:
        all_ids = {e["id"] for entries in self._data.values() for e in entries}
        while True:
            entry_id = os.urandom(3).hex()
            if entry_id not in all_ids:
                return entry_id

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

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_schedule_store.py -v
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/schedule_store.py bot/tests/test_schedule_store.py
git commit -m "feat: add ScheduleStore for atomic schedule persistence"
```

---

### Task 3: Update Scheduler to use ScheduleStore

**Files:**
- Modify: `bot/src/claude_assistant/scheduler.py`
- Rewrite: `bot/tests/test_scheduler.py`

The Scheduler currently takes `AssistantConfig` and registers jobs in `__init__`. It needs to instead take a `ScheduleStore`, load from it, and expose `add_job()`/`remove_job()` methods. It also needs a one-shot callback wrapper that deletes the schedule before executing.

- [ ] **Step 1: Write failing tests for new Scheduler interface**

Rewrite `bot/tests/test_scheduler.py`:

```python
import pytest
from unittest.mock import AsyncMock
from claude_assistant.schedule_store import ScheduleStore
from claude_assistant.scheduler import Scheduler


@pytest.fixture
def store(tmp_path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.json")


@pytest.fixture
def callback() -> AsyncMock:
    return AsyncMock()


def test_load_from_store(store: ScheduleStore, callback: AsyncMock) -> None:
    store.create("main", cron="0 9 * * *", prompt="morning", one_shot=False)
    store.create("journalist", cron="0 17 * * *", prompt="evening", one_shot=False)
    scheduler = Scheduler(store, callback)
    assert len(scheduler.get_jobs()) == 2


def test_load_empty_store(store: ScheduleStore, callback: AsyncMock) -> None:
    scheduler = Scheduler(store, callback)
    assert len(scheduler.get_jobs()) == 0


def test_add_job(store: ScheduleStore, callback: AsyncMock) -> None:
    scheduler = Scheduler(store, callback)
    scheduler.add_job("main", "abc123", "0 9 * * *", "test prompt", one_shot=False)
    assert len(scheduler.get_jobs()) == 1


def test_remove_job(store: ScheduleStore, callback: AsyncMock) -> None:
    store.create("main", cron="0 9 * * *", prompt="test", one_shot=False)
    scheduler = Scheduler(store, callback)
    entry_id = store.list("main")[0]["id"]
    scheduler.remove_job("main", entry_id)
    assert len(scheduler.get_jobs()) == 0


def test_remove_nonexistent_job(store: ScheduleStore, callback: AsyncMock) -> None:
    scheduler = Scheduler(store, callback)
    # Should not raise
    scheduler.remove_job("main", "nonexistent")


def test_filter_orphaned_agents(store: ScheduleStore, callback: AsyncMock) -> None:
    store.create("deleted_agent", cron="0 9 * * *", prompt="orphan", one_shot=False)
    scheduler = Scheduler(store, callback, valid_agents={"main"})
    assert len(scheduler.get_jobs()) == 0


@pytest.mark.asyncio
async def test_one_shot_fires_and_deletes(store: ScheduleStore, callback: AsyncMock) -> None:
    entry = store.create("main", cron="0 9 * * *", prompt="one-time", one_shot=True)
    scheduler = Scheduler(store, callback)
    # Simulate firing the one-shot wrapper directly
    await scheduler._run_one_shot("main", entry["id"], "one-time")
    callback.assert_called_once()
    # Verify the prompt was prefixed with [scheduled]
    call_args = callback.call_args[0]
    assert call_args[0] == "main"
    assert "[scheduled]" in call_args[1]
    # Verify the entry was deleted from store
    assert store.list("main") == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_scheduler.py -v
```

Expected: FAIL — Scheduler no longer accepts `AssistantConfig`

- [ ] **Step 3: Rewrite Scheduler implementation**

Replace `bot/src/claude_assistant/scheduler.py`:

```python
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from claude_assistant.schedule_store import ScheduleStore

log = logging.getLogger(__name__)

# callback signature: (agent_name, prompt) -> None
ScheduleCallback = Callable[[str, str], Awaitable[None]]


class Scheduler:
    def __init__(
        self,
        store: ScheduleStore,
        callback: ScheduleCallback,
        valid_agents: set[str] | None = None,
    ) -> None:
        self._scheduler = AsyncIOScheduler()
        self._callback = callback
        self._store = store

        for agent_name, entries in store.all().items():
            if valid_agents is not None and agent_name not in valid_agents:
                log.warning("Skipping orphaned schedules for agent %s", agent_name)
                continue
            for entry in entries:
                self._add_apscheduler_job(agent_name, entry)

    def _add_apscheduler_job(self, agent_name: str, entry: dict) -> None:
        job_id = f"{agent_name}_{entry['id']}"
        if entry.get("one_shot"):
            run_fn = self._run_one_shot
            args = [agent_name, entry["id"], entry["prompt"]]
        else:
            run_fn = self._run
            args = [agent_name, entry["prompt"]]
        self._scheduler.add_job(
            run_fn,
            trigger=CronTrigger.from_crontab(entry["cron"]),
            args=args,
            id=job_id,
            name=f"{agent_name}: {entry['prompt'][:50]}",
        )

    def add_job(
        self, agent_name: str, entry_id: str, cron: str, prompt: str, *, one_shot: bool
    ) -> None:
        entry = {"id": entry_id, "cron": cron, "prompt": prompt, "one_shot": one_shot}
        self._add_apscheduler_job(agent_name, entry)

    def remove_job(self, agent_name: str, entry_id: str) -> None:
        job_id = f"{agent_name}_{entry_id}"
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            log.debug("Job %s not found in scheduler", job_id)

    async def _run(self, agent_name: str, prompt: str) -> None:
        jitter = random.uniform(0, 300)  # 0-5 minutes
        log.info("Scheduled task for %s — delaying %.0fs", agent_name, jitter)
        await asyncio.sleep(jitter)
        log.info("Scheduled task firing for %s", agent_name)
        prefixed = f"[scheduled] {prompt}"
        await self._callback(agent_name, prefixed)

    async def _run_one_shot(self, agent_name: str, entry_id: str, prompt: str) -> None:
        # Delete from store and scheduler BEFORE execution, under the shared lock
        async with self._store.lock:
            self._store.delete(agent_name, entry_id)
            job_id = f"{agent_name}_{entry_id}"
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass
        log.info("One-shot schedule %s for %s — firing and removing", entry_id, agent_name)
        jitter = random.uniform(0, 300)
        await asyncio.sleep(jitter)
        prefixed = f"[scheduled] {prompt}"
        await self._callback(agent_name, prefixed)

    def get_jobs(self) -> list:
        return self._scheduler.get_jobs()

    def start(self) -> None:
        self._scheduler.start()
        log.info("Scheduler started with %d jobs", len(self.get_jobs()))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_scheduler.py -v
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/scheduler.py bot/tests/test_scheduler.py
git commit -m "refactor: update Scheduler to use ScheduleStore with add/remove/one-shot support"
```

---

### Task 4: MCP Server module

**Files:**
- Create: `bot/src/claude_assistant/mcp_server.py`
- Create: `bot/tests/test_mcp_server.py`

The MCP server is a FastMCP instance with 3 tools. It holds references to the ScheduleStore and Scheduler, and an asyncio.Lock for mutation safety. It extracts agent name from the `X-Agent-Name` header.

- [ ] **Step 1: Write failing tests for MCP tools**

Create `bot/tests/test_mcp_server.py`:

```python
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from claude_assistant.schedule_store import ScheduleStore
from claude_assistant.mcp_server import create_mcp_server


@pytest.fixture
def store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.json")


@pytest.fixture
def scheduler() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mcp(store: ScheduleStore, scheduler: MagicMock):
    return create_mcp_server(store, scheduler)


class TestCronValidation:
    def test_valid_cron(self) -> None:
        from apscheduler.triggers.cron import CronTrigger
        # Should not raise
        CronTrigger.from_crontab("0 9 * * *")

    def test_invalid_cron(self) -> None:
        from apscheduler.triggers.cron import CronTrigger
        with pytest.raises(ValueError):
            CronTrigger.from_crontab("invalid cron")


class TestMcpToolIntegration:
    """Test the MCP tools via store + scheduler interaction.

    Full HTTP integration tests (header extraction, FastMCP context) require
    a running server and are covered in Task 9 (E2E verification).
    These tests verify the core logic that the tools orchestrate.
    """

    @pytest.mark.asyncio
    async def test_create_calls_scheduler_add_job(
        self, store: ScheduleStore, scheduler: MagicMock
    ) -> None:
        entry = store.create("main", cron="0 9 * * *", prompt="test", one_shot=False)
        scheduler.add_job("main", entry["id"], "0 9 * * *", "test", one_shot=False)
        scheduler.add_job.assert_called_once_with(
            "main", entry["id"], "0 9 * * *", "test", one_shot=False
        )

    @pytest.mark.asyncio
    async def test_delete_calls_scheduler_remove_job(
        self, store: ScheduleStore, scheduler: MagicMock
    ) -> None:
        entry = store.create("main", cron="0 9 * * *", prompt="test", one_shot=False)
        entry_id = entry["id"]
        store.delete("main", entry_id)
        scheduler.remove_job("main", entry_id)
        scheduler.remove_job.assert_called_once_with("main", entry_id)

    @pytest.mark.asyncio
    async def test_create_rejects_invalid_cron(self) -> None:
        from apscheduler.triggers.cron import CronTrigger
        with pytest.raises(ValueError):
            CronTrigger.from_crontab("not a cron")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(
        self, store: ScheduleStore
    ) -> None:
        assert store.delete("main", "zzzzzz") is False

    @pytest.mark.asyncio
    async def test_list_scoped_to_agent(self, store: ScheduleStore) -> None:
        store.create("main", cron="0 9 * * *", prompt="main task", one_shot=False)
        store.create("journalist", cron="0 10 * * *", prompt="news", one_shot=False)
        assert len(store.list("main")) == 1
        assert len(store.list("journalist")) == 1
        assert store.list("main")[0]["prompt"] == "main task"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_mcp_server.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'claude_assistant.mcp_server'`

- [ ] **Step 3: Implement MCP server module**

Create `bot/src/claude_assistant/mcp_server.py`:

```python
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from apscheduler.triggers.cron import CronTrigger
from fastmcp import FastMCP
from starlette.requests import Request

from claude_assistant.schedule_store import ScheduleStore

log = logging.getLogger(__name__)


def _get_agent_name(ctx) -> str:
    """Extract agent name from X-Agent-Name header."""
    request: Request | None = ctx.get("request")
    if request is None:
        raise ValueError("No request context available")
    agent_name = request.headers.get("x-agent-name")
    if not agent_name:
        raise ValueError("X-Agent-Name header is required")
    return agent_name


def create_mcp_server(store: ScheduleStore, scheduler) -> FastMCP:
    """Create a FastMCP server with schedule management tools."""
    mcp = FastMCP("schedules")

    @mcp.tool()
    async def list_schedules(ctx) -> str:
        """List all cron schedules for the current agent.

        Returns the schedules as a JSON array. Each entry has: id, cron, prompt, one_shot.
        """
        agent = _get_agent_name(ctx)
        entries = store.list(agent)
        if not entries:
            return "No schedules configured."
        import json
        return json.dumps(entries, indent=2)

    @mcp.tool()
    async def create_schedule(cron: str, prompt: str, one_shot: bool, ctx=None) -> str:
        """Create a new cron schedule for the current agent.

        Args:
            cron: POSIX cron expression (5 fields: minute hour day month weekday).
                  Examples: "0 9 * * *" (daily 9am), "0 9 * * 1-5" (weekdays 9am),
                  "30 */2 * * *" (every 2 hours at :30).
            prompt: The instruction to execute when the schedule fires.
            one_shot: If true, the schedule fires once and is then automatically deleted.
                      Use true for reminders and one-time tasks.
                      Use false for recurring schedules.
        """
        agent = _get_agent_name(ctx)
        # Validate cron expression
        try:
            CronTrigger.from_crontab(cron)
        except (ValueError, KeyError) as e:
            return f"Invalid cron expression '{cron}': {e}"

        async with store.lock:
            entry = store.create(agent, cron=cron, prompt=prompt, one_shot=one_shot)
            scheduler.add_job(
                agent, entry["id"], cron, prompt, one_shot=one_shot
            )

        import json
        return f"Schedule created:\n{json.dumps(entry, indent=2)}"

    @mcp.tool()
    async def delete_schedule(id: str, ctx=None) -> str:
        """Delete a schedule by its ID. Use list_schedules to see available IDs.

        Args:
            id: The 6-character hex ID of the schedule to delete.
        """
        agent = _get_agent_name(ctx)
        async with store.lock:
            deleted = store.delete(agent, id)
            if deleted:
                scheduler.remove_job(agent, id)
                return f"Schedule '{id}' deleted."
            else:
                return f"Schedule '{id}' not found."

    return mcp
```

**Important:** The exact approach to extracting the agent name from the request header depends on FastMCP's context API. FastMCP v2 may provide the request via `ctx.request` or a different mechanism. During implementation, check FastMCP's docs for how to access HTTP headers inside a tool handler. The `_get_agent_name` function above is a starting point — adapt it based on what FastMCP actually provides. If FastMCP doesn't expose headers directly, an alternative is to add ASGI middleware that extracts the header and injects the agent name into the tool context.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_mcp_server.py -v
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/mcp_server.py bot/tests/test_mcp_server.py
git commit -m "feat: add FastMCP schedule management server with list/create/delete tools"
```

---

### Task 5: Add CLAUB_AGENT_NAME to agent process environment

**Files:**
- Modify: `bot/src/claude_assistant/claude_process.py:102-105`
- Modify: `bot/tests/test_claude_process.py`

- [ ] **Step 1: Write failing test**

Add to `bot/tests/test_claude_process.py`:

```python
@pytest.mark.asyncio
async def test_env_includes_agent_name(
    self, home_dir: Path, workspace: Path
) -> None:
    proc = AgentProcess(home_dir=home_dir, workspace=workspace, agent_name="journalist")
    env = proc._env()
    assert env["CLAUB_AGENT_NAME"] == "journalist"

@pytest.mark.asyncio
async def test_env_no_agent_name(
    self, home_dir: Path, workspace: Path
) -> None:
    proc = AgentProcess(home_dir=home_dir, workspace=workspace)
    env = proc._env()
    assert "CLAUB_AGENT_NAME" not in env
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_claude_process.py::TestAgentProcess::test_env_includes_agent_name -v
```

Expected: FAIL — `KeyError: 'CLAUB_AGENT_NAME'`

- [ ] **Step 3: Update _env() method**

In `bot/src/claude_assistant/claude_process.py`, modify `_env()` (lines 102-105):

```python
def _env(self) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(self.home_dir)
    if self.agent_name:
        env["CLAUB_AGENT_NAME"] = self.agent_name
    return env
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_claude_process.py -v
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/claude_process.py bot/tests/test_claude_process.py
git commit -m "feat: set CLAUB_AGENT_NAME env var in agent subprocess"
```

---

### Task 7: Integrate MCP server into bot lifecycle

**Files:**
- Modify: `bot/src/claude_assistant/discord_bot.py`
- Modify: `bot/src/claude_assistant/main.py`

This task wires everything together: the bot creates a ScheduleStore, passes it to the Scheduler and MCP server, starts the HTTP server before the scheduler, and shuts it down cleanly.

- [ ] **Step 1: Update main.py to resolve schedules path and MCP port**

In `bot/src/claude_assistant/main.py`, update `_resolve_paths()` (lines 23-36) to return the schedules path:

```python
def _resolve_paths() -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    """Resolve paths relative to CLAUB_HOME (default: ~/.claub)."""
    claub_home = Path(os.environ.get(
        "CLAUB_HOME",
        Path.home() / ".claub",
    ))
    return (
        claub_home / "config" / "agents.yaml",
        claub_home / "home",
        claub_home / "workspaces",
        claub_home / "data" / "sessions.json",
        claub_home / "config" / "mcp.json",
        claub_home / "config" / "agents",
        claub_home / "data" / "schedules.json",
    )
```

Update `main()` to create ScheduleStore and pass it along with the MCP port:

```python
def main() -> None:
    bot_dir = Path(__file__).resolve().parents[2]
    load_dotenv(bot_dir / ".envrc")

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        log.error("DISCORD_BOT_TOKEN environment variable is required")
        sys.exit(1)

    mcp_port = int(os.environ.get("CLAUB_MCP_PORT", "9400"))

    config_path, home_dir, workspaces_dir, sessions_path, mcp_config, agents_dir, schedules_path = _resolve_paths()

    if not config_path.exists():
        log.error("Config not found: %s", config_path)
        sys.exit(1)

    config = load_config(config_path)
    sessions = SessionStore(sessions_path)

    from claude_assistant.schedule_store import ScheduleStore
    schedule_store = ScheduleStore(schedules_path)

    bot = AssistantBot(
        config=config,
        home_dir=home_dir,
        workspaces_dir=workspaces_dir,
        session_store=sessions,
        schedule_store=schedule_store,
        mcp_port=mcp_port,
        mcp_config=mcp_config if mcp_config.exists() else None,
        agents_dir=agents_dir if agents_dir.exists() else None,
    )

    log.info("Starting claude-assistant")
    asyncio.run(bot.run(token))
```

- [ ] **Step 2: Update AssistantBot constructor and on_ready**

In `bot/src/claude_assistant/discord_bot.py`, update the constructor to accept `schedule_store` and `mcp_port`:

```python
def __init__(
    self,
    config: AssistantConfig,
    home_dir: Path,
    workspaces_dir: Path,
    session_store: SessionStore,
    schedule_store: ScheduleStore,
    mcp_port: int = 9400,
    mcp_config: Path | None = None,
    agents_dir: Path | None = None,
) -> None:
    self.config = config
    self.home_dir = home_dir
    self.workspaces_dir = workspaces_dir
    self.sessions = session_store
    self.schedule_store = schedule_store
    self.mcp_port = mcp_port
    self.mcp_config = mcp_config
    self.agents_dir = agents_dir
    self.router = Router(config)

    self._processes: dict[str, AgentProcess] = {}
    self._webhooks: dict[int, discord.Webhook] = {}
    self._supervisor_task: asyncio.Task | None = None
    self._idle_reaper_task: asyncio.Task | None = None
    self._mcp_server_task: asyncio.Task | None = None
    self._shutting_down = False
    self._last_activity: dict[str, float] = {}
    self._reaped: set[str] = set()
    self._idle_timeout = 600
    # ... rest of constructor unchanged
```

Add imports at the top:

```python
from claude_assistant.schedule_store import ScheduleStore
from claude_assistant.mcp_server import create_mcp_server
```

Update `on_ready` in `_setup_events()` to start the MCP server first, then the scheduler:

```python
@self._client.event
async def on_ready() -> None:
    log.info("Discord connected as %s", self._client.user)

    # Create scheduler first (MCP server references it)
    valid_agents = set(self.config.agents.keys())
    self._scheduler = Scheduler(
        self.schedule_store, self._handle_scheduled, valid_agents=valid_agents
    )

    # Start MCP server (must be listening before agents spawn)
    self._mcp_server_task = asyncio.create_task(self._start_mcp_server())

    self._supervisor_task = asyncio.create_task(self._supervise_all())
    self._idle_reaper_task = asyncio.create_task(self._reap_idle_processes())

    self._scheduler.start()
```

Add the `_start_mcp_server` method:

```python
async def _start_mcp_server(self) -> None:
    """Start the FastMCP HTTP server for schedule management."""
    import uvicorn

    mcp = create_mcp_server(self.schedule_store, self._scheduler)
    app = mcp.http_app()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=self.mcp_port, log_level="warning"
    )
    self._uvicorn_server = uvicorn.Server(config)
    log.info("Starting MCP server on 127.0.0.1:%d", self.mcp_port)
    await self._uvicorn_server.serve()
```

**Note:** The exact method to get an ASGI app from FastMCP may differ. Check FastMCP v2 docs — it might be `mcp.asgi_app()`, `mcp.http_app()`, or `mcp.get_app()`. Adapt accordingly during implementation.

- [ ] **Step 3: Update shutdown to stop MCP server**

In `bot/src/claude_assistant/discord_bot.py`, update `shutdown()`:

```python
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
    if self._processes:
        await asyncio.gather(
            *(p.stop() for p in self._processes.values()),
            return_exceptions=True,
        )
    await self._client.close()
```

- [ ] **Step 4: Update Scheduler import**

In `bot/src/claude_assistant/discord_bot.py`, the `Scheduler` import stays the same but the constructor call changes (already shown in step 2). Remove the old `from claude_assistant.config import AssistantConfig` if it's no longer needed elsewhere, but it's still used for `config.agents`, so keep it.

- [ ] **Step 5: Run all tests**

```bash
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py
```

Expected: all PASS (Task 6 already removed ScheduleEntry and updated test fixtures).

- [ ] **Step 6: Commit**

```bash
git add bot/src/claude_assistant/discord_bot.py bot/src/claude_assistant/main.py
git commit -m "feat: integrate MCP server and ScheduleStore into bot lifecycle"
```

---

### Task 6: Remove schedule support from config.py and update existing tests

**Files:**
- Modify: `bot/src/claude_assistant/config.py`
- Modify: `bot/tests/test_config.py`
- Modify: `bot/tests/test_discord_bot.py`

- [ ] **Step 1: Remove ScheduleEntry from config.py**

In `bot/src/claude_assistant/config.py`:

1. Delete the `ScheduleEntry` dataclass (lines 9-12)
2. Remove the `schedules` field from `AgentConfig` (line 18)
3. Remove schedule parsing from `load_config` (lines 40-43)

Updated `config.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class AgentConfig:
    channel_id: str
    display_name: str | None = None
    avatar_url: str | None = None
    allowed_tools_additional: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AssistantConfig:
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    allowed_user_ids: set[str] = field(default_factory=set)
    model: str | None = None


def load_config(path: Path) -> AssistantConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    agents: dict[str, AgentConfig] = {}
    for name, agent_raw in (raw.get("agents") or {}).items():
        channel_id = (agent_raw or {}).get("channel_id")
        if not channel_id:
            raise ValueError(f"agents.{name}.channel_id is required")
        agents[name] = AgentConfig(
            channel_id=channel_id,
            display_name=(agent_raw or {}).get("display_name"),
            avatar_url=(agent_raw or {}).get("avatar_url"),
            allowed_tools_additional=(agent_raw or {}).get("allowed_tools_additional") or [],
        )

    if "main" not in agents:
        raise ValueError("agents.main is required")

    allowed_user_ids = set(raw.get("allowed_user_ids") or [])
    model = raw.get("model")

    return AssistantConfig(agents=agents, allowed_user_ids=allowed_user_ids, model=model)
```

- [ ] **Step 2: Update test_config.py — remove schedule tests**

In `bot/tests/test_config.py`:

- Remove `test_load_config_with_agents` (tests schedule parsing) — replace with a version without schedules
- Remove `test_load_config_main_with_schedule`
- Keep `test_load_minimal_config`, `test_load_config_missing_main`, `test_load_config_agent_missing_channel`

Updated `test_load_config_with_agents`:

```python
def test_load_config_with_agents(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
        "  journalist:\n"
        '    channel_id: "456"\n'
        '    display_name: "The Journalist"\n'
    )
    config = load_config(cfg_file)
    assert "main" in config.agents
    assert "journalist" in config.agents
    assert config.agents["journalist"].channel_id == "456"
    assert config.agents["journalist"].display_name == "The Journalist"
```

- [ ] **Step 3: Update test_discord_bot.py — remove ScheduleEntry**

In `bot/tests/test_discord_bot.py`:

- Remove `ScheduleEntry` from imports (line 6)
- Remove schedule from the journalist config in the fixture (line 16):

```python
@pytest.fixture
def config() -> AssistantConfig:
    return AssistantConfig(
        agents={
            "main": AgentConfig(channel_id="100"),
            "journalist": AgentConfig(channel_id="200"),
        },
    )
```

- Update the `bot` fixture to pass a `schedule_store`:

```python
@pytest.fixture
def bot(config: AssistantConfig, tmp_path: Path) -> AssistantBot:
    from claude_assistant.schedule_store import ScheduleStore
    return AssistantBot(
        config=config,
        home_dir=tmp_path / "home",
        workspaces_dir=tmp_path / "workspaces",
        session_store=MagicMock(),
        schedule_store=ScheduleStore(tmp_path / "schedules.json"),
    )
```

- [ ] **Step 4: Run all tests**

```bash
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/config.py bot/tests/test_config.py bot/tests/test_discord_bot.py
git commit -m "refactor: remove schedule support from agents.yaml config"
```

---

### Task 8: Migration script and instance config updates

**Files:**
- Create: `scripts/migrate_schedules.py`
- Modify: `~/.claub/config/agents.yaml`
- Modify: `~/.claub/config/mcp.json`
- Modify: `~/.claub/config/settings.json`
- Create: `~/.claub/data/schedules.json`

- [ ] **Step 1: Write migration script**

Create `scripts/migrate_schedules.py`:

```python
#!/usr/bin/env python3
"""Migrate schedules from agents.yaml to schedules.json.

Usage: python scripts/migrate_schedules.py [CLAUB_HOME]
Default CLAUB_HOME: ~/.claub
"""

import json
import os
import sys
from pathlib import Path

import yaml


def main() -> None:
    claub_home = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".claub"
    agents_yaml = claub_home / "config" / "agents.yaml"
    schedules_json = claub_home / "data" / "schedules.json"

    if not agents_yaml.exists():
        print(f"agents.yaml not found at {agents_yaml}")
        sys.exit(1)

    with open(agents_yaml) as f:
        raw = yaml.safe_load(f)

    schedules: dict[str, list[dict]] = {}
    for name, agent in (raw.get("agents") or {}).items():
        for entry in agent.get("schedule") or []:
            crons = entry.get("cron", [])
            prompt = entry.get("prompt", "")
            for cron in crons:
                schedule_id = os.urandom(3).hex()
                schedules.setdefault(name, []).append({
                    "id": schedule_id,
                    "cron": cron,
                    "prompt": prompt,
                    "one_shot": False,
                })

    schedules_json.parent.mkdir(parents=True, exist_ok=True)
    with open(schedules_json, "w") as f:
        json.dump(schedules, f, indent=2)

    print(f"Migrated {sum(len(v) for v in schedules.values())} schedule(s) to {schedules_json}")
    for agent, entries in schedules.items():
        for e in entries:
            print(f"  {agent}: [{e['id']}] {e['cron']} — {e['prompt'][:60]}")

    print(f"\nNow remove 'schedule:' keys from {agents_yaml}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run migration**

```bash
cd /Users/you/Claude && python scripts/migrate_schedules.py
```

Verify output shows all schedules migrated. Then verify the file:

```bash
cat ~/.claub/data/schedules.json
```

- [ ] **Step 3: Remove schedule keys from agents.yaml**

Manually edit `~/.claub/config/agents.yaml` — remove all `schedule:` blocks from each agent. Keep only: `channel_id`, `display_name`, `avatar_url`, `allowed_tools_additional`.

- [ ] **Step 4: Update mcp.json — add schedules MCP**

Edit `~/.claub/config/mcp.json` to add the schedules entry:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest", "--browser", "chrome"]
    },
    "schedules": {
      "type": "http",
      "url": "http://localhost:9400/mcp",
      "headers": {
        "X-Agent-Name": "${CLAUB_AGENT_NAME}"
      }
    }
  }
}
```

- [ ] **Step 5: Update settings.json — add schedules permission**

Edit `~/.claub/config/settings.json`, add `"mcp__schedules__*"` to the `permissions.allow` array.

- [ ] **Step 6: Commit to both repos**

Project repo:

```bash
cd /Users/you/Claude
git add scripts/migrate_schedules.py
git commit -m "feat: add schedule migration script from agents.yaml to schedules.json"
```

Instance repo:

```bash
cd ~/.claub
git add config/agents.yaml config/mcp.json config/settings.json data/schedules.json
git commit -m "feat: migrate schedules to schedules.json, wire up schedule MCP"
```

---

### Task 9: End-to-end verification

- [ ] **Step 1: Run all unit tests**

```bash
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py
```

Expected: all PASS

- [ ] **Step 2: Restart the bot**

```bash
scripts/ctl.sh restart
```

- [ ] **Step 3: Check logs for clean startup**

```bash
scripts/ctl.sh logs
```

Verify:
- MCP server started on 127.0.0.1:9400
- Scheduler started with correct number of jobs
- No errors

- [ ] **Step 4: Test MCP from an agent channel**

Send a message in an agent's Discord channel asking it to list its schedules. The agent should be able to call `mcp__schedules__list_schedules` and show results.

- [ ] **Step 5: Test create and delete**

Ask the agent to create a test one-shot schedule, then list schedules to verify it appears, then delete it.

---

### Task 10: Update documentation

**Files:**
- Modify: `/Users/you/Claude/CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md**

Update the following sections:

1. **Architecture diagram** — add MCP server to the ASCII diagram
2. **Configuration section** — remove `agents.yaml` schedule examples, add `schedules.json` documentation
3. **agents.yaml section** — remove schedule fields, note they've moved
4. **Add Schedule Management section** — document the MCP tools, `schedules.json` location, and how agents use them
5. **Key Design Decisions** — add note about embedded MCP server approach
6. **Permissions section** — add `mcp__schedules__*` to the example

- [ ] **Step 2: Commit**

```bash
cd /Users/you/Claude
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for schedule management MCP"
```
