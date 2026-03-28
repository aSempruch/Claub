#!/bin/bash
set -e

# Copy config files into Claude CLI's native home
mkdir -p ~/.claude

# Persist ~/.claude.json inside the claude-home volume via symlink
touch -a ~/.claude/claude-symlink.json
ln -sf ~/.claude/claude-symlink.json ~/.claude.json
if [ -d "/claub/config/agents" ]; then
    cp -r /claub/config/agents ~/.claude/agents
fi
if [ -f "/claub/config/settings.json" ]; then
    cp /claub/config/settings.json ~/.claude/settings.json
fi
if [ -f "/claub/config/CLAUDE.md" ]; then
    cp /claub/config/CLAUDE.md ~/.claude/CLAUDE.md
fi
if [ -d "/claub/config/skills" ]; then
    cp -r /claub/config/skills ~/.claude/skills
fi

# Ensure data and workspace directories exist
mkdir -p /claub/workspaces


# Install dependencies for mounted MCP servers
for dir in /claub/mcps/*/; do
    if [ -f "$dir/pyproject.toml" ]; then
        uv sync --directory "$dir" 2>&1 | tail -1
    fi
done

exec uv run claude-assistant
