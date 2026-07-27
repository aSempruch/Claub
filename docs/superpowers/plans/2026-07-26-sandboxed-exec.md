# Sandboxed Code Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give agents `mcp__sandbox__run(command)` / `mcp__sandbox__install(packages)` — arbitrary shell execution in a throwaway `claub-exec` Docker container that holds no secrets and mounts only the calling agent's workspace, spawned by a host-side bridge daemon (never a Docker socket in the bot container).

**Architecture:** Mirrors the existing `scripts/playwright-bridge/` daemon pattern exactly. Agent → `mcps/sandbox/server.py` (FastMCP, baked into the bot image) → `POST http://host.docker.internal:9501/exec/{agent}` with an `X-Exec-Secret` header → `scripts/exec-bridge/bridge.py` (stdlib `ThreadingHTTPServer`, launchd) → `docker run --rm --network none ... claub-exec bash -c "<command>"`. Artifacts land on the workspace bind mount, which is mounted at the **same** path (`/claub/workspaces/{agent}`) in both containers, so a `[FILE:...]` marker resolves without translation.

**Tech Stack:** Python 3.12 stdlib (bridge), `mcp[cli]` + `httpx` (sandbox MCP, matching `mcps/file-download/`), Docker (Colima, aarch64), pytest + pytest-asyncio. Spec: `docs/superpowers/specs/2026-07-26-sandboxed-exec-design.md`.

## Global Constraints

- **Phasing is non-negotiable.** Build the `claub-exec` image **first** (Task 1), before the bridge or MCP. aarch64 has **no wheels** for `pycairo`, `manimpango`, or `moderngl` — all build from source — and that is the only genuinely uncertain step. Do not reorder to a top-down sequence.
- **Host:** Colima on Virtualization.framework, aarch64, virtiofs, 4 GiB RAM (the binding constraint — memory, not CPU). Docker socket `unix:///Users/you/.colima/default/docker.sock`, context `colima`. Verified: Docker 28.4.0, `--tmpfs /tmp:size=Nm,exec` short-form accepted.
- **Sandbox `docker run` flags, verbatim and load-bearing:** `--rm --name claub-exec-{agent}-{uuid} --network none --read-only --tmpfs /tmp:size=256m,exec --cap-drop ALL --security-opt no-new-privileges --memory 1g --cpus 1.5 --pids-limit 256`, env `HOME=/tmp MPLCONFIGDIR=/tmp/mpl UV_CACHE_DIR=/claub/workspaces/{agent}/.uv-cache UV_PYTHON_DOWNLOADS=never PATH=/claub/workspaces/{agent}/.venv/bin:/usr/local/bin:/usr/bin:/bin`, mounts `-v {host_ws}/{agent}:/claub/workspaces/{agent}` **plus** `-v {host_ws}/{agent}/.claude:/claub/workspaces/{agent}/.claude:ro`, `-w /claub/workspaces/{agent}`, then `bash -c "<command>"` (**not** `bash -lc`).
- **`.claude/` read-only nested mount is the whole security fix.** Without it, injected code writes `{workspace}/.claude/settings.local.json` and the agent escalates its own permissions on its next turn. Two adversarial tests are load-bearing acceptance criteria: (1) `echo x > {workspace}/.claude/settings.local.json` → read-only filesystem; (2) `curl http://host.docker.internal:9500/status` → no network.
- **Network:** `run` → `--network none` (arbitrary code never has network). `install` → default bridge network, command shape fixed by the server to `uv pip install <names>` where each name matches `^[A-Za-z0-9._-]+(==[A-Za-z0-9._-]+)?$`. The agent supplies names, never flags.
- **Output caps:** bridge streams each of stdout/stderr with a **1 MiB hard ceiling** (never `capture_output=True` — `run("yes")` would OOM the host). MCP additionally truncates each to **4000 chars**, tail-biased, with an explicit truncation marker.
- **Timeout ordering** (strictly): `MCP_TOOL_TIMEOUT (existing, ≥1200000ms) > MCP HTTP client 600s > bridge total 540s (queue + exec) > exec 180s default / 600s max`. Queue wait = bridge total − exec timeout; exceeding it returns "sandbox busy, N ahead", not a block.
- **Secret threading** (`.envrc` → `docker-compose.yml` env → bot container → MCP `env` block; and separately into the host bridge `config.json`): pick a var name `EXEC_BRIDGE_SECRET`. `.envrc` overrides `.env`, so any `docker compose up -d` must be `source .envrc && docker compose up -d`.
- **Deploy semantics:** bot-code / Dockerfile / MCP changes → `docker compose up -d --build`; config-only changes (settings.json, CLAUDE.md, mcp.json, agents.yaml) → `docker compose restart`.
- **Manim pinned `manim==0.20.1`** (confirmed on PyPI, current latest) so phase-2 example scenes target a known API. `uv` pinned in the image.
- **Pilot agent: `leetcode-coach`** (in the `coaching` group, has a compose-injected `shared-data` submount — see Task 6 `extra_mounts`).
- **Repo-vs-host boundary:** repo changes live under `/Users/you/Claude` and are committed. **Instance config** (`~/docker/claub/config/`), the **launchd plist**, the **host bridge `config.json`**, `.envrc`, and the **Colima restart** are OUTSIDE the repo — they are applied by hand, documented in the bridge README, and must NOT be committed. Do not modify anything under `/Users/you/docker/claub/` as part of repo tasks; Task 6/7 call out the manual instance edits explicitly.
- `docs/` is gitignored — commit the plan and any doc under `docs/` with `git add -f`.
- Tests run with: `cd bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`. Sandbox integration tests (Task 7) are gated behind `CLAUB_SANDBOX_INTEGRATION=1` and require the built image + a running bridge + Docker, mirroring the existing `CLAUDE_INTEGRATION_TEST` convention.
- Work on branch `feat/sandboxed-exec` (create it in Task 1 Step 1).

---

## Prerequisite (host, outside the repo — do this before Task 1)

Raise the Colima VM allocation 2 → 4 CPU. This roughly halves render times and is free; it restarts **every** container including all three Claub bots, so pick the moment.

- [ ] **P1: Restart Colima with 4 CPU**

Run:
```bash
colima stop && colima start --cpu 4 --memory 4
```
Expected: `colima status` reports running, `arch: aarch64`, `mountType: virtiofs`. Then:
```bash
docker context use colima && docker info --format '{{.NCPU}} {{.MemTotal}}'
```
Expected: `4` CPUs, ~4 GiB memory.

- [ ] **P2: Bring the bots back up** (Colima restart stops them; `restart: unless-stopped` restores them, but confirm):
```bash
cd /Users/you/Claude && source .envrc && docker compose up -d
docker compose ps
```
Expected: `claude-claub-1` is `Up`.

This step is manual host state — nothing to commit.

---

### Task 1: Build and smoke-test the `claub-exec` image (DO THIS FIRST)

The only genuinely uncertain step: aarch64 source builds for `pycairo`, `manimpango`, and `moderngl`. Surface it while nothing depends on it.

**Files:**
- Create: `docker/exec-sandbox/Dockerfile`
- Create: `docker/exec-sandbox/README.md` (build + smoke-test instructions)

**Interfaces:**
- Produces: a local Docker image tagged `claub-exec` containing Python 3.12, `ffmpeg`, a full C build toolchain, `uv` (pinned), and baked `manim==0.20.1 numpy matplotlib networkx pillow`. Consumed by the bridge (Task 3) as the `image` config value.

- [ ] **Step 1: Create the branch**

```bash
cd /Users/you/Claude && git checkout -b feat/sandboxed-exec
```

- [ ] **Step 2: Write the Dockerfile**

Create `docker/exec-sandbox/Dockerfile`:

```dockerfile
# Throwaway execution sandbox for Claub agents. Built by hand (see README),
# never referenced by docker-compose.yml — only ever `docker run --rm`.
#
# The build toolchain is MANDATORY on aarch64, not optional polish: pycairo and
# manimpango publish NO Linux wheels, and moderngl publishes none for aarch64,
# so all three compile from source both at bake time AND whenever `install`
# pulls an sdist-only package at runtime. It stays in the final image.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        build-essential \
        pkg-config \
        libcairo2-dev \
        libpango1.0-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# uv, pinned (bump deliberately, never floating).
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

# Bake the common scientific/animation stack into the system interpreter so a
# workspace venv created with --system-site-packages sees them for free.
# manim pinned so phase-2 example scenes target a known ManimCE API.
RUN uv pip install --system --break-system-packages \
        manim==0.20.1 \
        numpy \
        matplotlib \
        networkx \
        pillow

# No LaTeX: MathTex/Tex need ~1 GB of texlive. The skill uses Text/MarkupText.
# No default CMD — the bridge always passes `bash -c "<command>"`.
```

