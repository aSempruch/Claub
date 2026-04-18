# Playwright Bridge

Host-side daemon that spawns a per-agent `@playwright/mcp` child with its own
`--user-data-dir` so each agent has a persistent browser profile (cookies, logins)
that survives bot and host restarts.

Driven by bot lifecycle hooks (`on_start` / `on_stop` in `agents.yaml`). Not tied
to `docker compose`; the bridge stays resident via launchd and spawns / kills
Playwright children on demand.

## Install

1. Copy `config.example.json` somewhere host-local (e.g.
   `~/Library/Application Support/claub-playwright-bridge/config.json`) and edit:
   - Agent list: add or remove entries to match your `agents.yaml`.
   - `port` per agent: any free port; convention is 3846 + offset.
   - `user_data_dir`: where the profile lives on the host. The bridge creates
     the directory on first use — no need to `mkdir` yourself. Default is
     `~/Library/Application Support/claub-playwright-bridge/profiles/{agent}/`.

2. Copy `com.asempruch.playwright-bridge.plist.template` to
   `~/Library/LaunchAgents/com.asempruch.playwright-bridge.plist` and replace the
   `/ABSOLUTE/PATH/TO/...` placeholders plus `YOURNAME`.

3. Create the log directory and load:
   ```
   mkdir -p ~/Library/Logs/claub-playwright-bridge
   launchctl load ~/Library/LaunchAgents/com.asempruch.playwright-bridge.plist
   curl http://127.0.0.1:9500/status   # expect {}
   ```

## Wire into the bot

Add global hooks at the top of `/claub/config/agents.yaml`:

```yaml
on_start:
  - "curl -fsS --max-time 20 -X POST http://host.docker.internal:9500/start/$CLAUB_AGENT_NAME || true"
on_stop:
  - "curl -fsS --max-time 10 -X POST http://host.docker.internal:9500/stop/$CLAUB_AGENT_NAME || true"
```

The trailing `|| true` keeps a flaky bridge from blocking agent startup. The
bridge returns 204 for agents that aren't in its config, so the same hook is
safe to apply globally even if some agents don't use a browser.

In each agent's `.mcp.json` (per-agent, not shared), add:

```json
{
  "mcpServers": {
    "playwright": {
      "type": "http",
      "url": "http://host.docker.internal:<PORT>/mcp"
    }
  }
}
```

`<PORT>` matches that agent's entry in the bridge config.

Remove the `playwright` entry from the shared `/claub/config/mcp.json`.

## Network binding

Default `listen_host` is `127.0.0.1` — loopback only, not reachable from your LAN. Both Docker Desktop for Mac and Colima forward container-originated `host.docker.internal:<port>` connections through to the host's loopback interface, so this works fine.

Do **not** change it to `0.0.0.0` unless you have a specific reason — that would expose the bridge (which can spawn browser processes on your machine) to anyone on your network.

## Ops

- Logs: `~/Library/Logs/claub-playwright-bridge/stderr.log`
- Live status: `curl http://127.0.0.1:9500/status`
- Restart: `launchctl kickstart -k gui/$(id -u)/com.asempruch.playwright-bridge`
- Stop everything: `launchctl unload ~/Library/LaunchAgents/com.asempruch.playwright-bridge.plist`
  (child Playwright processes are killed via SIGTERM handler)

## Why this exists

See the "Playwright MCP (Host-Side)" section of the repo's `CLAUDE.md` — hooks
have to pre-spawn the per-agent Playwright MCP server on the host before
`claude` is exec'd, because Claude Code's own `SessionStart` hook does not
block initial MCP connection (empirically verified).
