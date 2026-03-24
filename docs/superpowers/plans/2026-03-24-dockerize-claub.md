# Dockerize Claub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Containerize the Claub Discord bot so it runs in Docker without the HOME override hack, using Claude CLI's native `~/.claude/` path.

**Architecture:** Docker-first deployment. Bot source baked into image, instance config/data/workspaces/mcps bind-mounted at `/claub/`. Claude CLI credentials persist in a named volume at `~/.claude/`. Entrypoint copies config into `~/.claude/` on each start.

**Tech Stack:** Docker, docker-compose, python:3.12-slim, Node.js (for Claude CLI), uv

**Spec:** `docs/superpowers/specs/2026-03-24-dockerize-claub-design.md`

---

### Task 1: Remove `home_dir` from `AgentProcess`

**Files:**
- Modify: `bot/src/claude_assistant/claude_process.py:42-51` (constructor)
- Modify: `bot/src/claude_assistant/claude_process.py:102-107` (`_env` method)
- Test: `bot/tests/test_claude_process.py`

- [ ] **Step 1: Update tests — remove `home_dir` fixture and parameter**

In `bot/tests/test_claude_process.py`:
- Delete the `home_dir` fixture (lines 9-14)
- Remove `home_dir` parameter from every test method signature
- Change every `AgentProcess(home_dir=home_dir, workspace=workspace, ...)` to `AgentProcess(workspace=workspace, ...)`
- Add a new test `test_env_does_not_override_home`:

```python
@pytest.mark.asyncio
async def test_env_does_not_override_home(self, workspace: Path) -> None:
    import os
    proc = AgentProcess(workspace=workspace)
    env = proc._env()
    assert env["HOME"] == os.environ["HOME"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_claude_process.py -v`
Expected: FAIL — `AgentProcess.__init__()` still requires `home_dir`

- [ ] **Step 3: Update `AgentProcess` — remove `home_dir`**

In `bot/src/claude_assistant/claude_process.py`:

Remove `home_dir` from `__init__` parameters and body:
```python
def __init__(
    self,
    workspace: Path,
    mcp_configs: list[Path] | None = None,
    agent_name: str | None = None,
    allowed_tools_additional: list[str] | None = None,
    model: str | None = None,
) -> None:
    self.workspace = workspace
    self.mcp_configs = mcp_configs or []
    self.agent_name = agent_name
    self.allowed_tools_additional = allowed_tools_additional or []
    self.model = model
    self._process: asyncio.subprocess.Process | None = None
    self._session_id: str | None = None
    self._lock = asyncio.Lock()
    self._lifecycle_lock = asyncio.Lock()
    self._ready = asyncio.Event()
```

Remove the `HOME` line from `_env()`:
```python
def _env(self) -> dict[str, str]:
    env = os.environ.copy()
    if self.agent_name:
        env["CLAUB_AGENT_NAME"] = self.agent_name
    return env
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_claude_process.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/claude_process.py bot/tests/test_claude_process.py
git commit -m "refactor: remove home_dir from AgentProcess, stop overriding HOME"
```

---

### Task 2: Remove `home_dir` from `AssistantBot` and `main.py`

**Files:**
- Modify: `bot/src/claude_assistant/discord_bot.py:24-34` (constructor), `:86-93` (`_start_agent`)
- Modify: `bot/src/claude_assistant/main.py:24-38` (`_resolve_paths`), `:51-72` (`main`)
- Test: `bot/tests/test_discord_bot.py`

- [ ] **Step 1: Update test — remove `home_dir` from bot fixture**

In `bot/tests/test_discord_bot.py`, update the `bot` fixture (lines 20-28):
```python
@pytest.fixture
def bot(config: AssistantConfig, tmp_path: Path) -> AssistantBot:
    from claude_assistant.schedule_store import ScheduleStore
    return AssistantBot(
        config=config,
        workspaces_dir=tmp_path / "workspaces",
        session_store=MagicMock(),
        schedule_store=ScheduleStore(tmp_path / "schedules.json"),
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_discord_bot.py -v`
Expected: FAIL — `AssistantBot.__init__()` still requires `home_dir`

