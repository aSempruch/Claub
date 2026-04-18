# Playwright Lifecycle Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each agent its own persistent Playwright browser session (cookies/login state that survive restarts), implemented via generic `on_start` / `on_stop` lifecycle hooks in the bot plus a host-side Playwright bridge daemon — no Playwright-specific code in the bot.

**Architecture:**
- Generic lifecycle hooks in `agents.yaml` (global + additive per-agent), executed by `AgentProcess` around claude subprocess start/stop. The bot has no concept of Playwright.
- A tiny host-side Python daemon (the "bridge") spawns per-agent Playwright MCP child processes on demand, each with its own `--user-data-dir`. The hooks `curl` the bridge.
- Per-agent `.mcp.json` files route each agent to its own Playwright port. Shared `mcp.json` loses the `playwright` entry.

**Tech Stack:** Python stdlib (bridge daemon), existing bot stack (asyncio, PyYAML), launchd, Playwright MCP.

**Why this shape:** Hooks run commands — they don't know about Playwright. That keeps `bot/` MCP-agnostic (current invariant). Empirical test (see session context) confirmed that Claude Code's `SessionStart` hook does NOT block MCP connection — so the timing-sensitive pre-spawn must happen in the bot layer before `claude` is exec'd, which is why the hooks sit in `AgentProcess` rather than being configured on Claude Code itself.

---

## File Structure

### Created in repo
- `scripts/playwright-bridge/bridge.py` — the daemon
- `scripts/playwright-bridge/config.example.json` — example bridge config
- `scripts/playwright-bridge/com.asempruch.playwright-bridge.plist.template` — launchd template
- `scripts/playwright-bridge/README.md` — setup instructions
- `bot/tests/test_playwright_bridge.py` — bridge tests (exercises real daemon subprocess with a stub command)

### Modified in repo
- `bot/src/claude_assistant/config.py` — add `on_start` / `on_stop` fields (top-level + per-agent, additive)
- `bot/src/claude_assistant/claude_process.py` — execute hooks around claude subprocess
- `bot/src/claude_assistant/discord_bot.py` — pass merged hooks into `AgentProcess`
- `bot/tests/test_config.py` — coverage for new config fields
- `bot/tests/test_claude_process.py` — coverage for hook execution
- `CLAUDE.md` — document hooks feature and Playwright bridge architecture
- `example/config/agents.yaml` — add sample `on_start` / `on_stop`
- `example/config/mcp.json` — drop `playwright`
- `example/config/agents/*.mcp.json` — add per-agent `playwright` entries

### Rollout on the dev host (not version-controlled)
- Install `~/Library/LaunchAgents/com.asempruch.playwright-bridge.plist`
- Write `~/docker/claub/playwright-bridge-config.json`
- Edit `~/docker/claub/config/agents.yaml`, `~/docker/claub/config/mcp.json`, `~/docker/claub/config/agents/*.mcp.json`
- Retire `~/Library/LaunchAgents/com.asempruch.playwright-mcp.plist`

---

## Task 1: Add `on_start` / `on_stop` config fields

**Files:**
- Modify: `bot/src/claude_assistant/config.py`
- Test: `bot/tests/test_config.py`

Additive semantics: final hook list is `global + per-agent` in that order.

- [ ] **Step 1: Write failing tests**

Append to `bot/tests/test_config.py`:

```python
def test_on_start_global_only(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        'on_start: ["echo global-start"]\n'
        'on_stop: ["echo global-stop"]\n'
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
    )
    config = load_config(cfg_file)
    assert config.on_start == ["echo global-start"]
    assert config.on_stop == ["echo global-stop"]
    assert config.agents["main"].on_start == []
    assert config.agents["main"].on_stop == []


def test_on_start_per_agent_additive(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        'on_start: ["echo global"]\n'
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
        '    on_start: ["echo local"]\n'
    )
    config = load_config(cfg_file)
    assert config.on_start == ["echo global"]
    assert config.agents["main"].on_start == ["echo local"]


def test_on_start_empty_defaults(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
    )
    config = load_config(cfg_file)
    assert config.on_start == []
    assert config.on_stop == []
    assert config.agents["main"].on_start == []
    assert config.agents["main"].on_stop == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_config.py -k "on_start" -v
```
Expected: 3 failures (AttributeError: 'AssistantConfig' object has no attribute 'on_start').

- [ ] **Step 3: Implement**

Edit `bot/src/claude_assistant/config.py`. Add fields to both dataclasses and populate in `load_config`:

