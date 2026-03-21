from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from claude_assistant.config import load_config
from claude_assistant.discord_bot import AssistantBot
from claude_assistant.session import SessionStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("claude_assistant")
logging.getLogger("claude_assistant.claude_process").setLevel(logging.DEBUG)


def _resolve_paths() -> tuple[Path, Path, Path, Path, Path, Path]:
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
    )


def main() -> None:
    # Load .envrc from bot/ directory
    bot_dir = Path(__file__).resolve().parents[2]
    load_dotenv(bot_dir / ".envrc")

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        log.error("DISCORD_BOT_TOKEN environment variable is required")
        sys.exit(1)

    config_path, home_dir, workspaces_dir, sessions_path, mcp_config, agents_dir = _resolve_paths()

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
        mcp_config=mcp_config if mcp_config.exists() else None,
        agents_dir=agents_dir if agents_dir.exists() else None,
    )

    log.info("Starting claude-assistant")
    asyncio.run(bot.run(token))


if __name__ == "__main__":
    main()
