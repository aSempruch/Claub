#!/bin/zsh
# Wrapper script for launchd — sets up environment and runs the bot.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BOT_DIR="$REPO_DIR/bot"

# Ensure claude CLI is on PATH (launchd doesn't inherit shell PATH)
export PATH="$HOME/.local/bin:$PATH"

# Load environment variables (.envrc)
set -a
source "$BOT_DIR/.envrc"
set +a

cd "$BOT_DIR"
exec /opt/homebrew/bin/uv run claude-assistant
