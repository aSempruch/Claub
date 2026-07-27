"""Pure helpers for the exec bridge — no I/O, unit-testable without Docker."""
from __future__ import annotations

import re
from collections.abc import Iterable

AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9._-]+(==[A-Za-z0-9._-]+)?$")


def validate_agent(name: str, allowed: list[str]) -> None:
    """Raise ValueError unless *name* matches the safe pattern AND is allowed.

    The name becomes part of a bind-mount path, so it is the one untrusted
    input. The caller maps ValueError to HTTP 404 (not 400) — no information
    about which agents exist.
    """
    if not AGENT_NAME_RE.match(name) or name not in allowed:
        raise ValueError(f"unknown agent: {name!r}")


def validate_packages(packages: list[str]) -> list[str]:
    """Return *packages* unchanged if every entry is a bare (optionally pinned)
    distribution name.

    This is THE security control for the networked /install path: it runs in
    the bridge, the one component a compromised bot container cannot tamper
    with. The MCP validates too, but only as convenience.
    """
    if not packages:
        raise ValueError("no packages given")
    for name in packages:
        if not PACKAGE_RE.match(name):
            raise ValueError(f"invalid package name: {name!r}")
    return packages


def build_install_command(agent: str, packages: list[str]) -> str:
    """Build the install command from validated names. The caller supplies
    NAMES ONLY — there is no code path by which a caller provides a command on
    the networked endpoint.
    """
    names = validate_packages(packages)
    ws = f"/claub/workspaces/{agent}"
    venv_python = f"{ws}/.venv/bin/python"
    bootstrap = f"[ -x {venv_python} ] || uv venv --system-site-packages {ws}/.venv"
    return f"{bootstrap} && uv pip install --python {venv_python} " + " ".join(names)


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
    """Full `docker run` argv.

    `network` is 'none' (/exec) or 'bridge' (/install) and is decided by the
    ENDPOINT the request arrived on. This function must never inspect
    `command` to infer it — that check is spoofable by a command that merely
    contains the install shape.
    """
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
