#!/bin/zsh
# Wrapper script for launchd — sets up environment and runs the bot.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BOT_DIR="$REPO_DIR/bot"

# Load environment variables (.envrc)
set -a
source "$BOT_DIR/.envrc"
set +a

export CLAUDE_ASSISTANT_ROOT="$REPO_DIR"

cd "$BOT_DIR"
exec /opt/homebrew/bin/uv run claude-assistant
