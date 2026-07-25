---
name: claub-playwright
description: Use when working on Claub's browser stack — per-agent Playwright profiles, the host-side bridge daemon, browser_file_upload ENOENT or allowed-roots errors, snapshot file sharing via /tmp/playwright, page zoom, or hCaptcha misalignment
---

# Claub Playwright Stack

Each agent gets its **own** persistent Playwright browser profile (cookies, localStorage, logins) that survives bot and host restarts, so agents can log into a site once and stay logged in. Other agents on the same site stay cleanly isolated.

**The bot itself is Playwright-agnostic.** It only runs generic shell hooks around each `claude` subprocess. All Playwright wiring lives in user config.

## Three Moving Parts

**1. Per-agent `--user-data-dir`.** Each agent's Playwright MCP process points at `~/docker/claub/playwright-profiles/{agent}/`. Playwright MCP can't multiplex profiles in a single server ([issue #1294](https://github.com/microsoft/playwright-mcp/issues/1294)), so one `@playwright/mcp` process runs per agent on its own port (main=3846, journalist=3847, …).

**2. Bridge daemon** (`scripts/playwright-bridge/bridge.py`). Stdlib-only Python HTTP service on the host that spawns/kills the per-agent MCP children on demand. Exposes `POST /start/<agent>`, `POST /stop/<agent>`, `GET /status`. Managed by launchd (`com.asempruch.playwright-bridge.plist`). Unknown agents get a **204 no-op**, so a global start hook is safe for browser-less agents. See `scripts/playwright-bridge/README.md`.

**3. Lifecycle hooks in `agents.yaml`:**

```yaml
on_start:
  - "curl -fsS --max-time 20 -X POST http://host.docker.internal:9500/start/$CLAUB_AGENT_NAME || true"
on_stop:
  - "curl -fsS --max-time 10 -X POST http://host.docker.internal:9500/stop/$CLAUB_AGENT_NAME || true"
```

Per-agent `.mcp.json` files point at that agent's port (e.g. `http://host.docker.internal:3847/mcp`). The shared `/claub/config/mcp.json` does **not** include `playwright`.

## Why Pre-Spawn, Not Claude Code's SessionStart Hook

Empirically, Claude Code fires its MCP connection handshake **in parallel with** (not after) `SessionStart` — the handshake can land 0.3 s *before* the hook even starts, and there is no initial-connection retry.

So the Playwright MCP must already be listening when `claude` is exec'd. That forces lifecycle management into the bot, not Claude. The `on_start` hook runs inside `AgentProcess.start()` and blocks on the `curl`, which blocks until Playwright's port is listening.

## Snapshot File Sharing

Agents save snapshots to `/tmp/playwright/` to keep large accessibility trees out of context. Playwright runs on the **host**, so the file lands in the host's `/tmp/playwright/`, bind-mounted read-only into the container at the same path.

This requires the Docker runtime to mount `/tmp/playwright` into the VM — **not** done by default. For Colima, add it to `mounts` in `~/.colima/default/colima.yaml`. Docker Desktop for Mac works out of the box (shares `/tmp`).

## File Uploads — the Host-Path Double Mount

`browser_file_upload` `fs.stat`s paths on the **host**, so a container-only path like `/claub/workspaces/career/resume.pdf` fails with **ENOENT**. Playwright MCP also enforces an allowed-roots check via the MCP `roots` capability — Claude Code sends its container cwd as a root, so host-native paths get rejected *before* `fs.stat` runs.

Both are needed:

1. **`--allow-unrestricted-file-access`** on each Playwright MCP instance (already in `scripts/playwright-bridge/config.example.json`'s `command_template`) — disables the roots-based restriction.
2. **Double mount** in `docker-compose.yml` — the host data dir is bind-mounted at both `/claub` and its native host path via `${CLAUB_DATA_PATH}:${CLAUB_DATA_PATH}`. The container exports `CLAUB_HOST_PATH=${CLAUB_DATA_PATH}`.

Agents use `/claub/...` normally, but swap in `$CLAUB_HOST_PATH/...` when handing a path to a file-chooser tool: `/claub/workspaces/career/resume.pdf` → `$CLAUB_HOST_PATH/workspaces/career/resume.pdf` → `/Users/you/docker/claub/workspaces/career/resume.pdf`, valid on both sides. Agent-facing docs for this live in `/claub/config/CLAUDE.md`.

## Page Zoom — Per-Profile, Never init.js

Goal: render at ~80% so more fits in the viewport.

**Do not use `documentElement.style.zoom = '80%'` in init.js.** It breaks hCaptcha: cross-origin iframes (the challenge frame) don't inherit CSS `zoom`, so the captcha lays out smaller than its parent-side iframe element and clicks misalign.

Instead each agent's `Default/Preferences` carries `profile.default_zoom_level` (and `partition.default_zoom_level.x`) — Chromium's **native** zoom (the Cmd+− equivalent), which works in the compositor and propagates to iframes correctly.

After adding a new agent and creating its bridge profile entry, run `scripts/playwright-bridge/apply-zoom-prefs.py` once. Idempotent — but **do not run it while Chromium has the profile open.**

## Common Mistakes

| Symptom | Cause |
|---|---|
| `ENOENT` on file upload | Passed a `/claub/...` path; use `$CLAUB_HOST_PATH/...` |
| Upload rejected before stat | Missing `--allow-unrestricted-file-access` |
| Captcha clicks land wrong | CSS `zoom` in init.js; use per-profile Chromium zoom |
| Snapshot file not found in container | `/tmp/playwright` not mounted into the Docker VM |
| MCP never connects at session start | Relying on Claude's `SessionStart`; must pre-spawn via `on_start` |
| New agent renders at 100% | Forgot `apply-zoom-prefs.py` after creating the profile |