- [ ] **Step 3: Update `AssistantBot` — remove `home_dir`**

In `bot/src/claude_assistant/discord_bot.py`:

Remove `home_dir` from constructor (lines 24-42):
```python
def __init__(
    self,
    config: AssistantConfig,
    workspaces_dir: Path,
    session_store: SessionStore,
    schedule_store: ScheduleStore,
    mcp_config: Path | None = None,
    agents_dir: Path | None = None,
    mcp_port: int = 9400,
) -> None:
    self.config = config
    self.workspaces_dir = workspaces_dir
    self.sessions = session_store
    self.schedule_store = schedule_store
    self.mcp_config = mcp_config
    self.agents_dir = agents_dir
    self.mcp_port = mcp_port
    self.router = Router(config)
```

Remove `home_dir=self.home_dir` from `_start_agent` (line 86-93):
```python
process = AgentProcess(
    workspace=workspace,
    mcp_configs=self._mcp_configs_for(name),
    agent_name=name,
    allowed_tools_additional=agent_config.allowed_tools_additional if agent_config else [],
    model=self.config.model,
)
```

- [ ] **Step 4: Update `main.py` — remove `home_dir` from path resolution**

In `bot/src/claude_assistant/main.py`:

Update `_resolve_paths` to return a 6-tuple (remove `claub_home / "home"` at line 32):
```python
def _resolve_paths() -> tuple[Path, Path, Path, Path, Path, Path]:
    """Resolve paths relative to CLAUB_HOME (default: ~/.claub)."""
    claub_home = Path(os.environ.get(
        "CLAUB_HOME",
        Path.home() / ".claub",
    ))
    return (
        claub_home / "config" / "agents.yaml",
        claub_home / "workspaces",
        claub_home / "data" / "sessions.json",
        claub_home / "config" / "mcp.json",
        claub_home / "config" / "agents",
        claub_home / "data" / "schedules.json",
    )
```

Update the unpacking at line 51 (was 7 vars, now 6):
```python
config_path, workspaces_dir, sessions_path, mcp_config, agents_dir, schedules_path = _resolve_paths()
```

Remove `home_dir=home_dir` from the `AssistantBot(...)` call (lines 63-72):
```python
bot = AssistantBot(
    config=config,
    workspaces_dir=workspaces_dir,
    session_store=sessions,
    schedule_store=schedules,
    mcp_config=mcp_config if mcp_config.exists() else None,
    agents_dir=agents_dir if agents_dir.exists() else None,
    mcp_port=mcp_port,
)
```

- [ ] **Step 5: Run all tests to verify they pass**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add bot/src/claude_assistant/discord_bot.py bot/src/claude_assistant/main.py bot/tests/test_discord_bot.py
git commit -m "refactor: remove home_dir from AssistantBot and main.py path resolution"
```

---

### Task 3: Update integration test

**Files:**
- Modify: `bot/tests/test_integration.py`

- [ ] **Step 1: Remove `home_dir` from integration test**

In `bot/tests/test_integration.py`:

Delete the `home_dir` fixture (lines 20-30) entirely. Since Docker is the primary deployment target and Claude CLI uses the real `~/.claude/`, the integration test no longer needs to copy credentials into a temp dir — it just uses the real home.

Update the test class (lines 40-54):
```python
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
```

- [ ] **Step 2: Commit**

```bash
git add bot/tests/test_integration.py
git commit -m "test: update integration test to remove home_dir"
```

---

### Task 4: Create Docker artifacts

**Files:**
- Create: `Dockerfile`
- Create: `entrypoint.sh`
- Create: `docker-compose.yml`
- Create: `.dockerignore`

- [ ] **Step 1: Create `.dockerignore`**

Create `.dockerignore` at repo root:
```
.git/
scripts/
docs/
__pycache__/
*.pyc
*.egg-info
.venv/
.envrc
.gitignore
instance/
bot/tests/
```

- [ ] **Step 2: Create `entrypoint.sh`**

Create `entrypoint.sh` at repo root:
```bash
#!/bin/sh
set -e

