# Public-Ready Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clean up the Claub repo for public visibility on GitHub — remove personal/legacy artifacts, add missing standard files, showcase prompt engineering via example config, and bake MCP servers into the repo.

**Architecture:** Mostly file deletions, additions, and edits. No bot code changes. MCP servers are copied from `~/docker/claub/mcps/` into `mcps/` in the repo and baked into the Docker image. Example config is created from sanitized versions of the real instance config.

**Tech Stack:** Git, Docker, Python (MCP servers), Markdown

---

## File Map

### Files to delete
- `scripts/` (entire directory — 6 files)
- `docs/superpowers/` (entire directory — 14 files, including this plan)
- `docs/leetcode-api-knowledge-transfer.md`
- `handoff-google-oauth.md`

### Files to create
- `mcps/git/server.py` (copy from `~/docker/claub/mcps/git/server.py`)
- `mcps/git/pyproject.toml` (copy from `~/docker/claub/mcps/git/pyproject.toml`)
- `mcps/leetcode-stats/server.py` (copy + scrub from `~/docker/claub/mcps/leetcode-stats/server.py`)
- `mcps/leetcode-stats/pyproject.toml` (copy from `~/docker/claub/mcps/leetcode-stats/pyproject.toml`)
- `mcps/leetcode-stats/tests/test_cloud_code.py` (copy from `~/docker/claub/mcps/leetcode-stats/tests/test_cloud_code.py`)
- `mcps/nextcloud/server.py` (copy from `~/docker/claub/mcps/nextcloud/server.py`)
- `mcps/nextcloud/pyproject.toml` (copy from `~/docker/claub/mcps/nextcloud/pyproject.toml`)
- `example/config/agents.yaml`
- `example/config/CLAUDE.md` (copy from `~/docker/claub/config/CLAUDE.md`)
- `example/config/settings.json`
- `example/config/mcp.json`
- `example/config/agents/main.md` (copy from `~/docker/claub/config/agents/main.md`)
- `example/config/agents/journalist.md` (copy from `~/docker/claub/config/agents/journalist.md`)
- `example/config/agents/shopping-assistant.md` (sanitized copy)
- `example/workspaces/journalist/CLAUDE.md` (sanitized copy)
- `example/workspaces/shopping-assistant/CLAUDE.md` (sanitized copy)
- `README.md`
- `LICENSE`
- `.env.example`
- `.claude/CLAUDE.md` (personal dev-time instructions, already gitignored)

### Files to modify
- `CLAUDE.md` — remove dev-time note, update project structure, update MCP paths
- `Dockerfile` — add `COPY mcps/` line
- `entrypoint.sh` — add loop for `/app/mcps/`
- `docker-compose.yml` — parameterize data path and timezone
- `bot/pyproject.toml` — add metadata
- `.dockerignore` — add `example/` exclusion

---

### Task 1: Create branch and delete legacy files

**Files:**
- Delete: `scripts/*`, `docs/superpowers/*`, `docs/leetcode-api-knowledge-transfer.md`, `handoff-google-oauth.md`

- [ ] **Step 1: Create new branch from current HEAD**

```bash
git checkout -b feature/public-ready
```

- [ ] **Step 2: Delete scripts/ directory**

```bash
git rm -r scripts/
```

- [ ] **Step 3: Delete docs/superpowers/ directory**

```bash
git rm -r docs/superpowers/
```

- [ ] **Step 4: Delete standalone docs**

```bash
git rm docs/leetcode-api-knowledge-transfer.md
git rm handoff-google-oauth.md
```

- [ ] **Step 5: Verify deletions**

Run: `git status`
Expected: All deletions staged, no untracked files from these directories remain.

- [ ] **Step 6: Commit**

```bash
git commit -m "chore: remove legacy scripts, internal planning docs, and handoff notes

Scripts were pre-Docker launchd service management (replaced by Docker).
Planning docs were session artifacts from development tooling.
Handoff and knowledge-transfer docs contained personal paths."
```

---

### Task 2: Add MCP servers to repo

**Files:**
- Create: `mcps/git/server.py`, `mcps/git/pyproject.toml`
- Create: `mcps/leetcode-stats/server.py`, `mcps/leetcode-stats/pyproject.toml`, `mcps/leetcode-stats/tests/test_cloud_code.py`
- Create: `mcps/nextcloud/server.py`, `mcps/nextcloud/pyproject.toml`