```python
@dataclass(frozen=True)
class AgentConfig:
    channel_id: str
    display_name: str | None = None
    avatar_url: str | None = None
    allowed_tools_additional: list[str] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)
    effort: str | None = None
    compact_pct: int | None = None
    on_start: list[str] = field(default_factory=list)
    on_stop: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AssistantConfig:
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    allowed_user_ids: set[str] = field(default_factory=set)
    model: str | None = None
    allowed_skills: list[str] = field(default_factory=list)
    effort: str | None = None
    compact_pct: int | None = None
    on_start: list[str] = field(default_factory=list)
    on_stop: list[str] = field(default_factory=list)
```

In `load_config`, inside the per-agent loop, add:

```python
agents[name] = AgentConfig(
    channel_id=channel_id,
    display_name=(agent_raw or {}).get("display_name"),
    avatar_url=(agent_raw or {}).get("avatar_url"),
    allowed_tools_additional=(agent_raw or {}).get("allowed_tools_additional") or [],
    allowed_skills=(agent_raw or {}).get("allowed_skills") or [],
    effort=agent_effort,
    compact_pct=agent_compact_pct,
    on_start=(agent_raw or {}).get("on_start") or [],
    on_stop=(agent_raw or {}).get("on_stop") or [],
)
```

And at the bottom before `return`:

```python
on_start = raw.get("on_start") or []
on_stop = raw.get("on_stop") or []

return AssistantConfig(
    agents=agents,
    allowed_user_ids=allowed_user_ids,
    model=model,
    allowed_skills=allowed_skills,
    effort=effort,
    compact_pct=compact_pct,
    on_start=on_start,
    on_stop=on_stop,
)
```

- [ ] **Step 4: Run tests to verify they pass**

```
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_config.py -v
```
Expected: all pass (including new ones).

- [ ] **Step 5: Commit**

```
git add bot/src/claude_assistant/config.py bot/tests/test_config.py
git commit -m "feat(config): add on_start/on_stop lifecycle hook lists"
```

---

## Task 2: Execute lifecycle hooks in `AgentProcess`

**Files:**
- Modify: `bot/src/claude_assistant/claude_process.py`
- Test: `bot/tests/test_claude_process.py`

Semantics:
- `on_start` hooks run **before** the claude subprocess is spawned; failures log a warning but don't abort startup.
- `on_stop` hooks run **after** claude has exited; same failure policy.
- Shell invocation via `asyncio.create_subprocess_shell` so `$CLAUB_AGENT_NAME` interpolates; stdout/stderr is captured and logged.
- Per-hook timeout of 15s; on timeout the hook subprocess is killed and a warning is logged.

- [ ] **Step 1: Write failing tests**

Add to `bot/tests/test_claude_process.py`:

```python
@pytest.mark.asyncio
async def test_on_start_hooks_run_before_spawn(workspace: Path, tmp_path: Path) -> None:
    """on_start hooks execute and receive CLAUB_AGENT_NAME in env."""
    marker = tmp_path / "marker.txt"
    hook = f'echo "$CLAUB_AGENT_NAME" > {marker}'
    proc = AgentProcess(
        workspace=workspace,
        agent_name="journalist",
        on_start_hooks=[hook],
    )
    await proc._run_hooks(proc.on_start_hooks, phase="on_start")
    assert marker.read_text().strip() == "journalist"


@pytest.mark.asyncio
async def test_on_stop_hooks_run(workspace: Path, tmp_path: Path) -> None:
    marker = tmp_path / "stopped.txt"
    hook = f'echo stopped > {marker}'
    proc = AgentProcess(
        workspace=workspace,
        agent_name="main",
        on_stop_hooks=[hook],
    )
    await proc._run_hooks(proc.on_stop_hooks, phase="on_stop")
    assert marker.read_text().strip() == "stopped"


@pytest.mark.asyncio
async def test_hook_failure_is_non_fatal(workspace: Path, caplog) -> None:
    """A failing hook logs a warning but does not raise."""
    import logging
    caplog.set_level(logging.WARNING)
    proc = AgentProcess(
        workspace=workspace,
        agent_name="main",
        on_start_hooks=["false"],
    )
    await proc._run_hooks(proc.on_start_hooks, phase="on_start")
    assert any("on_start hook failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_hook_timeout(workspace: Path, caplog) -> None:
    import logging
    caplog.set_level(logging.WARNING)
    proc = AgentProcess(
        workspace=workspace,
        agent_name="main",
        on_start_hooks=["sleep 30"],
        hook_timeout=0.5,
    )
    await proc._run_hooks(proc.on_start_hooks, phase="on_start")
    assert any("timed out" in rec.message for rec in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_claude_process.py -k "hook" -v
```
Expected: all four fail (TypeError: unexpected keyword argument 'on_start_hooks').

