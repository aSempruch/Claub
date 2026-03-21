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


def _resolve_paths() -> tuple[Path, Path, Path, Path, Path]:
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
        claude_dir / "config" / "mcp.json",
    )


def main() -> None:
    # Load .envrc from bot/ directory
    bot_dir = Path(__file__).resolve().parents[2]
    load_dotenv(bot_dir / ".envrc")

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        log.error("DISCORD_BOT_TOKEN environment variable is required")
        sys.exit(1)

    config_path, home_dir, workspaces_dir, sessions_path, mcp_config = _resolve_paths()

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
    )

    log.info("Starting claude-assistant")
    asyncio.run(bot.run(token))


if __name__ == "__main__":
    main()