- [ ] **Step 3: Build the image**

Run:
```bash
docker build -t claub-exec /Users/you/Claude/docker/exec-sandbox/
```
Expected: `naming to docker.io/library/claub-exec` success. **This is where the aarch64 source builds happen** — if `pycairo`/`manimpango`/`moderngl` fail, that surfaces here. If a source build fails for a missing dev header, add the corresponding `-dev` apt package and rebuild; do not proceed until the build is clean.

- [ ] **Step 4: Verify the baked stack imports and manim is the pinned version**

Run:
```bash
docker run --rm claub-exec python -c "import manim, numpy, matplotlib, networkx, PIL; print(manim.__version__)"
```
Expected: prints `0.20.1` with no ImportError.

- [ ] **Step 5: Verify a real Manim render works inside the sandbox flags**

This exercises the exact `docker run` flags the bridge will use (read-only root, tmpfs, network none, workspace mount), so a flag incompatibility surfaces now, not in Task 3.

Run:
```bash
mkdir -p /tmp/exec-smoke/.claude && cat > /tmp/exec-smoke/t.py <<'PY'
from manim import Scene, Text, Write
class T(Scene):
    def construct(self):
        self.play(Write(Text("ok")))
PY
docker run --rm --network none --read-only \
  --tmpfs /tmp:size=256m,exec --cap-drop ALL --security-opt no-new-privileges \
  --memory 1g --cpus 1.5 --pids-limit 256 \
  -e HOME=/tmp -e MPLCONFIGDIR=/tmp/mpl \
  -v /tmp/exec-smoke:/claub/workspaces/smoke \
  -w /claub/workspaces/smoke \
  claub-exec bash -c "manim -ql t.py T && ls media/videos/t/480p15/T.mp4"
```
Expected: render completes and the final line prints `media/videos/t/480p15/T.mp4`. Confirms: `-ql` renders under 1g/1.5cpu, the read-only root + tmpfs are compatible with Manim's scratch writes, and output lands on the host-backed mount (`ls /tmp/exec-smoke/media/videos/t/480p15/T.mp4` on the host also succeeds).

- [ ] **Step 6: Write the image README**

Create `docker/exec-sandbox/README.md`:

```markdown
# claub-exec — agent execution sandbox image

Throwaway container image for `mcp__sandbox__run` / `install`. Built by hand,
never in `docker-compose.yml` — only ever `docker run --rm` from the exec bridge.

## Build

    docker build -t claub-exec docker/exec-sandbox/

Rebuild after editing the Dockerfile or bumping a pin. On aarch64 the first
build compiles `pycairo`, `manimpango`, and `moderngl` from source (no wheels
exist) — expect a few minutes. The build toolchain stays in the final image
because `install` also needs a compiler at runtime for sdist-only packages.

## Smoke test

    docker run --rm claub-exec python -c "import manim; print(manim.__version__)"   # 0.20.1

## Pins

- `manim==0.20.1` (phase-2 example scenes target this API)
- `uv` 0.11.32
- No LaTeX (MathTex/Tex excluded to save ~1 GB — the skill uses Text/MarkupText)
```

- [ ] **Step 7: Commit**

```bash
git add -A docker/exec-sandbox/
git commit -m "feat(sandbox): claub-exec image with manim + aarch64 build toolchain"
```

---

### Task 2: Bridge pure helpers — validation, argv construction, output capping

Split the testable pure logic out of the HTTP/subprocess I/O (the `file-download` helpers/server pattern), so it unit-tests with no Docker.

**Files:**
- Create: `scripts/exec-bridge/helpers.py`
- Test: `bot/tests/test_exec_bridge_helpers.py`

**Interfaces:**
- Produces:
  - `AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")`
  - `validate_agent(name: str, allowed: list[str]) -> None` — raises `ValueError` if `name` fails the regex or is not in `allowed`. (Caller maps `ValueError` → HTTP 404, never 400 — no info about which agents exist.)
  - `clamp_timeout(requested: int | None, default: int, maximum: int) -> int`
  - `build_docker_argv(agent, command, cfg, network, name) -> list[str]` — the full `docker run` argv per the Global Constraints, including the nested read-only `.claude` mount and any `cfg["agents"][agent]["extra_mounts"]`. `cfg` carries `docker_bin`, `image`, `workspaces_root`. `network` is `"none"` or `"bridge"`.
  - `cap_stream(chunks: Iterable[bytes], limit: int) -> tuple[bytes, bool]` — accumulate up to `limit` bytes, return `(data, truncated)`.

- [ ] **Step 1: Write the failing tests**

Create `bot/tests/test_exec_bridge_helpers.py`:

```python
"""Unit tests for the exec-bridge pure helpers (no Docker required)."""
import importlib.util
import os

import pytest

# Load helpers.py under a unique module name (several bridges/MCPs ship a
# helpers.py; a bare import would collide) — same pattern as
# test_file_download_mcp.py.
_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "exec-bridge")
_spec = importlib.util.spec_from_file_location(
    "exec_bridge_helpers", os.path.join(_DIR, "helpers.py")
)
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)


def _cfg(**over):
    cfg = {
        "docker_bin": "docker",
        "image": "claub-exec",
        "workspaces_root": "/host/ws",
        "agents": {"leetcode-coach": {}},
    }
    cfg.update(over)
    return cfg


# --- validate_agent ---

def test_validate_agent_accepts_allowed():
    _h.validate_agent("leetcode-coach", ["leetcode-coach", "main"])  # no raise


def test_validate_agent_rejects_traversal():
    with pytest.raises(ValueError):
        _h.validate_agent("../main", ["main"])


def test_validate_agent_rejects_slash():
    with pytest.raises(ValueError):
        _h.validate_agent("a/b", ["a/b"])  # slash fails the regex even if "listed"


def test_validate_agent_rejects_unknown():
    with pytest.raises(ValueError):
        _h.validate_agent("ghost", ["main"])


# --- clamp_timeout ---

def test_clamp_timeout_default_when_none():
    assert _h.clamp_timeout(None, 180, 600) == 180


def test_clamp_timeout_caps_at_max():
    assert _h.clamp_timeout(9999, 180, 600) == 600


def test_clamp_timeout_floors_nonpositive():
    assert _h.clamp_timeout(0, 180, 600) == 180
    assert _h.clamp_timeout(-5, 180, 600) == 180


# --- build_docker_argv ---

def test_build_docker_argv_run_has_network_none_and_readonly_claude():
    argv = _h.build_docker_argv(
        "leetcode-coach", "echo hi", _cfg(), network="none", name="claub-exec-x"
    )
    assert argv[0] == "docker" and argv[1] == "run"
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert "--cap-drop" in argv and "--security-opt" in argv
    # workspace mount at its bot-container path
    assert "-v" in argv
    joined = " ".join(argv)
    assert "/host/ws/leetcode-coach:/claub/workspaces/leetcode-coach" in joined
    # nested read-only .claude mount — the load-bearing escalation block
    assert "/host/ws/leetcode-coach/.claude:/claub/workspaces/leetcode-coach/.claude:ro" in joined
    # bash -c, not bash -lc
    assert argv[-3:] == ["bash", "-c", "echo hi"]
    assert "-lc" not in argv


def test_build_docker_argv_install_uses_bridge_network():
    argv = _h.build_docker_argv(
        "leetcode-coach", "uv pip install rich", _cfg(), network="bridge", name="n"
    )
    assert argv[argv.index("--network") + 1] == "bridge"


def test_build_docker_argv_includes_extra_mounts():
    cfg = _cfg(agents={"leetcode-coach": {"extra_mounts": [
        "/Users/you/repos/shared/data:/claub/workspaces/leetcode-coach/shared-data"
    ]}})
    argv = _h.build_docker_argv("leetcode-coach", "true", cfg, "none", "n")
    assert "/Users/you/repos/shared/data:/claub/workspaces/leetcode-coach/shared-data" in argv


def test_build_docker_argv_respects_docker_bin():
    argv = _h.build_docker_argv("leetcode-coach", "true", _cfg(docker_bin="/fake/docker"), "none", "n")
    assert argv[0] == "/fake/docker"


# --- cap_stream ---

def test_cap_stream_under_limit_not_truncated():
    data, truncated = _h.cap_stream([b"abc", b"def"], 1024)
    assert data == b"abcdef" and truncated is False


def test_cap_stream_over_limit_truncates():
    data, truncated = _h.cap_stream([b"x" * 10, b"y" * 10], 15)
    assert len(data) == 15 and truncated is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd bot && uv run --extra dev pytest tests/test_exec_bridge_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError` / `FileNotFoundError` for `scripts/exec-bridge/helpers.py`.

