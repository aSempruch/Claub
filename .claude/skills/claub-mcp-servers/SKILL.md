---
name: claub-mcp-servers
description: Use when working on an MCP server that already ships with Claub — Nextcloud sharing, Home Assistant, Google Gmail/Calendar (invalid_grant, scope filtering, multi-account tokens), git, leetcode-stats, or file-download; covers where each lives, its env vars, and its failure modes
---

# Claub's Existing MCP Servers

For building a **new** server, use the `build-mcp-server` skill instead. This is the catalog of what already exists.

## Where Servers Live

| Location | Meaning |
|---|---|
| `/app/mcps/{name}/` | **Baked into the image** — source in the repo's `mcps/` |
| `/claub/mcps/{name}/` | **Instance servers** — bind-mounted from `~/docker/claub/mcps/`, not in the repo |

The entrypoint runs `uv sync` for any `/claub/mcps/*` subdirectory containing a `pyproject.toml` on every container start, so Python instance MCPs are always ready.

Wiring is via `--mcp-config`: shared (`/claub/config/mcp.json`, all agents) plus per-agent (`/claub/config/agents/{name}.mcp.json`). **`settings.json`'s `permissions.allow` is the hard ceiling** for what any agent can call.

## Catalog

| Server | Where | Tools | Notes |
|---|---|---|---|
| `git` | baked | 15 | Workspace-scoped, path-containment validated |
| `leetcode-stats` | baked | 5 | LeetCode GraphQL client |
| `nextcloud` | baked | 4 | File sharing via WebDAV |
| `hass` | baked | 3 | Deliberately narrow Home Assistant slice |
| `file-download` | baked | 1 | Least-privilege URL → workspace fetch |
| `google_{label}` | instance | varies | Gmail + Calendar, read-only, one per account |
| `notion` | npm-global | many | Third-party `@notionhq/notion-mcp-server`, pinned in the Dockerfile |
| `playwright` | host-side | many | See the `claub-playwright` skill |

## Nextcloud

Subprocess (stdio) server at `/app/mcps/nextcloud/`. Each agent spawns its own instance.

- **Setup:** dedicated Nextcloud user + app password (Settings > Security). Set `NEXTCLOUD_URL`, `NEXTCLOUD_LOGIN`, `NEXTCLOUD_TOKEN` in `.env`. Optional `NEXTCLOUD_EPHEMERAL_TTL_DAYS` (default 3).
- **Tools:** `share_file`, `list_shares`, `delete_shared_file`, `cleanup_ephemeral`.
- **Layout:** `claub/ephemeral/{agent}/` (auto-cleaned after TTL) or `claub/persistent/{agent}/` (permanent). Share links open Nextcloud's built-in viewer.
- **Cleanup:** runs automatically on process startup; `cleanup_ephemeral` triggers it manually.

## Home Assistant

Subprocess server at `/app/mcps/hass/`. **Not a general HASS bridge** — each tool is a typed wrapper around one entity or service, by design.

- **Setup:** `HASS_URL` and `HASS_TOKEN` (long-lived token from HA Profile > Security) in `.env`.
- **Tools:** `get_user_location`, `get_user_location_history`, `broadcast`.
- **Extending:** add a new `@mcp.tool()` in `mcps/hass/server.py` using the `_get_state(entity_id)`, `_get_history(entity_id, hours)`, or `_call_service(domain, service, payload)` helpers — they handle auth and HTTP. Shape the response to only the fields the agent needs, not raw HASS state JSON. Adding a *new capability* means writing a new tool, deliberately — never a generic stringly-typed `call_service`.

## file-download

Subprocess server at `/app/mcps/file-download/`. Least-privilege alternative to giving an agent a shell with `curl`.

- **Tool:** `download_file(url, dest_path, max_mb=50)`.
- Writes only inside `/claub/workspaces/$CLAUB_AGENT_NAME` (requires `CLAUB_AGENT_NAME`). Cannot read local files, execute, or reach internal services (SSRF-blocked). Hard cap 100 MB. Follows up to 5 redirects, **re-validating the host on every hop**.

## Notion

Third-party server, not ours. Installed globally into the image by the Dockerfile:

```dockerfile
RUN npm install -g @notionhq/notion-mcp-server@2.4.1
```

**Pinned on purpose** — bump the version deliberately, then `docker compose up -d --build`. `NOTION_TOKEN` is passed through in `docker-compose.yml`; agents opt in via their per-agent `.mcp.json` (plus the `mcp__notion__*` glob in `settings.json`).

## Google (Gmail + Calendar, read-only, multi-account)

An **instance** server at `/claub/mcps/google/server.py`. Exposes Gmail search/fetch and Calendar list/fetch against **one account at a time**. Multiple accounts = multiple MCP instances, each with its own token file.

**Account layout.** Each account gets `~/docker/claub/mcps/google/accounts/{label}/token.json` (mode 0600). The label identifies the *account being read*, **not** the GCP project owning the OAuth client — a refresh token from project A can authorize calls against account B. Token files are gitignored via `**/token.json` in the instance repo.

**Per-agent wiring** in `config/agents/{agent}.mcp.json`, conventionally named `google_{label}`:

```json
"google_rhs": {
  "command": "uv",
  "args": ["--directory", "/claub/mcps/google", "run", "server.py"],
  "env": {
    "GOOGLE_MCP_TOKEN": "/claub/mcps/google/accounts/rhs/token.json"
  }
}
```

Each agent spawns one subprocess per `google_*` entry, so per-account isolation is automatic and one agent can hold several accounts at once (each under its own `mcp__google_{label}__*` prefix). Each also needs `mcp__google_{label}__*` in `settings.json`'s allow list.

**Scope-based tool filtering.** At startup the server reads `token.json`'s `scopes` and registers only matching tool groups: `gmail_*` needs `gmail.readonly`, `calendar_*` needs `calendar.readonly`. A single-scope token exposes only half the catalog — agents never see tools they can't use. Startup writes one line to stderr (visible in `docker compose logs`):

```
google MCP: token=token.json, scopes=[...], registered=['gmail', 'calendar']
```

**Token replacement is hot for refresh-only swaps, cold for scope changes.** `token.json` is re-read on every `_creds()` call, so re-minting with the *same* scopes takes effect on the agent's next tool call. But the scope filter runs **once at startup** — adding or removing scopes requires `docker compose restart` before the tool catalog updates.

**Failure mode — `invalid_grant` / persistent 401:** the refresh token was revoked (user pulled access at <https://myaccount.google.com/permissions>, or the OAuth client/project changed upstream). Notify the user — re-minting needs a consent flow on the source machine.
