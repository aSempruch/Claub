#!/bin/bash
set -e

# Copy config files into Claude CLI's native home
mkdir -p ~/.claude
if [ -d "/claub/config/agents" ]; then
    cp -r /claub/config/agents ~/.claude/agents
fi
if [ -f "/claub/config/settings.json" ]; then
    cp /claub/config/settings.json ~/.claude/settings.json
fi
if [ -f "/claub/config/CLAUDE.md" ]; then
    cp /claub/config/CLAUDE.md ~/.claude/CLAUDE.md
fi

# Ensure data directories exist
mkdir -p /claub/data/workspaces

exec uv run claude-assistant
