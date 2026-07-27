# Exec Bridge

Host-side daemon that spawns a throwaway `claub-exec` container per
`mcp__sandbox__run` / `install` call, mounting only the calling agent's
workspace. The Docker socket never enters the bot container — this daemon holds
that authority on the host. Mirrors `scripts/playwright-bridge/`.

## Endpoints

| Method | Path | Body | Network |
|---|---|---|---|
| POST | `/exec/{agent}` | `{"command", "timeout"}` | **always `none`** |
| POST | `/install/{agent}` | `{"packages": [str]}` — no command field | bridge |
| GET | `/status` | — | — |

**Do not merge these into one endpoint.** A network flag in the request body
moves the decision into the bot container — the party the agent allowlist
already assumes may be compromised. A command-shape check is spoofable:
`run("echo 'uv pip install '; curl http://host.docker.internal:9500/status")`
would take the networked path and reach the Playwright bridge. `/install`
accepts package names only; the bridge validates each against
`^[A-Za-z0-9._-]+(==[A-Za-z0-9._-]+)?$` and builds the argv itself, so the
networked path cannot run an arbitrary command at all.

## Prerequisites

- Build the image first: `docker build -t claub-exec docker/exec-sandbox/`
  (see `docker/exec-sandbox/README.md`).
- Colima running with `--cpu 4 --memory 4`.

## Install

1. Copy `config.example.json` host-local (e.g.
   `~/Library/Application Support/claub-exec-bridge/config.json`) and edit:
   - `workspaces_root`: the HOST path bind-mounted into the bot at `/claub/workspaces`.
     **It must live under a path Colima shares into the VM** — on this host that
     is `/Users/you` (`colima ssh -- mount` lists the shares). A source outside
     those shares mounts an *empty* directory rather than failing, so the
     sandbox silently sees nothing.
   - `secret`: must equal `EXEC_BRIDGE_SECRET` in `.envrc` (see "Secret" below).
   - `agents`: allowlist — must agree with `agents.yaml`; a mismatch surfaces as a 404.
   - `extra_mounts` per agent: replicate any compose submount whose host source
     differs from `{workspace}/...`. **`leetcode-coach` needs the `shared-data`
     entry** — `docker-compose.yml` bind-mounts `/Users/you/repos/shared/data`
     into the bot at `{workspace}/shared-data`, but on the host that workspace
     subdir is empty. Without the extra mount the sandbox silently sees nothing.
     Any FUTURE compose submount must be added in BOTH places.

2. Copy the plist template to
   `~/Library/LaunchAgents/com.asempruch.exec-bridge.plist`, replace the
   `/ABSOLUTE/PATH/TO/...` placeholders and `YOURNAME`. The plist pins
   `DOCKER_HOST` to the colima socket — required, launchd envs are minimal.

3. Load:
   ```
   mkdir -p ~/Library/Logs/claub-exec-bridge
   launchctl load ~/Library/LaunchAgents/com.asempruch.exec-bridge.plist
   curl http://127.0.0.1:9501/status   # expect {"running": {}}
   ```

## Secret

The MCP sends `X-Exec-Secret`; the bridge checks it against `config.json`'s
`secret`. Thread the same value end to end: host `.envrc` (`export
EXEC_BRIDGE_SECRET=...`) → `docker-compose.yml` `environment` → the bot
container → the sandbox MCP's `env` block in the per-agent `.mcp.json`. The
secret is NEVER forwarded into the sandbox container (Docker gives a container
only the image ENV plus explicit `-e` flags), so sandbox code cannot reach the
bridge even though `install` has network.

## Network binding

`listen_host` is `127.0.0.1` (loopback). Colima forwards container-originated
`host.docker.internal:9501` through to host loopback. Do NOT set `0.0.0.0`.

## Housekeeping

`{workspace}/.venv`, `{workspace}/.uv-cache` accumulate per agent. Prune
manually or let the bot's startup sweep cover them.

## Ops

- Logs: `~/Library/Logs/claub-exec-bridge/stderr.log`
- Status: `curl http://127.0.0.1:9501/status`
- Restart: `launchctl kickstart -k gui/$(id -u)/com.asempruch.exec-bridge`