- [ ] **Step 3: Implement**

Create `scripts/exec-bridge/helpers.py`:

```python
"""Pure helpers for the exec bridge — no I/O, unit-testable without Docker."""
from __future__ import annotations

import re
from collections.abc import Iterable

AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_agent(name: str, allowed: list[str]) -> None:
    """Raise ValueError unless *name* matches the safe pattern AND is allowed.

    The name becomes part of a bind-mount path, so it is the one untrusted
    input. The caller maps ValueError to HTTP 404 (not 400) — no information
    about which agents exist.
    """
    if not AGENT_NAME_RE.match(name) or name not in allowed:
        raise ValueError(f"unknown agent: {name!r}")


def clamp_timeout(requested: int | None, default: int, maximum: int) -> int:
    if not requested or requested <= 0:
        return default
    return min(int(requested), maximum)


def build_docker_argv(
    agent: str,
    command: str,
    cfg: dict,
    network: str,
    name: str,
) -> list[str]:
    """Full `docker run` argv. `network` is 'none' (run) or 'bridge' (install)."""
    ws_root = cfg["workspaces_root"].rstrip("/")
    host_ws = f"{ws_root}/{agent}"
    cont_ws = f"/claub/workspaces/{agent}"
    argv = [
        cfg.get("docker_bin", "docker"), "run", "--rm",
        "--name", name,
        "--network", network,
        "--read-only",
        "--tmpfs", "/tmp:size=256m,exec",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", "1g", "--cpus", "1.5", "--pids-limit", "256",
        "-e", "HOME=/tmp",
        "-e", "MPLCONFIGDIR=/tmp/mpl",
        "-e", f"UV_CACHE_DIR={cont_ws}/.uv-cache",
        "-e", "UV_PYTHON_DOWNLOADS=never",
        "-e", f"PATH={cont_ws}/.venv/bin:/usr/local/bin:/usr/bin:/bin",
        "-v", f"{host_ws}:{cont_ws}",
        # Read-only nested mount: blocks writing {workspace}/.claude/settings.local.json,
        # which is the self-escalation path the sandbox exists to prevent. Skills
        # still work — .claude/skills is a symlink to ../.claude-skills on the
        # writable parent mount.
        "-v", f"{host_ws}/.claude:{cont_ws}/.claude:ro",
    ]
    for mount in cfg.get("agents", {}).get(agent, {}).get("extra_mounts", []):
        argv += ["-v", mount]
    argv += ["-w", cont_ws, cfg["image"], "bash", "-c", command]
    return argv


def cap_stream(chunks: Iterable[bytes], limit: int) -> tuple[bytes, bool]:
    """Accumulate up to *limit* bytes; return (data, truncated)."""
    buf = bytearray()
    truncated = False
    for chunk in chunks:
        if len(buf) >= limit:
            truncated = True
            break
        room = limit - len(buf)
        if len(chunk) > room:
            buf += chunk[:room]
            truncated = True
            break
        buf += chunk
    return bytes(buf), truncated
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bot && uv run --extra dev pytest tests/test_exec_bridge_helpers.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/exec-bridge/helpers.py bot/tests/test_exec_bridge_helpers.py
git commit -m "feat(exec-bridge): pure helpers for agent validation and docker argv"
```

---

### Task 3: Bridge daemon — HTTP server, container exec, timeout/reap, config, plist, README

**Files:**
- Create: `scripts/exec-bridge/bridge.py`
- Create: `scripts/exec-bridge/config.example.json`
- Create: `scripts/exec-bridge/com.asempruch.exec-bridge.plist.template`
- Create: `scripts/exec-bridge/README.md`
- Test: `bot/tests/test_exec_bridge.py`

**Interfaces:**
- Consumes: `helpers.py` (Task 2).
- Produces: a daemon listening on `listen_port` (default 9501) with `POST /exec/{agent}` (header `X-Exec-Secret`, body `{"command": str, "timeout": int}`, response `{"exit_code","stdout","stdout_truncated","stderr","stderr_truncated","timed_out","duration_s"}`) and `GET /status`. On startup it reaps orphaned `claub-exec-*` containers. Consumed by the MCP (Task 5) over HTTP.

**Testability note.** The bridge shells out to `docker`. Tests point `docker_bin` at a **fake docker** shell script (a tempfile), so the HTTP path, secret check, agent validation, timeout, and reaping are all exercised hermetically without a real container — same spirit as `test_playwright_bridge.py`'s stub command_template.

- [ ] **Step 1: Write the failing tests**

Create `bot/tests/test_exec_bridge.py`:

```python
"""Integration tests for the exec bridge using a FAKE docker binary."""
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[2] / "scripts" / "exec-bridge" / "bridge.py"


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
        except urllib.error.HTTPError:
            return  # any HTTP response means it's listening
        except Exception:
            time.sleep(0.05)
    raise TimeoutError(url)


def post(url: str, body: dict, headers: dict, timeout: float = 20.0):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


FAKE_DOCKER = """#!/usr/bin/env python3
import sys, time, os
args = sys.argv[1:]
# `docker run ... bash -c "<command>"` — emulate by echoing a marker so tests
# can assert flags were passed, and honor a couple of magic commands.
if args and args[0] == "run":
    cmd = args[-1]
    if cmd == "SLEEP":
        time.sleep(30)          # exceed the test's exec timeout
    if cmd == "FLOOD":
        sys.stdout.write("x" * (2 * 1024 * 1024)); sys.exit(0)
    sys.stdout.write("ran: " + cmd + "\\n")
    sys.stdout.write("flags: " + " ".join(args) + "\\n")
    sys.exit(0)
if args and args[0] == "rm":
    # record that a container was force-removed
    open(os.environ["FAKE_DOCKER_RM_LOG"], "a").write(" ".join(args) + "\\n")
    sys.exit(0)
if args and args[0] == "ps":
    sys.exit(0)   # no orphans
sys.exit(0)
"""


@pytest.fixture
def bridge(tmp_path: Path):
    port = free_port()
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rm_log = tmp_path / "rm.log"
    ws = tmp_path / "ws" / "leetcode-coach"
    (ws / ".claude").mkdir(parents=True)

    cfg = {
        "listen_host": "127.0.0.1", "listen_port": port,
        "workspaces_root": str(tmp_path / "ws"),
        "image": "claub-exec", "docker_bin": str(fake),
        "secret": "s3cret", "max_concurrent": 1,
        "default_timeout": 2, "max_timeout": 5, "bridge_total_timeout": 4,
        "agents": {"leetcode-coach": {}},
    }
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))

    env = {**os.environ, "FAKE_DOCKER_RM_LOG": str(rm_log)}
    proc = subprocess.Popen([sys.executable, str(BRIDGE), "--config", str(cfg_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        wait_http(f"http://127.0.0.1:{port}/status")
        yield f"http://127.0.0.1:{port}", rm_log
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_exec_happy_path(bridge):
    base, _ = bridge
    code, body = post(f"{base}/exec/leetcode-coach", {"command": "echo hi"},
                      {"X-Exec-Secret": "s3cret"})
    assert code == 200
    assert body["exit_code"] == 0
    assert "ran: echo hi" in body["stdout"]
    assert body["timed_out"] is False


def test_exec_flags_include_network_none_and_readonly_claude(bridge):
    base, _ = bridge
    _, body = post(f"{base}/exec/leetcode-coach", {"command": "true"},
                   {"X-Exec-Secret": "s3cret"})
    assert "--network none" in body["stdout"]
    assert ".claude:/claub/workspaces/leetcode-coach/.claude:ro" in body["stdout"]


def test_exec_missing_secret_rejected(bridge):
    base, _ = bridge
    code, _ = post(f"{base}/exec/leetcode-coach", {"command": "echo hi"}, {})
    assert code == 401


def test_exec_wrong_secret_rejected(bridge):
    base, _ = bridge
    code, _ = post(f"{base}/exec/leetcode-coach", {"command": "echo hi"},
                   {"X-Exec-Secret": "nope"})
    assert code == 401


def test_exec_unknown_agent_404(bridge):
    base, _ = bridge
    code, _ = post(f"{base}/exec/ghost", {"command": "echo hi"},
                   {"X-Exec-Secret": "s3cret"})
    assert code == 404


def test_exec_traversal_agent_404(bridge):
    base, _ = bridge
    # urllib will normalize some traversal, so hit the handler with an encoded name
    code, _ = post(f"{base}/exec/..%2Fmain", {"command": "echo hi"},
                   {"X-Exec-Secret": "s3cret"})
    assert code == 404


def test_exec_timeout_kills_and_reaps(bridge):
    base, rm_log = bridge
    _, body = post(f"{base}/exec/leetcode-coach", {"command": "SLEEP", "timeout": 1},
                   {"X-Exec-Secret": "s3cret"}, timeout=20)
    assert body["timed_out"] is True
    # bridge issued `docker rm -f <name>` for the timed-out container
    assert rm_log.exists() and "rm -f claub-exec-leetcode-coach-" in rm_log.read_text()


def test_exec_output_capped_at_bridge(bridge):
    base, _ = bridge
    _, body = post(f"{base}/exec/leetcode-coach", {"command": "FLOOD"},
                   {"X-Exec-Secret": "s3cret"}, timeout=20)
    assert body["stdout_truncated"] is True
    assert len(body["stdout"].encode()) <= 1024 * 1024


def test_status_ok(bridge):
    base, _ = bridge
    with urllib.request.urlopen(f"{base}/status", timeout=5) as resp:
        assert resp.status == 200
        json.loads(resp.read().decode())  # valid JSON
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd bot && uv run --extra dev pytest tests/test_exec_bridge.py -v`
Expected: FAIL — `bridge.py` does not exist yet (`FileNotFoundError` starting the subprocess / TimeoutError on `wait_http`).