- [ ] **Step 3: Implement hook execution**

In `bot/src/claude_assistant/claude_process.py`, extend the constructor signature and add `_run_hooks`:

```python
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
    on_start_hooks: list[str] | None = None,
    on_stop_hooks: list[str] | None = None,
    hook_timeout: float = 15.0,
) -> None:
    # ... existing assignments ...
    self.on_start_hooks = on_start_hooks or []
    self.on_stop_hooks = on_stop_hooks or []
    self.hook_timeout = hook_timeout
```

Add the helper method on `AgentProcess`:

```python
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
                stdout, stderr = await asyncio.wait_for(
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
            log.exception("%s hook raised (agent=%s): %s", phase, self.agent_name, hook)
```

In `start()`, run `on_start` hooks before `asyncio.create_subprocess_exec`:

```python
async def start(self, session_id: str | None = None) -> None:
    async with self._lifecycle_lock:
        self._ready.clear()
        await self._run_hooks(self.on_start_hooks, phase="on_start")
        cmd = self._build_command(session_id)
        log.info("Starting agent %s: %s", self.agent_name or "unnamed", " ".join(cmd))
        # ... rest unchanged ...
```

In `stop()`, run `on_stop` hooks after the process exits:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_claude_process.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```
git add bot/src/claude_assistant/claude_process.py bot/tests/test_claude_process.py
git commit -m "feat(agent): run on_start/on_stop shell hooks around claude subprocess"
```

---

## Task 3: Wire merged hooks from config into `AgentProcess`

**Files:**
- Modify: `bot/src/claude_assistant/discord_bot.py`
- Test: existing `bot/tests/test_discord_bot.py` stays green; add a small test confirming merge.

Merge rule matches `allowed_skills` precedent: `global + agent` (list concatenation, preserves order, duplicates allowed).

- [ ] **Step 1: Write failing test**

Add to `bot/tests/test_discord_bot.py` (check how the existing module imports things and follow that style — if it imports helpers from `discord_bot`, the test below should work as-is):

```python
def test_merged_hooks_helper() -> None:
    from claude_assistant.config import AgentConfig, AssistantConfig
    from claude_assistant.discord_bot import _merged_hooks

    cfg = AssistantConfig(
        agents={
            "main": AgentConfig(channel_id="1", on_start=["echo a"]),
        },
        on_start=["echo g1", "echo g2"],
    )
    assert _merged_hooks(cfg, "main", "on_start") == ["echo g1", "echo g2", "echo a"]
    assert _merged_hooks(cfg, "main", "on_stop") == []
```

- [ ] **Step 2: Run test to verify it fails**

```
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_discord_bot.py -k "merged_hooks" -v
```
Expected: ImportError for `_merged_hooks`.

- [ ] **Step 3: Implement `_merged_hooks` + pass through**

Add at module level in `bot/src/claude_assistant/discord_bot.py`:

```python
def _merged_hooks(config: AssistantConfig, agent_name: str, attr: str) -> list[str]:
    """Concatenate global and per-agent hook lists (global first)."""
    global_hooks: list[str] = list(getattr(config, attr, []) or [])
    agent_cfg = config.agents.get(agent_name)
    agent_hooks: list[str] = list(getattr(agent_cfg, attr, []) or []) if agent_cfg else []
    return global_hooks + agent_hooks
```

In `_start_agent`, pass merged lists into `AgentProcess`:

```python
process = AgentProcess(
    workspace=workspace,
    mcp_configs=self._mcp_configs_for(name),
    agent_name=name,
    agent_definition=self._load_agent_definition(name),
    allowed_tools_additional=agent_config.allowed_tools_additional if agent_config else [],
    model=self.config.model,
    disallowed_skills=self._disallowed_skills_for(name),
    effort=effort,
    compact_pct=compact_pct,
    on_start_hooks=_merged_hooks(self.config, name, "on_start"),
    on_stop_hooks=_merged_hooks(self.config, name, "on_stop"),
)
```

- [ ] **Step 4: Run tests**

```
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py
```
Expected: all pass.

- [ ] **Step 5: Commit**

