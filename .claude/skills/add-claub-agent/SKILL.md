---
name: add-claub-agent
description: Use when adding a new agent to a Claub instance, editing an existing agent's prompt or behavior, deciding which config level a piece of behavior belongs in (global CLAUDE.md vs agent identity .md vs workspace living config), or changing agent memory guidelines
---

# Configuring Claub Agents

## The Three-Level Split

Agent behavior is configured at three levels. Getting this split wrong produces either rigid agents that can't adapt, or unstable agents that lose their identity.

**Level 1 — Global** (`/claub/config/CLAUDE.md`): rules for **all** agents — safety, Discord behavior, workspace usage, memory protocol. Copied into `~/.claude/CLAUDE.md` at container startup.

**Level 2 — Agent identity** (`/claub/config/agents/{name}.md`): the agent's **stable core** — personality, communication style, role, capabilities, memory structure. This is *who the agent is*; it should rarely change. Does **NOT** include:
- Targets, criteria, or parameters the user might adjust → Level 3
- Scheduled task instructions → the schedule's `prompt`
- Runtime state or progress → memory

**Level 3 — Living config** (`/claub/workspaces/{name}/CLAUDE.md`): the **fluid details** — current targets, search criteria, focus areas, topic lists, thresholds. The agent **self-modifies** this when the user shifts focus ("stop covering crypto", "raise my comp target to $180k"). The Level 2 file must reference it so the agent knows it exists.

> **Rule of thumb:** if you'd change it by editing the agent's personality, it's Level 2. If you'd change it by telling the agent "from now on focus on X instead of Y", it's Level 3.

Every agent also gets `/claub/workspaces/{name}/` — an auto-created runtime scratch directory it can use freely.

## Adding an Agent

1. Add an entry to `/claub/config/agents.yaml` with `channel_id` (required). Agent names must match `[A-Za-z0-9_-]+` — they become MCP tool names and URL path segments.
2. Create `/claub/config/agents/{name}.md` — **stable identity only**. Must have YAML frontmatter with `name` and `description`. Reference the workspace CLAUDE.md.
3. Create `/claub/workspaces/{name}/CLAUDE.md` — the fluid details.
4. Optional: `/claub/config/agents/{name}.mcp.json` for agent-specific MCP servers. Any new MCP also needs its `mcp__{server}__*` glob in `settings.json`'s allow list.
5. If the agent needs a browser: add a bridge profile entry and run `apply-zoom-prefs.py` — see the `claub-playwright` skill.
6. `docker compose restart`

Config-only changes need a restart, not a rebuild — the entrypoint re-copies config at startup.

## Agent Memory

Agents are long-running assistants that may operate for months, not dev tools. Memory is file-based at `/claub/workspaces/{name}/memory/`.

Memory rules follow the same split: **global** rules (Level 1) enforce mechanical discipline; **agent-specific** rules (Level 2) define *what* to remember and *how long* to keep it. Preserve that split when editing.

**Design principles to enforce:**
- **Mandatory startup read** — every agent reads `memory/index.md` before any work, every session. No conditional checks.
- **Write-time pruning** — every memory write includes an index review: remove outdated entries, merge overlapping ones. This is the primary defense against bloat.
- **Compaction awareness** — context compaction silently drops history. Important things get written promptly, never deferred to end-of-session.
- **Current state wins** — when memory conflicts with observed reality, trust reality and update the memory. Stale entries compound errors.
- **Bounded growth** — index stays under ~50 entries. Agent-specific rules define retention (e.g. journalist keeps 7 days of briefs). Memory without a pruning policy degrades performance over time.

## Common Mistakes

| Mistake | Why it hurts |
|---|---|
| Putting adjustable targets in the Level 2 `.md` | User can't shift focus without you editing the agent's identity |
| Putting personality in workspace CLAUDE.md | Agent can self-modify its own identity and drift |
| Putting task instructions in the agent `.md` | Belongs in the schedule `prompt`; bloats every session |
| Adding an MCP without the `settings.json` glob | Tools silently unavailable — allow list is the hard ceiling |
| Rebuilding the image for a prompt change | Only `bot/` changes need `--build`; config needs `restart` |