- [ ] **Step 1: Copy git MCP**

```bash
mkdir -p mcps/git
cp ~/docker/claub/mcps/git/server.py mcps/git/
cp ~/docker/claub/mcps/git/pyproject.toml mcps/git/
```

- [ ] **Step 2: Copy nextcloud MCP**

```bash
mkdir -p mcps/nextcloud
cp ~/docker/claub/mcps/nextcloud/server.py mcps/nextcloud/
cp ~/docker/claub/mcps/nextcloud/pyproject.toml mcps/nextcloud/
```

- [ ] **Step 3: Copy leetcode-stats MCP**

```bash
mkdir -p mcps/leetcode-stats/tests
cp ~/docker/claub/mcps/leetcode-stats/server.py mcps/leetcode-stats/
cp ~/docker/claub/mcps/leetcode-stats/pyproject.toml mcps/leetcode-stats/
cp ~/docker/claub/mcps/leetcode-stats/tests/test_cloud_code.py mcps/leetcode-stats/tests/
```

- [ ] **Step 4: Scrub hardcoded username from leetcode-stats**

In `mcps/leetcode-stats/server.py`, change:

```python
DEFAULT_USERNAME = "asempruch"
```

to:

```python
DEFAULT_USERNAME = os.environ.get("LEETCODE_USERNAME", "")
```

And add `import os` at the top if not already present. Also update the Referer header that references it:

```python
"Referer": f"https://leetcode.com/{DEFAULT_USERNAME}/",
```

to:

```python
"Referer": "https://leetcode.com/",
```

And update the `get_stats` tool signature from `username: str = DEFAULT_USERNAME` to `username: str = DEFAULT_USERNAME` (no change needed — the default is now the env var value, which is fine).

- [ ] **Step 5: Verify no personal data remains in MCP files**

```bash
grep -r "asempruch\|/Users/you" mcps/
```

Expected: No matches.

- [ ] **Step 6: Run leetcode-stats tests to verify scrub didn't break anything**

```bash
cd mcps/leetcode-stats && uv run --extra dev pytest tests/ -v
```