```
git add bot/src/claude_assistant/discord_bot.py bot/tests/test_discord_bot.py
git commit -m "feat(bot): merge global + per-agent hooks into AgentProcess"
```

---

## Task 4: Bridge daemon tests (write first)

**Files:**
- Create: `bot/tests/test_playwright_bridge.py`

These tests drive the bridge design. They spawn the real `bridge.py` subprocess and hit it over HTTP; `command_template` is overridden to a tiny Python stub so we don't need npm.

- [ ] **Step 1: Write the tests**

```python
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[2] / "scripts" / "playwright-bridge" / "bridge.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.3).read()
            return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError(url)


def post(url: str, timeout: float = 20.0) -> tuple[int, str]:
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture
def bridge(tmp_path: Path):
    """Yield a running bridge with a stub command. Cleans up on teardown."""
    bridge_port = free_port()
    stub_port = free_port()

    stub = (
        "import http.server, socketserver, sys, os\n"
        "p = int(sys.argv[sys.argv.index('--port')+1])\n"
        "u = sys.argv[sys.argv.index('--user-data-dir')+1]\n"
        "os.makedirs(u, exist_ok=True)\n"
        "open(os.path.join(u, 'spawned.txt'), 'w').write('yes')\n"
        "srv = socketserver.TCPServer(('127.0.0.1', p), http.server.BaseHTTPRequestHandler)\n"
        "srv.serve_forever()\n"
    )

    cfg = {
        "listen_host": "127.0.0.1",
        "listen_port": bridge_port,
        "command_template": [
            sys.executable, "-c", stub,
            "--port", "{port}",
            "--user-data-dir", "{user_data_dir}",
        ],
        "agents": {
            "test-agent": {
                "port": stub_port,
                "user_data_dir": str(tmp_path / "profile"),
            },
        },
    }
    cfg_path = tmp_path / "bridge.json"
    cfg_path.write_text(json.dumps(cfg))

    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), "--config", str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        wait_http(f"http://127.0.0.1:{bridge_port}/status")
        yield {"port": bridge_port, "stub_port": stub_port, "profile": tmp_path / "profile"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_unknown_agent_is_noop(bridge):
    code, _ = post(f"http://127.0.0.1:{bridge['port']}/start/other")
    assert code == 204


def test_start_and_stop(bridge):
    code, body = post(f"http://127.0.0.1:{bridge['port']}/start/test-agent")
    assert code == 200, body
    assert json.loads(body)["status"] == "started"
    assert (bridge["profile"] / "spawned.txt").read_text() == "yes"
    with socket.create_connection(("127.0.0.1", bridge["stub_port"]), timeout=1):
        pass

    code, body = post(f"http://127.0.0.1:{bridge['port']}/stop/test-agent")
    assert code == 200
    assert json.loads(body)["status"] == "stopped"

    time.sleep(0.3)
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", bridge["stub_port"]), timeout=0.5):
            pass


def test_double_start_is_idempotent(bridge):
    code1, body1 = post(f"http://127.0.0.1:{bridge['port']}/start/test-agent")
    assert code1 == 200
    pid1 = json.loads(body1)["pid"]

    code2, body2 = post(f"http://127.0.0.1:{bridge['port']}/start/test-agent")
    assert code2 == 200
    assert json.loads(body2)["pid"] == pid1

    post(f"http://127.0.0.1:{bridge['port']}/stop/test-agent")


def test_stop_when_not_running(bridge):
    code, body = post(f"http://127.0.0.1:{bridge['port']}/stop/test-agent")
    assert code == 200
    assert json.loads(body)["status"] == "not-running"
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_playwright_bridge.py -v
```
Expected: all four fail — the bridge script doesn't exist yet.

- [ ] **Step 3: Commit the failing tests**

```
git add bot/tests/test_playwright_bridge.py
git commit -m "test(bridge): failing tests covering start/stop/idempotent/unknown"
```

---

## Task 5: Bridge daemon implementation

**Files:**
- Create: `scripts/playwright-bridge/bridge.py`
- Create: `scripts/playwright-bridge/config.example.json`

