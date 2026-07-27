"""MCP server exposing a throwaway execution sandbox to a Claub agent.

`run` POSTs to the bridge's /exec (always --network none); `install` POSTs
package NAMES to /install (networked, but structurally unable to run an
arbitrary command — the bridge builds that argv). Follows the latex-resume /
file-download shape: agent name comes from the environment (never a tool
parameter), so an agent cannot address another agent's sandbox.
"""
import json
import logging
import os
import sys
from pathlib import Path

import httpx

# Resolve helpers.py next to this file regardless of cwd — running the server
# directly puts its dir on sys.path, but loading it by path (as the tests do)
# does not. Same pattern as scripts/exec-bridge/bridge.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import truncate_tail, validate_packages

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

from mcp.server.fastmcp import FastMCP

AGENT_NAME = os.environ.get("CLAUB_AGENT_NAME")
if not AGENT_NAME:
    raise RuntimeError("CLAUB_AGENT_NAME environment variable is required but not set")

BRIDGE_URL = os.environ.get("EXEC_BRIDGE_URL", "http://host.docker.internal:9501").rstrip("/")
BRIDGE_SECRET = os.environ.get("EXEC_BRIDGE_SECRET", "")
WORKSPACE_DIR = f"/claub/workspaces/{AGENT_NAME}"
HTTP_TIMEOUT = 600  # < MCP_TOOL_TIMEOUT, > bridge total

mcp = FastMCP("sandbox")


def _err(message: str) -> str:
    return json.dumps({"exit_code": 1, "error": message, "stdout": "", "stderr": "",
                       "timed_out": False})


def _post(path: str, payload: dict) -> dict:
    resp = httpx.post(
        f"{BRIDGE_URL}/{path}/{AGENT_NAME}",
        json=payload,
        headers={"X-Exec-Secret": BRIDGE_SECRET},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _call_and_format(path: str, payload: dict) -> str:
    try:
        data = _post(path, payload)
    except httpx.ConnectError:
        return _err("sandbox bridge is not running on the host (connection refused). "
                    "Ask the operator to start the exec bridge.")
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = ": " + str(e.response.json().get("error", ""))
        except Exception:
            pass
        return _err(f"sandbox bridge returned HTTP {e.response.status_code}{detail}")
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
    return _call_and_format("exec", {"command": command, "timeout": timeout})


@mcp.tool()
def install(packages: list[str]) -> str:
    """Install Python packages into your workspace venv (persists across runs).

    Use this when a package isn't already baked into the sandbox. Names only —
    no flags, no URLs. The venv is created on first use with
    --system-site-packages so the baked manim/numpy/etc. stay visible.

    Args:
        packages: Distribution names, optionally pinned (e.g. ["rich", "networkx==3.3"]).
    """
    # Local validation is a fast, clear error for the agent — NOT the control.
    # The bridge re-validates and is the enforcing party.
    try:
        names = validate_packages(packages)
    except ValueError as e:
        return _err(str(e))
    # Names only. The bridge owns the whole command (venv bootstrap included),
    # so the networked endpoint cannot be handed anything to execute.
    return _call_and_format("install", {"packages": names})


if __name__ == "__main__":
    mcp.run(transport="stdio")
