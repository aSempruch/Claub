---
name: build-mcp-server
description: Use when building, wiring, or testing a NEW custom MCP server for a Claub agent — covers server structure, FastMCP patterns, config wiring, permissions, and isolated testing
---

# Building Custom MCP Servers

Build a small MCP server when an agent needs access to an external API or capability. This gives least-privilege access — the agent can only call the tools the server exposes. **Prefer an MCP server over granting Bash permissions to run scripts.**

For servers that already exist (Nextcloud, HASS, Google, git, file-download), use the `claub-mcp-servers` skill instead.

## Location & Structure

Instance servers live in `/claub/mcps/{server-name}/` inside the container — bind-mounted from `~/docker/claub/mcps/{server-name}/` on the host. This keeps custom MCPs with the instance config, not in the repo.

```
~/docker/claub/mcps/{server-name}/      # host path
  pyproject.toml    # dependencies: mcp[cli], plus any needed libs (httpx, etc.)
  server.py         # FastMCP server with @mcp.tool() decorated functions
```

Servers meant to ship with the project instead go in the repo's `mcps/`, which is baked into the image at `/app/mcps/`.

## Minimal Server Pattern

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("server-name")

@mcp.tool()
async def my_tool(param: str) -> str:
    """Tool description — used by Claude to understand when to call it."""
    # ... implementation ...
    return result

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

## Minimal pyproject.toml

No build-system needed for script-only servers:

```toml
[project]
name = "server-name"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["mcp[cli]"]
```

## Wiring It Up

1. Create the server in `~/docker/claub/mcps/{server-name}/`.
2. Add `/claub/config/agents/{agent}.mcp.json`:
   ```json
   {
     "mcpServers": {
       "server-name": {
         "command": "uv",
         "args": ["--directory", "/claub/mcps/{server-name}", "run", "server.py"]
       }
     }
   }
   ```
   Paths inside this file are **container** paths (`/claub/...`), not host paths.
3. Add `"mcp__{server-name}__*"` to `permissions.allow` in `/claub/config/settings.json`. **This allow list is the hard ceiling** — a tool missing from it is silently unavailable.
4. Update the agent's `.md` prompt to reference the MCP tool instead of scripts.
5. `docker compose restart` — the entrypoint runs `uv sync` for every `/claub/mcps/*` dir with a `pyproject.toml` on each start, so dependencies install automatically.
6. Test (below).

## Key Rules

- **No `print()`** in MCP servers — it corrupts the stdio JSON-RPC stream. Use `logging` or `print(..., file=sys.stderr)`. Stderr is visible in `docker compose logs`.
- Tools can be sync or async. `@mcp.tool()` auto-generates JSON schema from type hints and docstrings.
- Need the calling agent's identity? Read `CLAUB_AGENT_NAME` from the environment — the bot sets it per process.

## Testing

Test in isolation first — no `--agent`, no shared MCP configs. Agent personas and shared MCPs can mask failures.

```bash
docker exec claude-claub-1 claude -p \
  --permission-mode acceptEdits \
  --mcp-config /claub/config/agents/{agent}.mcp.json \
  --no-session-persistence --output-format json \
  -- "Call mcp__{server-name}__{tool} and show the raw result"
```

Then the full agent test, which loads the real persona, both MCP configs, and the allow list:

```bash
docker exec claude-claub-1 uv run --project /app/bot \
  python -m claude_assistant.debug_agent {agent} -p "test prompt"
```

The debug CLI never touches `sessions.json`. See the `claub-logs` skill for more on it.