Design (encoded into the tests above):
- stdlib-only Python 3.10+ (no deps — runs via `/usr/bin/env python3` on the host).
- `ThreadingHTTPServer` on `0.0.0.0:9500` (reachable from container via `host.docker.internal`).
- In-memory `state: dict[str, subprocess.Popen]`, guarded by a `threading.Lock`.
- `POST /start/<agent>`: if already running and alive, 200 immediately. Otherwise: read agent's `port` + `user_data_dir` from config, spawn `command_template` with `{port}` and `{user_data_dir}` substituted, wait up to 15s for `127.0.0.1:<port>` to accept a TCP connection, return 200 if listening, 504 if not. Unknown agent → 204 no-op (so global hooks can safely fire for browser-less agents).
- `POST /stop/<agent>`: if running, SIGTERM + wait 5s + SIGKILL. Return 200 (idempotent).
- `GET /status`: JSON map of agents with alive/pid/port.
- SIGTERM handler kills all children.

- [ ] **Step 1: Create `config.example.json`**

```json
{
  "listen_host": "0.0.0.0",
  "listen_port": 9500,
  "command_template": [
    "/opt/homebrew/bin/npx",
    "@playwright/mcp@latest",
    "--snapshot-mode=none",
    "--image-responses=omit",
    "--console-level=error",
    "--host", "127.0.0.1",
    "--port", "{port}",
    "--allowed-hosts", "host.docker.internal:{port},localhost:{port}",
    "--init-script", "/Users/you/Library/Application Support/playwright-mcp/init.js",
    "--config", "/Users/you/Library/Application Support/playwright-mcp/config.json",
    "--allow-unrestricted-file-access",
    "--user-data-dir", "{user_data_dir}"
  ],
  "agents": {
    "main":               { "port": 3846, "user_data_dir": "/Users/you/docker/claub/playwright-profiles/main" },
    "journalist":         { "port": 3847, "user_data_dir": "/Users/you/docker/claub/playwright-profiles/journalist" },
    "career":             { "port": 3848, "user_data_dir": "/Users/you/docker/claub/playwright-profiles/career" },
    "shopping-assistant": { "port": 3849, "user_data_dir": "/Users/you/docker/claub/playwright-profiles/shopping-assistant" },
    "planner":       { "port": 3850, "user_data_dir": "/Users/you/docker/claub/playwright-profiles/planner" }
  }
}
```

- [ ] **Step 2: Create `bridge.py`**