- [ ] **Step 3: Implement the bridge**

Create `scripts/exec-bridge/bridge.py`:

```python
#!/usr/bin/env python3
"""Exec bridge daemon.

Runs an arbitrary command inside a throwaway `claub-exec` container per request,
mounting only the calling agent's workspace. Mirrors scripts/playwright-bridge/.
The Docker socket never enters the bot container — this daemon holds that
authority on the host, gated by a shared secret.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import build_docker_argv, cap_stream, clamp_timeout, validate_agent

log = logging.getLogger("exec-bridge")

CONFIG: dict = {}
SEM = threading.Semaphore(1)
RUNNING: dict[str, int] = {}
RUNNING_LOCK = threading.Lock()
STREAM_CAP = 1024 * 1024  # 1 MiB per stream — hard ceiling against OOM


def _read_capped(pipe, limit: int) -> tuple[bytes, bool]:
    def chunks():
        while True:
            data = pipe.read(65536)
            if not data:
                return
            yield data
    return cap_stream(chunks(), limit)


def run_container(argv: list[str], name: str, exec_timeout: int, docker_bin: str) -> dict:
    """Run the container, streaming output under a byte cap; kill on timeout."""
    import time
    start = time.monotonic()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    result: dict = {}
    out_holder: dict = {}

    def reader(pipe, key):
        out_holder[key] = _read_capped(pipe, STREAM_CAP)

    t_out = threading.Thread(target=reader, args=(proc.stdout, "out"))
    t_err = threading.Thread(target=reader, args=(proc.stderr, "err"))
    t_out.start(); t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=exec_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Kill the CONTAINER, not just the docker CLI: `docker rm -f` on the name.
        subprocess.run([docker_bin, "rm", "-f", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    t_out.join(); t_err.join()
    stdout, out_trunc = out_holder.get("out", (b"", False))
    stderr, err_trunc = out_holder.get("err", (b"", False))
    return {
        "exit_code": proc.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stdout_truncated": out_trunc,
        "stderr": stderr.decode(errors="replace"),
        "stderr_truncated": err_trunc,
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - start, 2),
    }


def handle_exec(agent: str, command: str, requested_timeout: int | None) -> tuple[int, dict]:
    allowed = list(CONFIG.get("agents", {}).keys())
    try:
        validate_agent(agent, allowed)
    except ValueError:
        return 404, {"error": "unknown agent"}

    exec_timeout = clamp_timeout(requested_timeout, CONFIG["default_timeout"], CONFIG["max_timeout"])
    # Queue wait is bounded separately: bridge_total - exec, so a queued call
    # fails fast with "sandbox busy" rather than blocking to the outer timeout.
    queue_wait = max(1, CONFIG.get("bridge_total_timeout", 540) - exec_timeout)
    if not SEM.acquire(timeout=queue_wait):
        with RUNNING_LOCK:
            ahead = sum(RUNNING.values())
        return 503, {"error": f"sandbox busy, {ahead} ahead — try again shortly"}

    name = f"claub-exec-{agent}-{uuid.uuid4().hex[:12]}"
    network = "bridge" if command.startswith("uv pip install ") else "none"
    argv = build_docker_argv(agent, command, CONFIG, network, name)
    with RUNNING_LOCK:
        RUNNING[agent] = RUNNING.get(agent, 0) + 1
    try:
        result = run_container(argv, name, exec_timeout, CONFIG.get("docker_bin", "docker"))
        return 200, result
    finally:
        with RUNNING_LOCK:
            RUNNING[agent] = max(0, RUNNING.get(agent, 1) - 1)
        SEM.release()


def reap_orphans() -> None:
    docker = CONFIG.get("docker_bin", "docker")
    try:
        out = subprocess.check_output(
            [docker, "ps", "-aq", "--filter", "name=claub-exec-"], text=True)
        ids = [i for i in out.split() if i]
        if ids:
            subprocess.run([docker, "rm", "-f", *ids],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("reaped %d orphaned container(s)", len(ids))
    except Exception as e:
        log.warning("orphan reap failed: %s", e)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        # Path is /exec/{agent}; unquote so encoded traversal is caught by the regex.
        from urllib.parse import unquote
        parts = [unquote(p) for p in self.path.strip("/").split("/")]
        if len(parts) == 2 and parts[0] == "exec":
            if self.headers.get("X-Exec-Secret", "") != CONFIG.get("secret"):
                self._send(401, {"error": "missing or invalid secret"})
                return
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            code, body = handle_exec(parts[1], payload.get("command", ""), payload.get("timeout"))
            self._send(code, body)
            return
        self._send(404, {"error": "unknown path"})

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/status":
            with RUNNING_LOCK:
                self._send(200, {"running": dict(RUNNING)})
            return
        self._send(404, {"error": "unknown path"})

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    global CONFIG, SEM
    CONFIG = json.loads(Path(args.config).read_text())
    SEM = threading.Semaphore(int(CONFIG.get("max_concurrent", 1)))
    reap_orphans()
    host = CONFIG.get("listen_host", "127.0.0.1")
    port = int(CONFIG.get("listen_port", 9501))
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("exec bridge listening on %s:%d", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bot && uv run --extra dev pytest tests/test_exec_bridge.py -v`
Expected: all PASS.

- [ ] **Step 5: Write the bridge config example**

Create `scripts/exec-bridge/config.example.json`:

```json
{
  "listen_host": "127.0.0.1",
  "listen_port": 9501,
  "workspaces_root": "/Users/you/docker/claub/workspaces",
  "image": "claub-exec",
  "docker_bin": "docker",
  "secret": "CHANGE_ME_MATCH_ENVRC_EXEC_BRIDGE_SECRET",
  "max_concurrent": 1,
  "default_timeout": 180,
  "max_timeout": 600,
  "bridge_total_timeout": 540,
  "agents": {
    "leetcode-coach": {
      "extra_mounts": [
        "/Users/you/repos/shared/data:/claub/workspaces/leetcode-coach/shared-data"
      ]
    }
  }
}
```

- [ ] **Step 6: Write the launchd plist template**

Create `scripts/exec-bridge/com.asempruch.exec-bridge.plist.template`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.asempruch.exec-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>python3</string>
        <string>/ABSOLUTE/PATH/TO/scripts/exec-bridge/bridge.py</string>
        <string>--config</string>
        <string>/ABSOLUTE/PATH/TO/exec-bridge-config.json</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <!-- The bridge shells out to `docker`, which resolves the colima
             context from ~/.docker/config.json (HOME-dependent). launchd envs
             are minimal, so pin the socket explicitly. -->
        <key>DOCKER_HOST</key>
        <string>unix:///Users/you/.colima/default/docker.sock</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOURNAME/Library/Logs/claub-exec-bridge/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOURNAME/Library/Logs/claub-exec-bridge/stderr.log</string>
</dict>
</plist>
```

- [ ] **Step 7: Write the bridge README**

Create `scripts/exec-bridge/README.md`:

```markdown
# Exec Bridge

Host-side daemon that spawns a throwaway `claub-exec` container per
`mcp__sandbox__run` / `install` call, mounting only the calling agent's
workspace. The Docker socket never enters the bot container — this daemon holds
that authority on the host. Mirrors `scripts/playwright-bridge/`.

## Prerequisites

- Build the image first: `docker build -t claub-exec docker/exec-sandbox/`
  (see `docker/exec-sandbox/README.md`).