# Copy config into Claude CLI's native home
mkdir -p ~/.claude
cp -r /claub/config/agents/ ~/.claude/agents/ 2>/dev/null || true
cp /claub/config/settings.json ~/.claude/settings.json 2>/dev/null || true
cp /claub/config/CLAUDE.md ~/.claude/CLAUDE.md 2>/dev/null || true

# Ensure data and workspace dirs exist
mkdir -p /claub/data /claub/workspaces

cd /app/bot
exec uv run claude-assistant
```

- [ ] **Step 3: Create `Dockerfile`**

Create `Dockerfile` at repo root:
```dockerfile
FROM python:3.12-slim

# Install Node.js (required for Claude CLI) and procps (for healthcheck pgrep)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl procps && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Install Claude CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Copy bot source and install dependencies
WORKDIR /app/bot
COPY bot/pyproject.toml bot/uv.lock ./
COPY bot/src ./src
RUN uv sync --frozen --no-dev

# Default env
ENV CLAUB_HOME=/claub

# Entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
```

Note: `COPY bot/src` must come before `uv sync` because hatchling (the build backend) needs the package source to build. This sacrifices layer caching for dependency installs but ensures the build works.

- [ ] **Step 4: Create `docker-compose.yml`**

Create `docker-compose.yml` at repo root:
```yaml
services:
  claub:
    build: .
    environment:
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
    volumes:
      - claude-home:/root/.claude
      - ./instance/config:/claub/config
      - ./instance/data:/claub/data
      - ./instance/workspaces:/claub/workspaces
      - ./instance/mcps:/claub/mcps
    init: true
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "pgrep", "-f", "claude-assistant"]
      interval: 30s
      timeout: 5s
      retries: 3

volumes:
  claude-home:
```

- [ ] **Step 5: Commit**

```bash
git add .dockerignore Dockerfile entrypoint.sh docker-compose.yml
git commit -m "feat: add Dockerfile, entrypoint, compose, and dockerignore"
```

---

### Task 5: Build and smoke test

- [ ] **Step 1: Build the image**

Run: `cd /Users/you/Claude && docker build -t claub .`
Expected: Successful build, no errors

- [ ] **Step 2: Verify image contents**

Run: `docker run --rm --entrypoint which claub claude`
Expected: Prints path to `claude` binary

Run: `docker run --rm --entrypoint which claub uv`
Expected: Prints `/usr/local/bin/uv`

Run: `docker run --rm --entrypoint python claub --version`
Expected: `Python 3.12.x`

- [ ] **Step 3: Test entrypoint config copy (just the copy logic, not the full bot)**

```bash
mkdir -p /tmp/claub-test/config/agents
echo "test: true" > /tmp/claub-test/config/settings.json
echo "# test" > /tmp/claub-test/config/CLAUDE.md
echo "name: test" > /tmp/claub-test/config/agents/main.md

docker run --rm \
  -v /tmp/claub-test/config:/claub/config \
  --entrypoint sh claub \
  -c "
    mkdir -p ~/.claude
    cp -r /claub/config/agents/ ~/.claude/agents/ 2>/dev/null || true
    cp /claub/config/settings.json ~/.claude/settings.json 2>/dev/null || true
    cp /claub/config/CLAUDE.md ~/.claude/CLAUDE.md 2>/dev/null || true
    echo '--- ~/.claude contents ---'
    ls -la ~/.claude/
    echo '--- settings.json ---'
    cat ~/.claude/settings.json
    echo '--- agents/ ---'
    ls ~/.claude/agents/
  "

rm -rf /tmp/claub-test
```
Expected: Shows `settings.json`, `CLAUDE.md`, and `agents/main.md` in `~/.claude/`

- [ ] **Step 4: Commit any fixes**

If any issues were found during smoke testing, fix and commit:
```bash
git add -A && git commit -m "fix: address issues found during Docker smoke test"
```