```python
#!/usr/bin/env python3
"""Playwright bridge daemon.

Spawns a per-agent @playwright/mcp child process on demand, each with a
persistent --user-data-dir so login state survives across restarts.

Lifecycle is driven by HTTP calls from the bot (typically via global
on_start / on_stop hooks configured in agents.yaml).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("playwright-bridge")

STATE: dict[str, subprocess.Popen] = {}
STATE_LOCK = threading.Lock()
CONFIG: dict = {}


def wait_for_port(host: str, port: int, timeout: float) -> bool:
    """Block until (host,port) accepts a TCP connection or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def build_command(agent_cfg: dict) -> list[str]:
    template = CONFIG["command_template"]
    port = str(agent_cfg["port"])
    user_data_dir = agent_cfg["user_data_dir"]
    return [a.format(port=port, user_data_dir=user_data_dir) for a in template]


def spawn_agent(name: str) -> tuple[int, str]:
    """Start or reuse the Playwright child for agent. Returns (status, body)."""
    agent_cfg = CONFIG.get("agents", {}).get(name)
    if not agent_cfg:
        log.info("start %s: no config, no-op", name)
        return 204, ""

    with STATE_LOCK:
        existing = STATE.get(name)
        if existing and existing.poll() is None:
            log.info("start %s: already running pid=%d", name, existing.pid)
            if wait_for_port("127.0.0.1", agent_cfg["port"], timeout=1.0):
                return 200, json.dumps({"status": "already-running", "pid": existing.pid})

        Path(agent_cfg["user_data_dir"]).mkdir(parents=True, exist_ok=True)
        cmd = build_command(agent_cfg)
        log.info("start %s: spawning %s", name, " ".join(cmd))
        child = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
        )
        STATE[name] = child

    ok = wait_for_port("127.0.0.1", agent_cfg["port"], timeout=15.0)
    if not ok:
        log.warning("start %s: port %d did not come up", name, agent_cfg["port"])
        return 504, json.dumps({"status": "timeout", "pid": child.pid})
    log.info("start %s: ready on port %d (pid=%d)", name, agent_cfg["port"], child.pid)
    return 200, json.dumps({"status": "started", "pid": child.pid, "port": agent_cfg["port"]})


def stop_agent(name: str) -> tuple[int, str]:
    with STATE_LOCK:
        child = STATE.pop(name, None)
    if not child:
        return 200, json.dumps({"status": "not-running"})
    if child.poll() is not None:
        return 200, json.dumps({"status": "already-exited"})
    try:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
    except Exception:
        log.exception("stop %s: error", name)
    log.info("stop %s: terminated pid=%d", name, child.pid)
    return 200, json.dumps({"status": "stopped"})


def status() -> str:
    out: dict[str, dict] = {}
    with STATE_LOCK:
        for name, child in STATE.items():
            out[name] = {
                "pid": child.pid,
                "alive": child.poll() is None,
                "port": CONFIG.get("agents", {}).get(name, {}).get("port"),
            }
    return json.dumps(out)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str = "") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_POST(self) -> None:
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] in ("start", "stop"):
            action, name = parts
            code, body = spawn_agent(name) if action == "start" else stop_agent(name)
            self._send(code, body)
            return
        self._send(404, json.dumps({"error": "unknown path"}))

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/status":
            self._send(200, status())
            return
        self._send(404, json.dumps({"error": "unknown path"}))

    def log_message(self, fmt: str, *args) -> None:  # route through logging
        log.info("%s - %s", self.address_string(), fmt % args)


def shutdown(*_args) -> None:
    log.info("shutdown: killing %d children", len(STATE))
    with STATE_LOCK:
        for name, child in list(STATE.items()):
            if child.poll() is None:
                try:
                    child.terminate()
                except Exception:
                    pass
    sys.exit(0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="path to bridge config JSON")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    global CONFIG
    CONFIG = json.loads(Path(args.config).read_text())

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    host = CONFIG.get("listen_host", "0.0.0.0")
    port = int(CONFIG.get("listen_port", 9500))
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("bridge listening on %s:%d", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run tests to verify they pass**

```
cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_playwright_bridge.py -v
```
Expected: all four tests pass.

- [ ] **Step 4: Commit**

```
git add scripts/playwright-bridge/bridge.py scripts/playwright-bridge/config.example.json
git commit -m "feat(scripts): Playwright bridge daemon"
```

---

## Task 6: launchd plist template + README

**Files:**
- Create: `scripts/playwright-bridge/com.asempruch.playwright-bridge.plist.template`
- Create: `scripts/playwright-bridge/README.md`

- [ ] **Step 1: Write plist template**

```
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.asempruch.playwright-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>python3</string>
        <string>/ABSOLUTE/PATH/TO/scripts/playwright-bridge/bridge.py</string>
        <string>--config</string>
        <string>/ABSOLUTE/PATH/TO/playwright-bridge-config.json</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOURNAME/Library/Logs/playwright-bridge/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOURNAME/Library/Logs/playwright-bridge/stderr.log</string>
</dict>
</plist>
```

- [ ] **Step 2: Write README**

`scripts/playwright-bridge/README.md`:

````markdown
# Playwright Bridge

Host-side daemon that spawns a per-agent `@playwright/mcp` child with its own `--user-data-dir`. Called by bot lifecycle hooks.

## Install

1. Copy `config.example.json` somewhere host-local (e.g. `~/docker/claub/playwright-bridge-config.json`) and edit agent → port / profile-dir entries.

2. Copy `com.asempruch.playwright-bridge.plist.template` to `~/Library/LaunchAgents/com.asempruch.playwright-bridge.plist` and replace both `/ABSOLUTE/PATH/TO/...` placeholders plus `YOURNAME`.

3. `mkdir -p ~/Library/Logs/playwright-bridge`

4. `launchctl load ~/Library/LaunchAgents/com.asempruch.playwright-bridge.plist`

## Wire into the bot

In `/claub/config/agents.yaml`, add at the top level:

```yaml
on_start:
  - "curl -fsS --max-time 20 -X POST http://host.docker.internal:9500/start/$CLAUB_AGENT_NAME || true"
on_stop:
  - "curl -fsS --max-time 10 -X POST http://host.docker.internal:9500/stop/$CLAUB_AGENT_NAME || true"
```

The trailing `|| true` prevents a flaky bridge from blocking agent startup — the bot logs a warning but proceeds.

In each agent's `.mcp.json` (per-agent, not shared), add:

```json
{
  "mcpServers": {
    "playwright": {
      "type": "http",
      "url": "http://host.docker.internal:<PORT>/mcp"
    }
  }
}
```

Where `<PORT>` matches the port in the bridge config for that agent.

Remove the `playwright` entry from the shared `/claub/config/mcp.json`.

## Ops

- Logs: `~/Library/Logs/playwright-bridge/stderr.log`
- Status: `curl http://127.0.0.1:9500/status`
- Restart: `launchctl kickstart -k gui/$(id -u)/com.asempruch.playwright-bridge`

