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
import sys
from pathlib import Path

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

    process = _build_process(args.agent)
    runner = (
        _run_one_shot(process, args.prompt)
        if args.prompt is not None
        else _run_interactive(process)
    )
    try:
        asyncio.run(runner)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
