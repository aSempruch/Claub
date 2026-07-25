"""Debug CLI for talking to a configured agent without session persistence.

Reuses the production config path so all the flags an agent normally runs
with (--agents, --agent, --allowedTools, --disallowedTools, --model,
--effort, per-agent MCP configs) are applied automatically — the only
differences from a live agent are that --resume is never passed and
--no-session-persistence is added.

Usage:
    python -m claude_assistant.debug_agent <name> [-p PROMPT]

One-shot (with -p): send the prompt, print the response, exit.
Interactive (no -p): read lines from stdin, print each response; EOF exits.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn

from claude_assistant.agent_messaging import combine_mcp_apps, create_messaging_servers
from claude_assistant.claude_process import AgentProcess
from claude_assistant.config import (
    AssistantConfig,
    discover_skills,
    load_config,
    parse_agent_file,
)
from claude_assistant.discord_bot import _ensure_authoring_symlink, build_agent_process
from claude_assistant.main import _resolve_paths

log = logging.getLogger("claude_assistant.debug_agent")


def _mcp_configs_for(
    name: str, shared: Path | None, agents_dir: Path | None
) -> list[Path]:
    configs: list[Path] = []
    if shared and shared.exists():
        configs.append(shared)
    if agents_dir:
        per_agent = agents_dir / f"{name}.mcp.json"
        if per_agent.exists():
            configs.append(per_agent)
    return configs


def _disallowed_skills_for(
    name: str, config: AssistantConfig, all_skills: list[str]
) -> list[str]:
    agent_config = config.agents.get(name)
    allowed = set(config.allowed_skills)
    if agent_config:
        allowed |= set(agent_config.allowed_skills)
    return [s for s in all_skills if s not in allowed]


def _load_agent_definition(
    name: str, agents_dir: Path | None
) -> dict[str, str] | None:
    if not agents_dir:
        return None
    agent_file = agents_dir / f"{name}.md"
    if not agent_file.exists():
        return None
    try:
        return parse_agent_file(agent_file)
    except Exception:
        log.exception("Failed to parse agent file %s", agent_file)
        return None


def _build_process(agent_name: str) -> AgentProcess:
    (
        config_path,
        workspaces_dir,
        _sessions_path,
        mcp_config,
        agents_dir,
        _schedules_path,
        skills_dir,
        _history_path,
    ) = _resolve_paths()

    if not config_path.exists():
        print(f"error: config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    config = load_config(config_path)
    if agent_name not in config.agents:
        known = ", ".join(sorted(config.agents.keys()))
        print(
            f"error: unknown agent {agent_name!r}. known: {known}",
            file=sys.stderr,
        )
        sys.exit(2)

    all_skills = discover_skills(skills_dir)
    workspace = workspaces_dir / agent_name
    workspace.mkdir(parents=True, exist_ok=True)
    _ensure_authoring_symlink(workspace, "skills")
    _ensure_authoring_symlink(workspace, "agents")

    shared_mcp = mcp_config if mcp_config.exists() else None
    agents_dir_resolved = agents_dir if agents_dir.exists() else None

    return build_agent_process(
        name=agent_name,
        workspace=workspace,
        config=config,
        mcp_configs=_mcp_configs_for(agent_name, shared_mcp, agents_dir_resolved),
        agent_definition=_load_agent_definition(agent_name, agents_dir_resolved),
        disallowed_skills=_disallowed_skills_for(agent_name, config, all_skills),
        debug=True,
    )


async def start_debug_messaging(config, agents_dir, build_process, live):
    """Run an isolated agent-messaging MCP server for a debug session.

    Receivers are built via *build_process* (debug=True — fresh sessions, no
    persistence), started lazily, and returned in a registry for cleanup.
    Sets CLAUB_MSG_PORT so every subsequently spawned process (including
    receivers-of-receivers) talks to this server instead of the live bot's.
    """
    registry: dict[str, AgentProcess] = {}

    async def deliver(name: str, content: str) -> str:
        process = registry.get(name)
        if process is None or not process.is_alive:
            process = build_process(name)
            await process.start(None)
            registry[name] = process
            live[name] = process
        return await process.send_message(content)

    descriptions: dict[str, str] = {}
    for name in config.agents:
        definition = _load_agent_definition(name, agents_dir)
        if definition:
            descriptions[name] = definition.get("description", "")

    mcps = create_messaging_servers(
        config.agents, config.agent_groups, deliver, live.get, descriptions
    )
    app = combine_mcp_apps(
        None, {n: m.http_app(path="/mcp") for n, m in mcps.items()}
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()  # surface startup errors
        await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    os.environ["CLAUB_MSG_PORT"] = str(port)
    log.info("debug messaging server on 127.0.0.1:%d", port)
    return server, task, registry


async def _run_one_shot(process: AgentProcess, prompt: str) -> None:
    await process.start(None)
    try:
        reply = await process.send_message(prompt)
        print(reply)
    finally:
        await process.stop()


async def _run_interactive(process: AgentProcess) -> None:
    await process.start(None)
    loop = asyncio.get_running_loop()
    try:
        print("debug agent ready. type a message and press enter. EOF exits.", file=sys.stderr)
        while True:
            sys.stderr.write("> ")
            sys.stderr.flush()
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            prompt = line.rstrip("\n")
            if not prompt:
                continue
            reply = await process.send_message(prompt)
            print(reply, flush=True)
    finally:
        await process.stop()


async def _run(agent_name: str, prompt: str | None) -> None:
    (
        config_path, _workspaces, _sessions, _mcp_config, agents_dir,
        _schedules, _skills, _history,
    ) = _resolve_paths()
    config = load_config(config_path)

    server = serve_task = None
    registry: dict[str, AgentProcess] = {}
    live: dict[str, AgentProcess] = {}
    in_group = any(agent_name in m for m in config.agent_groups.values())
    if in_group:
        server, serve_task, registry = await start_debug_messaging(
            config, agents_dir if agents_dir.exists() else None, _build_process, live
        )

    process = _build_process(agent_name)
    live[agent_name] = process
    try:
        if prompt is not None:
            await _run_one_shot(process, prompt)
        else:
            await _run_interactive(process)
    finally:
        for receiver in registry.values():
            await receiver.stop()
        if server is not None:
            server.should_exit = True
        if serve_task is not None:
            await serve_task


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="claude_assistant.debug_agent",
        description="Talk to a configured agent without session persistence.",
    )
    parser.add_argument("agent", help="agent name from agents.yaml")
    parser.add_argument(
        "-p",
        "--prompt",
        help="send this prompt and exit; omit for interactive mode",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_run(args.agent, args.prompt))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