## Why this exists

See `CLAUDE.md` — "Playwright MCP (Host-Side)" section.
````

- [ ] **Step 3: Commit**

```
git add scripts/playwright-bridge/com.asempruch.playwright-bridge.plist.template scripts/playwright-bridge/README.md
git commit -m "docs(bridge): launchd template and setup readme"
```

---

## Task 7: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

Update the "Playwright MCP (Host-Side)" section to describe the new architecture: lifecycle hooks, bridge daemon, per-agent profiles, and per-agent `.mcp.json`. Replace the existing description of the single shared plist on port 3846.

- [ ] **Step 1: Find the existing Playwright section**

```
cd /Users/you/Claude && grep -n "Playwright" CLAUDE.md
```

- [ ] **Step 2: Rewrite the section**

Replace the "Playwright MCP (Host-Side)" subsection with text covering:

1. Each agent gets its own persistent browser profile (cookies, logins) via a `--user-data-dir` per agent.
2. A host-side Python bridge daemon (`scripts/playwright-bridge/bridge.py`) spawns / kills the per-agent Playwright MCP children on demand. Managed by launchd (`com.asempruch.playwright-bridge`).
3. The bot triggers start/stop via generic `on_start` / `on_stop` hooks in `agents.yaml` (see new "Lifecycle Hooks" subsection) — the bot itself is Playwright-agnostic.
4. Each agent's `.mcp.json` points at its own Playwright port. Shared `mcp.json` does NOT include `playwright`.
5. Profile dirs live at `~/docker/claub/playwright-profiles/{agent}/`.
6. Keep the existing notes about `--allow-unrestricted-file-access`, the host-path double-mount for `browser_file_upload`, and `CLAUB_HOST_PATH` — they still apply.
7. Mention the empirical finding: Claude Code's `SessionStart` hook does NOT block MCP connection, which is why lifecycle is wired through `on_start`/`on_stop` in the bot (pre-exec) rather than Claude's hooks.

Also add a new top-level subsection after "agents.yaml":

**`on_start` / `on_stop` lifecycle hooks**

Explain the generic feature: `AgentProcess` runs shell commands before spawning claude (`on_start`) and after it exits (`on_stop`). Hooks run sequentially, each with a 15s default timeout; failures log a warning and don't block. Both top-level and per-agent fields accepted; **per-agent hooks are additive** on top of global (matches `allowed_skills` precedent). Example:

```yaml
on_start:
  - "curl -fsS --max-time 20 -X POST http://host.docker.internal:9500/start/$CLAUB_AGENT_NAME || true"
on_stop:
  - "curl -fsS --max-time 10 -X POST http://host.docker.internal:9500/stop/$CLAUB_AGENT_NAME || true"
```

`$CLAUB_AGENT_NAME` is set in the agent process env.

- [ ] **Step 3: Commit**

```
git add CLAUDE.md
git commit -m "docs: describe lifecycle hooks and per-agent Playwright bridge"
```

---

## Task 8: Update `example/` config to match

**Files:**
- Modify: `example/config/agents.yaml`
- Modify: `example/config/mcp.json`
- Modify or create: `example/config/agents/*.mcp.json`

- [ ] **Step 1: Add top-level hooks to `example/config/agents.yaml`**

```yaml
on_start:
  - "curl -fsS --max-time 20 -X POST http://host.docker.internal:9500/start/$CLAUB_AGENT_NAME || true"
on_stop:
  - "curl -fsS --max-time 10 -X POST http://host.docker.internal:9500/stop/$CLAUB_AGENT_NAME || true"
```

- [ ] **Step 2: Remove `playwright` from `example/config/mcp.json`**

Edit the file; leave `schedules`, `git`, `nextcloud` etc. intact.

- [ ] **Step 3: Add or create per-agent `.mcp.json`**

For each agent in the example that uses Playwright, ensure its `.mcp.json` contains (merging with any existing entries):

```json
{
  "mcpServers": {
    "playwright": {
      "type": "http",
      "url": "http://host.docker.internal:3846/mcp"
    }
  }
}
```

(Use sequential ports per the README for extra agents.)

- [ ] **Step 4: Commit**

```
git add example/
git commit -m "docs(example): per-agent Playwright mcp, global lifecycle hooks"
```

---

## Rollout on the dev host (not version-controlled)

These steps apply the change to the user's live instance.