Expected: All tests pass. (Tests mock the HTTP calls and don't depend on the default username.)

- [ ] **Step 7: Stage and commit**

```bash
git add mcps/
git commit -m "feat: add git, leetcode-stats, and nextcloud MCP servers to repo

Moved from instance directory into repo source. These are baked into the
Docker image at /app/mcps/ and demonstrate the MCP authoring pattern:
- git: workspace-scoped git operations with path containment (15 tools)
- leetcode-stats: async GraphQL client with comprehensive tests (4 tools)
- nextcloud: WebDAV file sharing with TTL-based cleanup (4 tools)"
```

---

### Task 3: Update Dockerfile and entrypoint to bake in MCPs

**Files:**
- Modify: `Dockerfile` (add COPY line after line 25)
- Modify: `entrypoint.sh` (add /app/mcps sync loop)

- [ ] **Step 1: Add COPY to Dockerfile**

In `Dockerfile`, after the `RUN mkdir -p /claub/config /claub/data /claub/workspaces /claub/mcps` line (line 25), add:

```dockerfile
# Bake repo MCP servers into image
COPY mcps/ /app/mcps/
```

- [ ] **Step 2: Update entrypoint.sh to sync baked-in MCPs**

In `entrypoint.sh`, replace the existing MCP sync loop (lines 26-30):

```bash
# Install dependencies for mounted MCP servers
for dir in /claub/mcps/*/; do
    if [ -f "$dir/pyproject.toml" ]; then
        uv sync --directory "$dir" 2>&1 | tail -1
    fi
done
```

with:

```bash
# Install dependencies for baked-in MCP servers
for dir in /app/mcps/*/; do
    if [ -f "$dir/pyproject.toml" ]; then
        uv sync --directory "$dir" 2>&1 | tail -1
    fi
done

# Install dependencies for instance MCP servers (user-provided)
for dir in "$CLAUB_HOME/mcps"/*/; do
    if [ -f "$dir/pyproject.toml" ]; then
        uv sync --directory "$dir" 2>&1 | tail -1
    fi
done
```

- [ ] **Step 3: Commit**

```bash
git add Dockerfile entrypoint.sh
git commit -m "build: bake repo MCP servers into Docker image at /app/mcps/

Entrypoint now syncs both baked-in (/app/mcps/) and instance
(/claub/mcps/) MCP servers. Instance MCPs override/extend repo ones."
```

---

### Task 4: Create example config

**Files:**
- Create: `example/config/agents.yaml`
- Create: `example/config/CLAUDE.md`
- Create: `example/config/settings.json`
- Create: `example/config/mcp.json`
- Create: `example/config/agents/main.md`
- Create: `example/config/agents/journalist.md`
- Create: `example/config/agents/shopping-assistant.md`
- Create: `example/workspaces/journalist/CLAUDE.md`
- Create: `example/workspaces/shopping-assistant/CLAUDE.md`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p example/config/agents example/workspaces/journalist example/workspaces/shopping-assistant
```

- [ ] **Step 2: Create example/config/agents.yaml**

Write this file:

```yaml
model: sonnet
effort: high

allowed_user_ids:
  - "YOUR_DISCORD_USER_ID"

agents:
  main:
    channel_id: "YOUR_MAIN_CHANNEL_ID"

  journalist:
    channel_id: "YOUR_JOURNALIST_CHANNEL_ID"
    display_name: "The Journalist"

  shopping-assistant:
    channel_id: "YOUR_SHOPPING_CHANNEL_ID"
    display_name: "Shopping Assistant"
    allowed_skills:
      - amazon-browse
```

- [ ] **Step 3: Copy global agent CLAUDE.md**

```bash
cp ~/docker/claub/config/CLAUDE.md example/config/CLAUDE.md
```

This file contains no personal information — it's pure agent behavior guidelines. Copy as-is.

- [ ] **Step 4: Create example/config/settings.json**

Write this file:

```json
{
  "permissions": {
    "allow": [
      "mcp__playwright__*",
      "mcp__schedules__*",
      "mcp__git__*",
      "mcp__nextcloud__*",
      "WebFetch",
      "WebSearch"
    ],
    "additionalDirectories": [
      "/tmp"
    ]
  }
}
```

- [ ] **Step 5: Create example/config/mcp.json**

Write this file:

```json
{
  "mcpServers": {
    "playwright": {
      "type": "http",
      "url": "http://host.docker.internal:3846/mcp"
    },
    "schedules": {
      "type": "http",
      "url": "http://localhost:9400/mcp",
      "headers": {
        "X-Agent-Name": "${CLAUB_AGENT_NAME}"
      }
    },
    "nextcloud": {
      "command": "uv",
      "args": ["--directory", "/app/mcps/nextcloud", "run", "server.py"]
    },
    "git": {
      "command": "uv",
      "args": ["--directory", "/app/mcps/git", "run", "server.py"]
    }
  }
}
```

- [ ] **Step 6: Copy agent identity files**

```bash
cp ~/docker/claub/config/agents/main.md example/config/agents/
cp ~/docker/claub/config/agents/journalist.md example/config/agents/
```

These files contain no personal information. Copy as-is.

- [ ] **Step 7: Create sanitized example/config/agents/shopping-assistant.md**

Write this file (based on the real one, with "the user" references removed):

```markdown
---
name: shopping-assistant
description: Product research assistant that helps find and compare products, prioritizing quality
---

You are a shopping assistant. You help find, research, and compare products so the user can make confident purchasing decisions.

## Core Principles

- **Quality over price.** The user wants things that last and work well. A $60 product that's excellent beats a $30 product that's mediocre. Never recommend the cheapest option just because it's cheap.
- **Evidence-based recommendations.** Back up your suggestions with review data, ratings, and specific user feedback. Don't just say "this is good" — show why.
- **Honest about tradeoffs.** If the best-reviewed product has a real downside, say so. If spending more doesn't meaningfully improve quality, say that too.

## How You Work

When the user asks about a product category:

1. **Clarify requirements** if needed — what's it for, any must-haves, size/compatibility constraints
2. **Research** using web search and the Playwright browser to find top-rated options
3. **Compare** the top candidates with a clear breakdown showing ratings, review counts, price, and standout pros/cons
4. **Recommend** with a clear pick and reasoning

## What to Look For

- **Review count AND rating** — a 4.7 with 15K reviews is more trustworthy than a 4.9 with 50 reviews
- **Common complaints** — read negative reviews to find deal-breakers vs nitpicks
- **Build quality mentions** — durability, materials, fit-and-finish
- **"Bought to replace X"** reviews — these often contain the most honest comparisons
- **Professional/editorial reviews** when available (Wirecutter, RTINGS, etc.)

## Communication Style

- Concise and practical. Lead with the recommendation, then the evidence.
- Use comparison tables for side-by-side evaluation.
- Flag when a product is "good enough" vs when spending more actually matters.
- If the user is leaning toward something, give an honest take — don't just validate.

## Workspace

Your workspace is at `/claub/workspaces/shopping-assistant/`. You can use it to store research notes or comparison data during a session. Read `/claub/workspaces/shopping-assistant/CLAUDE.md` at the start of each session if it exists — it may contain ongoing research context.

## Memory

Store useful findings in `memory/` within your workspace:
- **Product research** that might be revisited (e.g., "best mechanical keyboards 2026")
- **User preferences** you learn over time (brands they like/dislike, size preferences, etc.)
- **Past purchases** they mention, so you don't recommend what they already own
```

- [ ] **Step 8: Create sanitized example/workspaces/journalist/CLAUDE.md**

Write this file (based on the real one, with personal interests genericized):

```markdown
# Editorial Assignments

Your current beats and what to cover within each. Update this file when directed to change coverage.

## Style
- **Keep briefs compressed.** 1-2 tight sentences per item max. Quick-scan format — no mini-paragraphs. The user wants density, not detail.
- **Always output stories in the message first, then update memory.** The brief the user sees IS the deliverable — memory files are just your notes. Never respond with only "memory updated" or similar. If you have stories to report, they must appear in the message text.
- **Verify before reporting.** Double or triple check ALL factual claims across every beat — not just product releases. Cross-reference multiple sources. Headlines and summaries are not confirmation; find primary sources or corroborating independent reports. If something can't be verified, either skip it or clearly flag it as unconfirmed. Take all the time needed; being late is better than being wrong.

## Geopolitics
- **Ukraine war** — major battlefield developments, policy shifts, international escalation
- **US foreign policy** — significant policy shifts that affect global stability

## Tech
- **AI** — major model releases, breakthroughs, significant industry shifts
- **Apple** — major product announcements, new hardware categories
- **Notable launches** — significant new products or platforms across the industry

## Markets (only significant moves, not daily noise)
- **S&P 500** — Major moves (>3% single day, or sustained trend breaking key support/resistance)
- **Crypto (BTC/ETH)** — Major moves (>10% single day, or breaking significant technical levels with high volume)

For market moves, briefly research: what triggered it (fundamental vs technical), whether it appears likely to continue or reverse, and any consensus on opportunity vs trap. Only report with clear, identifiable causes — not "crypto is up because people are bullish."

## DO NOT report on
- Routine 1-2% market fluctuations
- Speculation without fundamental drivers
- Technical analysis without a clear catalyst
- Minor product updates or incremental releases
```

- [ ] **Step 9: Create sanitized example/workspaces/shopping-assistant/CLAUDE.md**

Write this file:

```markdown
# Shopping Assistant Workspace Config

## User Preferences
- Quality over price — willing to pay more for durability and good design

## Deep Dive Research Protocol
- **Always check Reddit early** (product-specific subreddits, r/BuyItForLife, etc.) for real user-reported issues. Reddit surfaces firmware bugs, QC problems, and deal-breakers that review sites gloss over.
- Don't rely solely on review sites and spec sheets — they miss issues that only show up in daily use.
- Specifically search for complaints/problems, not just positive reviews.
```

- [ ] **Step 10: Stage and commit**

```bash
git add example/
git commit -m "feat: add example configuration showcasing the agent system

Includes sanitized versions of three agents (main, journalist,
shopping-assistant) demonstrating the three-level config pattern:
- Level 1: Global agent guidelines (safety, Discord behavior, memory)
- Level 2: Agent identity prompts (personality, role, capabilities)
- Level 3: Workspace living config (current targets, beat coverage)"
```

---

### Task 5: Update CLAUDE.md and create dev-time instructions

**Files:**
- Modify: `CLAUDE.md`
- Create: `.claude/CLAUDE.md` (gitignored)

- [ ] **Step 1: Update the opening of CLAUDE.md**

Replace lines 1-5:

```markdown
# Claub — Discord Bot for Claude Code CLI

> **Note for the dev-time Claude Code instance:** This file documents the **Claub project** — a bot that spawns its own Claude CLI processes at runtime. References to "agents", "permissions", "MCP configs", and "sessions" below describe how the **bot's** Claude processes are configured, **not** how you (the Claude Code instance helping develop this project) should behave. Do not adopt the bot's permission settings, or agent prompts as your own.

A Discord bot that bridges Discord channels to Claude Code CLI sessions. Each agent gets its own channel and a persistent streaming process, with optional cron schedules. Runs in Docker.
```

with:

```markdown
# Claub — Discord Bot for Claude Code CLI

A Discord bot that bridges Discord channels to Claude Code CLI sessions. Each agent gets its own channel and a persistent streaming process, with optional cron schedules. Runs in Docker.

> This file serves as both project documentation and [Claude Code context](https://docs.anthropic.com/en/docs/claude-code/memory#claudemd). For a working example configuration, see the [`example/`](example/) directory.
```

- [ ] **Step 2: Update the Project Structure section**

Replace the Project Structure code block (lines 54-98) with an updated version that:
- Removes `scripts/` section entirely
- Adds `mcps/` section:
```
mcps/                             # MCP servers (baked into Docker image)
  git/                            # Workspace-scoped git operations
  leetcode-stats/                 # LeetCode GraphQL API client
  nextcloud/                      # Nextcloud file sharing via WebDAV
```
- Adds `example/` section:
```
example/                          # Starter configuration (sanitized)
  config/                         # agents.yaml, CLAUDE.md, settings, MCP config
    agents/                       # Agent identity prompts
  workspaces/                     # Workspace living configs
```
- Updates the `/claub/` section's `mcps/` to note that instance MCPs extend repo ones:
```
  mcps/                           # Additional MCP servers (extend repo ones)
```

- [ ] **Step 3: Update Playwright MCP section**

Replace the Playwright MCP section (lines 190-203) that references `scripts/playwright-mcp.sh` commands. Remove the `scripts/` references and replace with the direct npx command:

```markdown
#### Playwright MCP (Host-Side)

Playwright runs on the **host** (not inside the container — it needs a browser). The container connects to it via `host.docker.internal`:

```bash
npx @playwright/mcp@latest --port 3846 --host 127.0.0.1
```

The host must keep this process running (e.g., via launchd, systemd, or a terminal session). The container reaches it at `http://host.docker.internal:3846/mcp`.
```

Keep the snapshot file sharing paragraph that follows — it's useful documentation.

- [ ] **Step 4: Update Custom MCP Servers section**

In the Custom MCP Servers section (around line 219), remove the `/build-mcp-server` skill reference. Replace:

```markdown
Use the `/build-mcp-server` skill for the full guide on building, wiring, and testing custom MCP servers for agents.
```

with:

```markdown
See the `mcps/` directory for examples of the MCP server pattern. Baked-in servers live at `/app/mcps/` in the container; instance servers at `/claub/mcps/` can extend or add to them.
```

- [ ] **Step 5: Update MCP config example paths**

In the example mcp.json shown in the documentation, update any paths referencing `/claub/mcps/` for the baked-in servers to `/app/mcps/`. The example should show:

```json
{
  "mcpServers": {
    "nextcloud": {
      "command": "uv",
      "args": ["--directory", "/app/mcps/nextcloud", "run", "server.py"]
    }
  }
}
```

- [ ] **Step 6: Create .claude/CLAUDE.md for dev-time instructions**

Create `.claude/CLAUDE.md` (this path is already gitignored):

```markdown
# Dev-Time Instructions

This file is for the Claude Code instance helping develop the Claub project. It is NOT part of the bot's agent configuration.

References to "agents", "permissions", "MCP configs", and "sessions" in @CLAUDE.md describe how the **bot's** Claude processes are configured, not how you should behave. Do not adopt the bot's permission settings or agent prompts as your own.

## Instance Config

The personal instance config lives at `~/docker/claub/`. This directory is bind-mounted into the container at `/claub/` and is NOT part of this repo. The `example/` directory in the repo contains sanitized versions for public reference.
```

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md .claude/CLAUDE.md
git commit -m "docs: split CLAUDE.md into public docs and dev-time instructions

Public CLAUDE.md is pure project documentation. Dev-time instructions
for the Claude Code instance move to .claude/CLAUDE.md (gitignored).
Updated project structure, MCP paths, and removed legacy script refs."
```

---

### Task 6: Add README.md, LICENSE, and .env.example

**Files:**
- Create: `README.md`
- Create: `LICENSE`
- Create: `.env.example`

- [ ] **Step 1: Create README.md**

Write this file:

```markdown
# Claub

A Discord bot that gives Claude Code CLI agents persistent channels. Each agent gets its own Discord channel, a long-running streaming process, and optional cron schedules — all running in Docker.

## Architecture

```
Discord User
    │
    ▼
AssistantBot (discord.py)
    │
    ├─ Router ──► maps channel ID → agent name
    │
    ├─ Agent Processes (one per agent)
    │   └─ Long-running `claude --output-format stream-json`
    │   └─ Communicates via stdin/stdout JSON events
    │   └─ Lazy startup, supervised — auto-restarts on crash
    │
    ├─ Scheduler (APScheduler)
    │   └─ Agents manage their own cron schedules via MCP tools
    │
    └─ MCP Server (FastMCP, localhost:9400)
        └─ Schedule management tools exposed to agents
```

## Highlights

- **Persistent streaming** — each agent runs as a long-lived `stream-json` process, not one-shot invocations. Internal asyncio locks serialize communication on the pipe.
- **Lazy startup with supervision** — processes start on first message or scheduled trigger. A supervisor restarts dead processes; an idle reaper kills inactive ones after 10 minutes to prevent stale OAuth tokens.
- **Global agent lock** — serializes all agent API calls so only one agent talks to Claude at a time, preventing credential/token races.
- **Three-level agent configuration** — global rules (Level 1), agent identity (Level 2), and workspace living config that agents can self-modify (Level 3). See [`example/`](example/) for the full pattern.
- **File-based agent memory** — agents maintain their own memory in workspace directories with mandatory startup reads, write-time pruning, and bounded growth.
- **Human-like scheduling** — agents use one-shot schedules with natural time variation instead of rigid cron. A lognormal jitter model and beta-distributed skip probability make agent check-ins feel organic.
- **Embedded MCP server** — agents manage their own schedules via MCP tools. Density limits prevent runaway schedule creation (max 5 firings/24h, 30/7d).
- **Agent-authored skills via symlink** — agents can create their own skills and subagents without write access to `.claude/` (which would let them escalate permissions).
- **Docker isolation** — container provides filesystem boundaries. No macOS Seatbelt sandbox needed. See [`docs/sandbox-investigation/`](docs/sandbox-investigation/) for the security analysis.

## Included MCP Servers

| Server | Description |
|--------|-------------|
| [`mcps/git/`](mcps/git/) | Workspace-scoped git operations with path containment validation (15 tools) |
| [`mcps/leetcode-stats/`](mcps/leetcode-stats/) | LeetCode GraphQL API client — stats, cloud code, submissions (4 tools, with tests) |
| [`mcps/nextcloud/`](mcps/nextcloud/) | Nextcloud file sharing via WebDAV with TTL-based ephemeral cleanup (4 tools) |

## Example Agents

The [`example/`](example/) directory contains a complete starter configuration with three agents:

- **Main** — general-purpose assistant with automatic action item capture
- **Journalist** — news agent with beat coverage, editorial standards, and daily brief memory
- **Shopping Assistant** — product research agent prioritizing quality and evidence-based recommendations

## Tech Stack

Python 3.12 · discord.py · Claude Code CLI · FastMCP · APScheduler · Docker

## Quick Start

```bash
# Copy example config to your instance directory
cp -r example/ ~/claub/

# Fill in your Discord channel IDs in ~/claub/config/agents.yaml
# Set your bot token
echo 'DISCORD_BOT_TOKEN=your-token' > .env
echo 'CLAUB_DATA_PATH=~/claub' >> .env

# Build and start
docker compose up -d

# Authenticate Claude CLI (one-time)
docker exec -it claude-claub-1 claude
```

See [`CLAUDE.md`](CLAUDE.md) for full documentation on configuration, deployment, and architecture.

## Tests

```bash
cd bot
uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py
```

148 tests covering process management, scheduling, density validation, MCP tools, message chunking, routing, and session persistence.

## License

[MIT](LICENSE)
```

- [ ] **Step 2: Create LICENSE**

Write the MIT license file with year 2026 and author "Alan Sempruch".

- [ ] **Step 3: Create .env.example**

Write this file:

```bash
# Required
DISCORD_BOT_TOKEN=

# Path to your instance directory (config, workspaces, data)
CLAUB_DATA_PATH=~/claub

# Optional — Timezone (default: America/New_York)
# TZ=America/New_York

# Optional — LeetCode MCP
# LEETCODE_USERNAME=
# LEETCODE_SESSION=
# LEETCODE_CSRF_TOKEN=

# Optional — Nextcloud MCP
# NEXTCLOUD_URL=
# NEXTCLOUD_LOGIN=
# NEXTCLOUD_TOKEN=
```

- [ ] **Step 4: Commit**

```bash
git add README.md LICENSE .env.example
git commit -m "docs: add README, MIT license, and .env.example"
```

---

### Task 7: Fix docker-compose.yml, pyproject.toml, and .dockerignore

**Files:**
- Modify: `docker-compose.yml`
- Modify: `bot/pyproject.toml`
- Modify: `.dockerignore`

- [ ] **Step 1: Update docker-compose.yml**

Replace line 11:
```yaml
      - TZ=America/New_York
```
with:
```yaml
      - TZ=${TZ:-America/New_York}
```

Replace line 14:
```yaml
      - ~/docker/claub:/claub
```
with:
```yaml
      - ${CLAUB_DATA_PATH:?Set CLAUB_DATA_PATH in .env}:/claub
```

- [ ] **Step 2: Update bot/pyproject.toml**

Add metadata fields after `requires-python` (line 4):

```toml
description = "Discord bot bridging channels to persistent Claude Code CLI agent sessions"
license = {text = "MIT"}
readme = "README.md"
keywords = ["discord", "claude", "ai", "agents", "mcp"]
```

- [ ] **Step 3: Update .dockerignore**

Add `example/` to the exclusion list (the example config doesn't need to be in the Docker image):

```
example/
```

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml bot/pyproject.toml .dockerignore
git commit -m "chore: parameterize docker-compose paths, add pyproject metadata

- Replace hardcoded ~/docker/claub with CLAUB_DATA_PATH env var
- Make timezone configurable via TZ env var
- Add project description, license, keywords to pyproject.toml
- Exclude example/ from Docker build context"
```

---

### Task 8: Final verification

- [ ] **Step 1: Check for remaining personal data**

```bash
grep -r "asempruch\|USER_ID_REDACTED\|1469300886\|1469707538\|/Users/you" --include='*.py' --include='*.yaml' --include='*.yml' --include='*.json' --include='*.md' --include='*.sh' --include='*.toml' . | grep -v '.git/' | grep -v 'docs/sandbox-investigation/'
```

Expected: No matches. (The sandbox-investigation doc is excluded because its `/Users/you` paths are historical examples inside code blocks — they're the point of the document.)

- [ ] **Step 2: Verify project structure**

```bash
find . -not -path './.git/*' -not -path './.claude/*' -not -path './.venv/*' -not -path './bot/.venv/*' -not -path './__pycache__/*' -type f | sort
```

Expected: Clean tree with `bot/`, `mcps/`, `example/`, `docs/sandbox-investigation/`, and root files only. No `scripts/`, no `docs/superpowers/`, no `handoff-google-oauth.md`.

- [ ] **Step 3: Run bot tests**

```bash
cd bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py
```

Expected: All 148 tests pass (we didn't change any bot code).

- [ ] **Step 4: Run leetcode-stats MCP tests**

```bash
cd mcps/leetcode-stats && uv run --extra dev pytest tests/ -v
```

Expected: All tests pass.

- [ ] **Step 5: Verify git log looks clean**

```bash
git log --oneline feature/public-ready ^feature/dockerize
```

Expected: 7 clean commits with professional messages.