- Colima running with `--cpu 4 --memory 4`.

## Install

1. Copy `config.example.json` host-local (e.g.
   `~/Library/Application Support/claub-exec-bridge/config.json`) and edit:
   - `workspaces_root`: the HOST path bind-mounted into the bot at `/claub/workspaces`.
   - `secret`: must equal `EXEC_BRIDGE_SECRET` in `.envrc` (see "Secret" below).
   - `agents`: allowlist — must agree with `agents.yaml`; a mismatch surfaces as a 404.
   - `extra_mounts` per agent: replicate any compose submount whose host source
     differs from `{workspace}/...`. **`leetcode-coach` needs the `shared-data`
     entry** — `docker-compose.yml` bind-mounts `/Users/you/repos/shared/data`
     into the bot at `{workspace}/shared-data`, but on the host that workspace
     subdir is empty. Without the extra mount the sandbox silently sees nothing.
     Any FUTURE compose submount must be added in BOTH places.

2. Copy the plist template to
   `~/Library/LaunchAgents/com.asempruch.exec-bridge.plist`, replace the
   `/ABSOLUTE/PATH/TO/...` placeholders and `YOURNAME`. The plist pins
   `DOCKER_HOST` to the colima socket — required, launchd envs are minimal.

3. Load:
   ```
   mkdir -p ~/Library/Logs/claub-exec-bridge
   launchctl load ~/Library/LaunchAgents/com.asempruch.exec-bridge.plist
   curl http://127.0.0.1:9501/status   # expect {"running": {}}
   ```

## Secret

The MCP sends `X-Exec-Secret`; the bridge checks it against `config.json`'s
`secret`. Thread the same value end to end: host `.envrc` (`export
EXEC_BRIDGE_SECRET=...`) → `docker-compose.yml` `environment` → the bot
container → the sandbox MCP's `env` block in the per-agent `.mcp.json`. The
secret is NEVER forwarded into the sandbox container (Docker gives a container
only the image ENV plus explicit `-e` flags), so sandbox code cannot reach the
bridge even though `install` has network.

## Network binding

`listen_host` is `127.0.0.1` (loopback). Colima forwards container-originated
`host.docker.internal:9501` through to host loopback. Do NOT set `0.0.0.0`.

## Housekeeping

`{workspace}/.venv`, `{workspace}/.uv-cache` accumulate per agent. Prune
manually or let the bot's startup sweep cover them.

## Ops

- Logs: `~/Library/Logs/claub-exec-bridge/stderr.log`
- Status: `curl http://127.0.0.1:9501/status`
- Restart: `launchctl kickstart -k gui/$(id -u)/com.asempruch.exec-bridge`
```

- [ ] **Step 8: Commit**

```bash
git add scripts/exec-bridge/
git commit -m "feat(exec-bridge): daemon, config example, launchd plist, README"
```

---

### Task 4: Sandbox MCP pure helpers — install name validation and output truncation

**Files:**
- Create: `mcps/sandbox/helpers.py`
- Test: `bot/tests/test_sandbox_mcp.py`

**Interfaces:**
- Produces:
  - `PACKAGE_RE = re.compile(r"^[A-Za-z0-9._-]+(==[A-Za-z0-9._-]+)?$")`
  - `validate_packages(packages: list[str]) -> list[str]` — returns the list unchanged if every name matches `PACKAGE_RE`; raises `ValueError` otherwise (catches `--index-url=...`, flags, empty).
  - `build_install_command(packages: list[str], venv_python: str) -> str` — the fixed `uv pip install --python {venv_python} <names>` string; the agent never supplies flags.
  - `truncate_tail(text: str, limit: int = 4000) -> str` — tail-biased truncation with an explicit leading marker when dropped.

- [ ] **Step 1: Write the failing tests**

Create `bot/tests/test_sandbox_mcp.py`:

```python
"""Unit tests for the sandbox MCP pure helpers (no bridge/Docker required)."""
import importlib.util
import os

import pytest

_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "mcps", "sandbox")
_spec = importlib.util.spec_from_file_location(
    "sandbox_helpers", os.path.join(_DIR, "helpers.py")
)
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)


# --- validate_packages ---

def test_validate_packages_accepts_plain_and_pinned():
    assert _h.validate_packages(["rich", "numpy==2.0.0"]) == ["rich", "numpy==2.0.0"]


def test_validate_packages_rejects_flag_injection():
    with pytest.raises(ValueError):
        _h.validate_packages(["--index-url=http://evil"])


def test_validate_packages_rejects_space_smuggling():
    with pytest.raises(ValueError):
        _h.validate_packages(["rich --upgrade"])


def test_validate_packages_rejects_empty_list():
    with pytest.raises(ValueError):
        _h.validate_packages([])


# --- build_install_command ---

def test_build_install_command_fixed_shape():
    cmd = _h.build_install_command(["rich", "numpy==2.0.0"],
                                   "/claub/workspaces/x/.venv/bin/python")
    assert cmd == ("uv pip install --python /claub/workspaces/x/.venv/bin/python "
                   "rich numpy==2.0.0")
    assert cmd.startswith("uv pip install ")  # bridge routes this to network=bridge


# --- truncate_tail ---

def test_truncate_tail_under_limit_unchanged():
    assert _h.truncate_tail("hello", 4000) == "hello"


def test_truncate_tail_over_limit_keeps_tail_and_marks():
    text = "".join(str(i % 10) for i in range(5000))
    out = _h.truncate_tail(text, 4000)
    assert len(out) <= 4000 + 80          # marker adds a little
    assert out.endswith(text[-100:])      # tail preserved
    assert "truncated" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd bot && uv run --extra dev pytest tests/test_sandbox_mcp.py -v`
Expected: FAIL — `mcps/sandbox/helpers.py` does not exist.

- [ ] **Step 3: Implement**

Create `mcps/sandbox/helpers.py`:

```python
"""Pure helpers for the sandbox MCP server — no I/O, unit-testable."""
from __future__ import annotations

import re

PACKAGE_RE = re.compile(r"^[A-Za-z0-9._-]+(==[A-Za-z0-9._-]+)?$")


def validate_packages(packages: list[str]) -> list[str]:
    """Return *packages* unchanged if every name is a bare (optionally pinned)
    distribution name. Raises ValueError otherwise — this is what stops an
    agent smuggling a flag such as --index-url through `install`.
    """
    if not packages:
        raise ValueError("no packages given")
    for name in packages:
        if not PACKAGE_RE.match(name):
            raise ValueError(f"invalid package name: {name!r}")
    return packages


def build_install_command(packages: list[str], venv_python: str) -> str:
    """The fixed install command shape. The server builds this — never the agent."""
    return f"uv pip install --python {venv_python} " + " ".join(packages)


def truncate_tail(text: str, limit: int = 4000) -> str:
    """Tail-biased truncation with an explicit marker when content is dropped."""
    if len(text) <= limit:
        return text
    marker = f"[... {len(text) - limit} chars truncated ...]\n"
    return marker + text[-limit:]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bot && uv run --extra dev pytest tests/test_sandbox_mcp.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add mcps/sandbox/helpers.py bot/tests/test_sandbox_mcp.py
git commit -m "feat(sandbox-mcp): pure helpers for package validation and truncation"
```

---

### Task 5: Sandbox MCP server — `run` / `install` tools, bridge client, pyproject

**Files:**
- Create: `mcps/sandbox/server.py`
- Create: `mcps/sandbox/pyproject.toml`
- Test: `bot/tests/test_sandbox_mcp.py` (append — server-level tests with a mocked bridge)

**Interfaces:**
- Consumes: `helpers.py` (Task 4). Reads `CLAUB_AGENT_NAME`, `EXEC_BRIDGE_SECRET`, `EXEC_BRIDGE_URL` (default `http://host.docker.internal:9501`) from the environment at startup; fails loudly if `CLAUB_AGENT_NAME` is absent (the `latex-resume`/`file-download` precedent).
- Produces:
  - `run(command: str, timeout: int = 180) -> str` — JSON (`--network none` chosen bridge-side because the command does not start with `uv pip install `).
  - `install(packages: list[str]) -> str` — validates names, bootstraps `{workspace}/.venv` on first use, then runs the fixed install command (network).
  - Internal `_post_exec(command, timeout) -> dict` that POSTs to the bridge with the `X-Exec-Secret` header and maps a connection failure to an actionable error.

- [ ] **Step 1: Write the failing tests**

Append to `bot/tests/test_sandbox_mcp.py`:

```python
import json
import sys
import types
from unittest.mock import MagicMock


def _load_server(monkeypatch, agent="leetcode-coach"):
    """Import mcps/sandbox/server.py fresh with env set and httpx stubbed."""
    monkeypatch.setenv("CLAUB_AGENT_NAME", agent)
    monkeypatch.setenv("EXEC_BRIDGE_SECRET", "s3cret")
    monkeypatch.setenv("EXEC_BRIDGE_URL", "http://bridge:9501")
    spec = importlib.util.spec_from_file_location(
        "sandbox_server", os.path.join(_DIR, "server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sandbox_server"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_run_posts_command_and_returns_json(monkeypatch):
    srv = _load_server(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["command"] = json["command"]
        captured["secret"] = headers.get("X-Exec-Secret")
        resp = MagicMock()
        resp.json.return_value = {"exit_code": 0, "stdout": "hi", "stdout_truncated": False,
                                  "stderr": "", "stderr_truncated": False,
                                  "timed_out": False, "duration_s": 0.1}
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr(srv.httpx, "post", fake_post)
    out = json.loads(srv.run("echo hi"))
    assert out["exit_code"] == 0 and out["stdout"] == "hi"
    assert captured["url"].endswith("/exec/leetcode-coach")   # agent from env, not param
    assert captured["command"] == "echo hi"
    assert captured["secret"] == "s3cret"


def test_run_truncates_long_stdout(monkeypatch):
    srv = _load_server(monkeypatch)
    big = "z" * 9000

    def fake_post(url, json, headers, timeout):
        resp = MagicMock()
        resp.json.return_value = {"exit_code": 0, "stdout": big, "stdout_truncated": True,
                                  "stderr": "", "stderr_truncated": False,
                                  "timed_out": False, "duration_s": 0.1}
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr(srv.httpx, "post", fake_post)
    out = json.loads(srv.run("flood"))
    assert len(out["stdout"]) < 9000 and "truncated" in out["stdout"].lower()


def test_run_bridge_down_gives_actionable_error(monkeypatch):
    srv = _load_server(monkeypatch)

    def fake_post(*a, **k):
        raise srv.httpx.ConnectError("refused")

    monkeypatch.setattr(srv.httpx, "post", fake_post)
    out = json.loads(srv.run("echo hi"))
    assert out["exit_code"] != 0
    assert "bridge" in out["error"].lower() and "not running" in out["error"].lower()


def test_install_rejects_flag_without_calling_bridge(monkeypatch):
    srv = _load_server(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(srv.httpx, "post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    out = json.loads(srv.install(["--index-url=http://evil"]))
    assert out["exit_code"] != 0 and called["n"] == 0
    assert "invalid" in out["error"].lower()


def test_install_builds_fixed_command(monkeypatch):
    srv = _load_server(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["command"] = json["command"]
        resp = MagicMock()
        resp.json.return_value = {"exit_code": 0, "stdout": "", "stdout_truncated": False,
                                  "stderr": "", "stderr_truncated": False,
                                  "timed_out": False, "duration_s": 0.1}
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr(srv.httpx, "post", fake_post)
    srv.install(["rich"])
    # bootstrap venv + install, both routed through the bridge; the install
    # command has the fixed uv-pip shape so the bridge grants it network.
    assert "uv pip install --python /claub/workspaces/leetcode-coach/.venv/bin/python rich" \
        in captured["command"]


def test_missing_agent_name_raises(monkeypatch):
    monkeypatch.delenv("CLAUB_AGENT_NAME", raising=False)
    monkeypatch.setenv("EXEC_BRIDGE_SECRET", "s")
    spec = importlib.util.spec_from_file_location(
        "sandbox_server_noenv", os.path.join(_DIR, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    with pytest.raises(RuntimeError, match="CLAUB_AGENT_NAME"):
        spec.loader.exec_module(mod)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd bot && uv run --extra dev pytest tests/test_sandbox_mcp.py -v -k "run or install or missing_agent"`
Expected: FAIL — `mcps/sandbox/server.py` does not exist.

- [ ] **Step 3: Implement the server**

Create `mcps/sandbox/server.py`:

```python
"""MCP server exposing a throwaway execution sandbox to a Claub agent.

`run` and `install` POST to the host-side exec bridge, which spawns a
`claub-exec` container mounting only this agent's workspace. Follows the
latex-resume / file-download shape: agent name comes from the environment (never
a tool parameter), so an agent cannot address another agent's sandbox.
"""
import json
import logging
import os
import sys

import httpx

from helpers import build_install_command, truncate_tail, validate_packages

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

from mcp.server.fastmcp import FastMCP

AGENT_NAME = os.environ.get("CLAUB_AGENT_NAME")
if not AGENT_NAME:
    raise RuntimeError("CLAUB_AGENT_NAME environment variable is required but not set")

BRIDGE_URL = os.environ.get("EXEC_BRIDGE_URL", "http://host.docker.internal:9501").rstrip("/")
BRIDGE_SECRET = os.environ.get("EXEC_BRIDGE_SECRET", "")
WORKSPACE_DIR = f"/claub/workspaces/{AGENT_NAME}"
VENV_PYTHON = f"{WORKSPACE_DIR}/.venv/bin/python"
HTTP_TIMEOUT = 600  # < MCP_TOOL_TIMEOUT, > bridge total

mcp = FastMCP("sandbox")


def _err(message: str) -> str:
    return json.dumps({"exit_code": 1, "error": message, "stdout": "", "stderr": "",
                       "timed_out": False})


def _post_exec(command: str, timeout: int) -> dict:
    resp = httpx.post(
        f"{BRIDGE_URL}/exec/{AGENT_NAME}",
        json={"command": command, "timeout": timeout},
        headers={"X-Exec-Secret": BRIDGE_SECRET},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _run_and_format(command: str, timeout: int) -> str:
    try:
        data = _post_exec(command, timeout)
    except httpx.ConnectError:
        return _err("sandbox bridge is not running on the host (connection refused). "
                    "Ask the operator to start the exec bridge.")
    except httpx.HTTPStatusError as e:
        return _err(f"sandbox bridge returned HTTP {e.response.status_code}")
    except httpx.HTTPError as e:
        return _err(f"sandbox bridge request failed: {e}")
    data["stdout"] = truncate_tail(data.get("stdout", ""))
    data["stderr"] = truncate_tail(data.get("stderr", ""))
    return json.dumps(data)


@mcp.tool()
def run(command: str, timeout: int = 180) -> str:
    """Run a shell command in a throwaway sandbox container.

    The command runs via `bash -c` in a fresh container that mounts ONLY your
    workspace (at its usual /claub/workspaces/<you> path) and holds no secrets,
    no Claude credentials, and no other agent's data. It has NO network. Files
    you write to your workspace persist; everything else is discarded when the
    command finishes. Emit a rendered file to Discord with
    [FILE:/claub/workspaces/<you>/path/to/output].

    Args:
        command: Shell command line (e.g. "manim -ql scene.py MyScene").
        timeout: Seconds before the container is killed (default 180, max 600).

    Returns:
        JSON: exit_code, stdout, stderr (each tail-truncated to 4000 chars),
        timed_out, duration_s.
    """
    return _run_and_format(command, timeout)


@mcp.tool()
def install(packages: list[str]) -> str:
    """Install Python packages into your workspace venv (persists across runs).

    Use this when a package isn't already baked into the sandbox. Names only —
    no flags, no URLs. The venv is created on first use with
    --system-site-packages so the baked manim/numpy/etc. stay visible.

    Args:
        packages: Distribution names, optionally pinned (e.g. ["rich", "networkx==3.3"]).
    """
    try:
        names = validate_packages(packages)
    except ValueError as e:
        return _err(str(e))
    # Bootstrap the venv (idempotent) then install, both via the bridge. The
    # install command's fixed `uv pip install ` prefix is what makes the bridge
    # grant network for this call and only this call.
    bootstrap = (
        f"[ -x {VENV_PYTHON} ] || uv venv --system-site-packages {WORKSPACE_DIR}/.venv"
    )
    command = f"{bootstrap} && " + build_install_command(names, VENV_PYTHON)
    return _run_and_format(command, timeout=600)


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Note: the `install` command string still begins its meaningful work with `uv venv ... &&`, so the bridge's `command.startswith("uv pip install ")` check would route it to `network=none`. Fix the routing to also grant network when the command **contains** the install invocation. In `scripts/exec-bridge/bridge.py`, change the network selection in `handle_exec` to:

```python
    network = "bridge" if "uv pip install " in command else "none"
