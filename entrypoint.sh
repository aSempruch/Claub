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
mkdir -p /claub/workspaces

# Install dependencies for mounted MCP servers
for dir in /claub/mcps/*/; do
    if [ -f "$dir/pyproject.toml" ]; then
        uv sync --directory "$dir" 2>&1 | tail -1
    fi
done

exec uv run claude-assistant