- [ ] **Write bridge config**

```
cp /Users/you/Claude/scripts/playwright-bridge/config.example.json /Users/you/docker/claub/playwright-bridge-config.json
```
Edit: ensure the six current agents (main, journalist, career, shopping-assistant, planner, leetcode-coach) each have a port assigned. leetcode-coach doesn't need browsing but costs nothing to include.

- [ ] **Create profile dirs**

```
mkdir -p /Users/you/docker/claub/playwright-profiles/{main,journalist,career,shopping-assistant,planner,leetcode-coach}
mkdir -p /Users/you/Library/Logs/playwright-bridge
```

- [ ] **Install the plist**

```
cp /Users/you/Claude/scripts/playwright-bridge/com.asempruch.playwright-bridge.plist.template \
   /Users/you/Library/LaunchAgents/com.asempruch.playwright-bridge.plist
# Edit placeholders: /ABSOLUTE/PATH -> /Users/you/Claude/scripts/playwright-bridge (and bridge config path)
#                    YOURNAME -> you
launchctl load /Users/you/Library/LaunchAgents/com.asempruch.playwright-bridge.plist
curl -s http://127.0.0.1:9500/status   # expect {}
```

- [ ] **Retire the old single-instance Playwright plist**

```
launchctl unload /Users/you/Library/LaunchAgents/com.asempruch.playwright-mcp.plist
# Keep the file archived for now, or delete:
#   rm /Users/you/Library/LaunchAgents/com.asempruch.playwright-mcp.plist
```

- [ ] **Edit `/Users/you/docker/claub/config/agents.yaml`**

Add top-level `on_start` / `on_stop` (see example config).

- [ ] **Edit `/Users/you/docker/claub/config/mcp.json`**

Remove the `"playwright"` entry.

- [ ] **Create / edit per-agent `.mcp.json`**

For each agent in `/Users/you/docker/claub/config/agents/`:
- `main.mcp.json`, `journalist.mcp.json`, `shopping-assistant.mcp.json`, `planner.mcp.json` — create with just the `playwright` entry at that agent's port.
- `career.mcp.json`, `leetcode-coach.mcp.json` — merge `playwright` into the existing `mcpServers`.

- [ ] **Restart the bot**

```
cd /Users/you/Claude && docker compose up -d --build
docker compose logs --tail 50
```

Verify the bot starts cleanly; look for the `on_start` log lines when an agent wakes up.

---

## Verification

- [ ] **Basic bridge reachability from container**

```
docker exec claude-claub-1 curl -fsS http://host.docker.internal:9500/status
```
Expected: valid JSON.

- [ ] **Hook fires on agent start**

In a Discord channel for one of the agents, send a trivial message. Tail logs:

```
docker compose logs -f --tail 0 | grep -E "on_start|on_stop"
```
Expected: `Running on_start hook for <agent>: curl ...` followed by `on_start hook ok`.

- [ ] **Playwright MCP comes up with a persistent profile**

```
curl http://127.0.0.1:9500/status
```
Expected: that agent shows `alive: true` and the correct port.

- [ ] **Login persistence E2E**

1. Pick one agent (e.g. `career`). Ask it to navigate to a login-gated site (e.g. linkedin.com) and log in interactively (the browser window appears on your desktop).
2. `/clear` in that channel, wait for idle reap (or `docker compose restart`), then ask the agent to visit the same site.
3. Expected: still logged in.

- [ ] **Isolation between agents**

1. Log `career` into a site.
2. Send `main` to the same site.
3. Expected: `main` is NOT logged in — separate profile.

- [ ] **Retired plist is gone**

```
launchctl list | grep playwright
```
Expected: `com.asempruch.playwright-bridge` present, `com.asempruch.playwright-mcp` absent.

---

## Self-Review Notes

- **Spec coverage:** lifecycle hooks (config, execution, wiring) = Tasks 1–3; bridge = Tasks 4–6; docs = Task 7; examples = Task 8; rollout = separate section; verification covers login persistence + isolation.
- **No bot-side Playwright knowledge** — grep for "playwright" after Task 3 should return zero hits in `bot/src/`.
- **Hook failure is non-fatal** — consistent across Tasks 2 and the `|| true` in the example hook commands, so a bridge outage degrades gracefully rather than bricking the bot.
- **Port collisions:** bridge config lists ports per agent; port 3846 is reused for `main` (was the old shared port). Old plist must be unloaded before bridge starts, or the `main` agent's spawn will fail the port-wait and 504.