```

and update `test_build_docker_argv_install_uses_bridge_network` expectations already pass a `uv pip install ...` string, so they are unaffected. Add a bridge test asserting the bootstrap-prefixed form also selects bridge network (append to `bot/tests/test_exec_bridge.py`):

```python
def test_exec_install_form_gets_bridge_network(bridge):
    base, _ = bridge
    _, body = post(f"{base}/exec/leetcode-coach",
                   {"command": "[ -x p ] || uv venv v && uv pip install --python p rich"},
                   {"X-Exec-Secret": "s3cret"})
    assert "--network bridge" in body["stdout"]
```

- [ ] **Step 4: Create the MCP pyproject**

Create `mcps/sandbox/pyproject.toml`:

```toml
[project]
name = "sandbox"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "mcp[cli]",
    "httpx",
]
```

- [ ] **Step 5: Run the full unit suite**

Run: `cd bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: all PASS (new sandbox + bridge tests, plus no regressions).

- [ ] **Step 6: Commit**

```bash
git add mcps/sandbox/ scripts/exec-bridge/bridge.py bot/tests/test_exec_bridge.py bot/tests/test_sandbox_mcp.py
git commit -m "feat(sandbox-mcp): run/install tools posting to the exec bridge"
```

---

### Task 6: Wiring — secret threading, config plumbing, docs, pilot opt-in

Repo changes: `docker-compose.yml`, `example/`, repo `CLAUDE.md`. **Instance changes (outside the repo, applied by hand):** `~/docker/claub/config/mcp.json` per-agent for `leetcode-coach`, `settings.json`, `agents.yaml`, plus `.envrc` and the bridge `config.json` from Task 3. Those instance edits are listed here but must NOT be committed and are verified live in Task 7.

**Files:**
- Modify: `docker-compose.yml` (add `EXEC_BRIDGE_SECRET` to `environment`)
- Modify: `example/config/mcp.json` (per-agent example is under `example/config/agents/` — add a sandbox block to an example agent's `.mcp.json`, or document it)
- Modify: `example/config/agents.yaml` (show `mcp__sandbox__*` opt-in + `algorithm-animation` skill)
- Modify: `example/config/settings.json` (add `mcp__sandbox__*` to allow list)
- Modify: repo root `CLAUDE.md` (add a Sandbox section + architecture note)

**Interfaces:** none (configuration + docs).

- [ ] **Step 1: docker-compose.yml — thread the secret**

Add to the `environment:` list (after `NOTION_TOKEN`):

```yaml
      - EXEC_BRIDGE_SECRET=${EXEC_BRIDGE_SECRET}
```

This is the one compose change the design otherwise avoids; it makes the secret available in the bot container so the per-agent `.mcp.json` `env` block can forward it to the sandbox MCP.

- [ ] **Step 2: example/config/settings.json — allow the tool**

Add `"mcp__sandbox__*",` to `permissions.allow` (after the schedules/agents entries). This is the hard ceiling; without it the tool is silently unavailable.

- [ ] **Step 3: example/config/agents.yaml — pilot opt-in**

Under an example agent (e.g. add a new commented block or extend `journalist`), show the opt-in shape, and add a comment that this is off by default:

```yaml
  # Example: give one agent the execution sandbox + animation skill.
  # leetcode-coach:
  #   channel_id: "YOUR_CHANNEL_ID"
  #   allowed_tools_additional:
  #     - "mcp__sandbox__*"
  #   allowed_skills:
  #     - algorithm-animation
```

- [ ] **Step 4: example agent .mcp.json — wire the MCP**

Add a `sandbox` server to `example/config/agents/journalist.mcp.json` (or whichever example agent) inside `mcpServers`, commented or live per the file's convention:

```json
    "sandbox": {
      "command": "uv",
      "args": ["--directory", "/app/mcps/sandbox", "run", "server.py"],
      "env": {
        "CLAUB_AGENT_NAME": "${CLAUB_AGENT_NAME}",
        "EXEC_BRIDGE_SECRET": "${EXEC_BRIDGE_SECRET}"
      }
    }
```

- [ ] **Step 5: repo CLAUDE.md — document the subsystem**

In the "### MCP Servers" area / project structure, add `sandbox/  # Throwaway container exec via the host exec bridge` to the `mcps/` list and `exec-bridge/` next to `playwright-bridge/` under `scripts/`. Then add a subsection after "### Lifecycle Hooks" or near the Playwright note:

```markdown
### Sandboxed Execution

Agents opted in via `allowed_tools_additional: ["mcp__sandbox__*"]` get
`mcp__sandbox__run(command)` and `mcp__sandbox__install(packages)` — arbitrary
shell in a throwaway `claub-exec` container that holds no secrets and mounts
only the calling agent's workspace (same path, `/claub/workspaces/{agent}`).
Spawned host-side by `scripts/exec-bridge/` (launchd, port 9501), mirroring the
Playwright bridge — the Docker socket never enters the bot container. `run` has
no network; `install` runs a fixed `uv pip install` under bridge network. The
`.claude/` dir is mounted read-only so injected code cannot write
`settings.local.json` to escalate. Image built by hand:
`docker build -t claub-exec docker/exec-sandbox/`. Design:
`docs/superpowers/specs/2026-07-26-sandboxed-exec-design.md`.
```

Update the architecture diagram's note near the MCP server line to mention the exec bridge as a sibling host-side daemon.

- [ ] **Step 6: Commit repo changes**

```bash
git add docker-compose.yml example/ CLAUDE.md
git commit -m "feat(sandbox): compose secret, example config, and docs wiring"
```

- [ ] **Step 7: Apply the INSTANCE config (host, NOT committed)**

These edits live under `~/docker/claub/config/` and `.envrc` / the bridge config — apply by hand, do not commit:

- `.envrc`: add `export EXEC_BRIDGE_SECRET=<a long random value>`.
- Host bridge `config.json` (from Task 3): set `secret` to the same value; confirm the `leetcode-coach` `extra_mounts` entry is present.
- `~/docker/claub/config/settings.json`: add `"mcp__sandbox__*"` to `permissions.allow`.
- `~/docker/claub/config/agents.yaml`: under `leetcode-coach`, add `allowed_tools_additional: ["mcp__sandbox__*"]` (and `allowed_skills: [algorithm-animation]` once Task 8 lands).
- `~/docker/claub/config/agents/leetcode-coach.mcp.json`: add the `sandbox` server block from Step 4.

No commit — these are verified live in Task 7.

---

### Task 7: Adversarial suite and end-to-end smoke (integration-gated)

The adversarial tests ARE the acceptance criteria for the security claims — real automated tests, gated behind `CLAUB_SANDBOX_INTEGRATION=1`, requiring the built image and a running bridge. Two are load-bearing: the `.claude/settings.local.json` write must fail, and `run` must have no reachability to `host.docker.internal`.

**Files:**
- Create: `bot/tests/test_sandbox_adversarial.py`

**Interfaces:** consumes the built `claub-exec` image (Task 1), a running bridge (Task 3), and the wired secret (Task 6). This is the end-to-end gate.

- [ ] **Step 1: Write the adversarial suite**

Create `bot/tests/test_sandbox_adversarial.py`:

```python
"""Adversarial acceptance tests — assert the sandbox FAILS to escape.

Gated: set CLAUB_SANDBOX_INTEGRATION=1 and have (a) the claub-exec image built,
(b) the exec bridge running, (c) EXEC_BRIDGE_SECRET + EXEC_BRIDGE_URL exported
to point at it, and (d) the calling agent name (default leetcode-coach) in the
bridge allowlist with a real host workspace.

Run: CLAUB_SANDBOX_INTEGRATION=1 EXEC_BRIDGE_SECRET=... \
     uv run --extra dev pytest tests/test_sandbox_adversarial.py -v
"""
import json
import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CLAUB_SANDBOX_INTEGRATION") != "1",
    reason="requires built image + running bridge",
)

AGENT = os.environ.get("CLAUB_SANDBOX_AGENT", "leetcode-coach")
URL = os.environ.get("EXEC_BRIDGE_URL", "http://127.0.0.1:9501").rstrip("/")
SECRET = os.environ.get("EXEC_BRIDGE_SECRET", "")


def _run(command: str, timeout: int = 60) -> dict:
    resp = httpx.post(f"{URL}/exec/{AGENT}", json={"command": command, "timeout": timeout},
                      headers={"X-Exec-Secret": SECRET}, timeout=120)
    resp.raise_for_status()
    return resp.json()


def test_no_claude_credentials():
    out = _run("cat /root/.claude/.credentials.json 2>&1 || true")
    assert ".credentials.json" not in out["stdout"] or "No such file" in out["stdout"]
    assert "sk-" not in out["stdout"]


def test_no_other_agent_workspace():
    out = _run("ls /claub/workspaces/main 2>&1 || true")
    assert "No such file" in out["stdout"] or "cannot access" in out["stdout"]


def test_no_secrets_in_env():
    out = _run("env | grep -iE 'token|key|secret' || true")
    assert out["stdout"].strip() == ""


def test_cannot_write_outside_workspace():
    out = _run("echo x > /etc/probe 2>&1 || true")
    assert "Read-only file system" in out["stdout"] or "Permission denied" in out["stdout"]


def test_cannot_write_claude_settings_local():
    # LOAD-BEARING: the self-escalation path. Must be a read-only filesystem error.
    out = _run(f"echo x > /claub/workspaces/{AGENT}/.claude/settings.local.json 2>&1 || true")
    assert "Read-only file system" in out["stdout"]


def test_no_network_to_host_bridge():
    # LOAD-BEARING: run must not reach the playwright bridge or any browser MCP.
    out = _run("curl -sS --max-time 5 http://host.docker.internal:9500/status 2>&1 || true")
    assert "9500" not in out["stdout"] or "Could not resolve" in out["stdout"] \
        or "Failed to connect" in out["stdout"] or "Network is unreachable" in out["stdout"]
    assert '"running"' not in out["stdout"] and "{}" not in out["stdout"]


def test_workspace_is_writable():
    out = _run(f"echo ok > /claub/workspaces/{AGENT}/.sandbox-probe && "
               f"cat /claub/workspaces/{AGENT}/.sandbox-probe && "
               f"rm /claub/workspaces/{AGENT}/.sandbox-probe")
    assert out["stdout"].strip() == "ok"


def test_bridge_rejects_missing_secret():
    resp = httpx.post(f"{URL}/exec/{AGENT}", json={"command": "echo x"}, timeout=30)
    assert resp.status_code == 401
```

- [ ] **Step 2: Build the image and start the bridge (host)**

```bash
docker build -t claub-exec /Users/you/Claude/docker/exec-sandbox/
# Start the bridge via launchd (Task 3 install) or directly for the test:
python3 /Users/you/Claude/scripts/exec-bridge/bridge.py --config ~/Library/Application\ Support/claub-exec-bridge/config.json &
curl http://127.0.0.1:9501/status   # {"running": {}}
```

- [ ] **Step 3: Run the adversarial suite**

```bash
cd /Users/you/Claude/bot && CLAUB_SANDBOX_INTEGRATION=1 \
  EXEC_BRIDGE_SECRET="$(grep EXEC_BRIDGE_SECRET /Users/you/Claude/.envrc | cut -d= -f2)" \
  uv run --extra dev pytest tests/test_sandbox_adversarial.py -v
```
Expected: all PASS. If `test_cannot_write_claude_settings_local` or `test_no_network_to_host_bridge` fails, STOP — the design is broken; do not proceed.

- [ ] **Step 4: Deploy the wired bot and manual smoke**

Instance config + compose changed → rebuild:
```bash
cd /Users/you/Claude && source .envrc && docker compose up -d --build
```
Then drive `leetcode-coach` via the debug CLI through the full ladder:
```bash
docker exec claude-claub-1 uv run --project /app/bot \
  python -m claude_assistant.debug_agent leetcode-coach \
  -p "Use mcp__sandbox__run to: (1) echo hello, (2) run a one-line python that prints 2+2, (3) install the 'rich' package with mcp__sandbox__install, then in a SEPARATE run import rich and print its version. Report each result."
```
Expected: hello, `4`, and a rich version — proving run, a python script, and an install that persists across two separate `run` calls (the venv lives on the workspace mount).

- [ ] **Step 5: Commit the test**

```bash
git add bot/tests/test_sandbox_adversarial.py
git commit -m "test(sandbox): adversarial acceptance suite (integration-gated)"
```

---

### Task 8: Phase 2 placeholder — `algorithm-animation` skill (planned separately)

Phase 2 is a shared skill plus the already-baked Manim dependency. It is **out of scope for this plan** and gets its own plan once phase 1 lands and the sandbox is proven. Recorded here so nothing is lost.

**Files (future, not this plan):**
- Create: `/claub/config/skills/algorithm-animation/SKILL.md` (instance) and possibly a sanitized `example/` copy.

**Scope when planned:**
- Two or three complete, verified example scenes (graph traversal, array/two-pointer walk, DP table fill), pinned to `manim==0.20.1`, using `Text`/`MarkupText` only (no `MathTex` — no LaTeX in the image).
- House rules: `-ql` (480p15) default, mp4 not gif (Discord 10 MB cap), `[FILE:...]` delivery.
- The mandatory self-check loop: render → generate a six-frame contact sheet via `ffmpeg` + Pillow → `Read` it before posting. This is the single highest-value item.
- Gate via `allowed_skills: [algorithm-animation]` on `leetcode-coach`.

**Open items carried from the spec** (address when planning phase 2 or as follow-ups):
- Attachments: the workspace `.attachments/` change (download into `{workspace}/.attachments/{message_id}/` instead of container `/tmp`) is a **separate change**, not folded into phase 1; without it "upload a CSV, have the agent plot it" doesn't work for binaries.
- `--tmpfs /tmp:size=256m,exec` short form confirmed working on Docker 28.4.0 during planning; re-verify against the `claub-exec` image in Task 1 Step 5.
- Raising `max_concurrent` above 1 requires adding the per-agent `.venv` lock and re-checking `max_concurrent × 1 GiB + ~2 GiB ≤ 4 GiB VM RAM`.
- Re-run Task 7's adversarial suite after any Claude CLI version bump — the bot container's credential/cross-workspace protections are upstream CLI behavior, not configuration here.

---

## Self-Review

**Spec coverage.**
- Host bridge (not docker.sock) → Task 3. Ephemeral per call → the bridge spawns `docker run --rm` per request, no session. Network decision (run none / install fixed) → Global Constraints + Task 2 argv + Task 5 install shape + bridge routing. uv workspace venv → Task 5 `install` bootstrap. `claub-exec` image with mandatory aarch64 toolchain → Task 1. Bridge endpoints/config/input handling/concurrency/output cap → Tasks 2–3. MCP two tools, env agent name, truncation, bridge-down error → Tasks 4–5. `docker run` five load-bearing details (read-only `.claude`, network none, `bash -c`, clean env, path identity, extra_mounts exception) → Task 2 argv + Global Constraints. Colima prereq → Prerequisite. Timeout budget/orphan reap/kill-container → Task 3. Adversarial + bridge unit + MCP tests + manual smoke → Tasks 2/3/5/7. Configuration (agents.yaml/mcp.json/settings/bridge config/secret) → Task 6. Phasing (image FIRST) → task order enforced. Visualization layer → Task 8 placeholder. Attachments known-limitation → Task 8 open items (spec says separate change). Covered.
- **Two spec items to flag to the reviewer (below), not silently worked around.**

**Placeholder scan.** No TBD/TODO; every code and test step carries real content.

**Type consistency.** Bridge response keys (`exit_code`, `stdout`, `stdout_truncated`, `stderr`, `stderr_truncated`, `timed_out`, `duration_s`) are produced in Task 3 `run_container` and consumed identically in Task 5 `_run_and_format` and Task 7. `build_docker_argv(agent, command, cfg, network, name)` signature is consistent across Task 2 definition, its tests, and the Task 3 caller. `validate_packages` / `build_install_command` / `truncate_tail` consistent Task 4 ↔ Task 5.

## Notes for the reviewer (spec issues found)

1. **Network routing for `install` needed a correction the spec's endpoint table doesn't cover.** The spec's architecture shows a single `POST /exec/{agent}` and says `run` gets `--network none` while `install` gets bridge network under a fixed `uv pip install` command. But `install` must first bootstrap the venv (`uv venv ... && uv pip install ...`), so a naive `command.startswith("uv pip install")` check on the bridge would deny that combined command network. The plan resolves this by routing on `"uv pip install " in command` (substring, not prefix) and adds a bridge test for the bootstrap-prefixed form. This is safe because the bridge's only client is the secret-gated MCP and the secret never enters the sandbox — but it is a real detail the spec's "the command shape is fixed by the server" glosses over. Worth confirming the substring rule is acceptable, or splitting `install` into its own bridge endpoint for a cleaner trust boundary.

2. **The spec's `permissions.deny` double-slash note is already live in the instance settings** (`Read(//root/.claude/...)`, `Read(//claub/config/**)` are present in the current `~/docker/claub/config/settings.json`). Nothing to add there for phase 1, but the spec presents it as prospective ("if it is ever needed") — it is in fact already deployed, so re-verifying those rules still deny after a CLI bump belongs in the Task 7 post-upgrade checklist (added).

3. Everything else in the spec was implementable as written.
