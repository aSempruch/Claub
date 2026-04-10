# Nextcloud File Sharing MCP Server

**Date:** 2026-04-10
**Status:** Draft

## Problem

Claub agents produce files (PDFs, documents) that are sent to Discord as attachments via the `[FILE:]` marker system. Users must download each file to view it — there's no way to preview in-browser. For iterative workflows like resume editing, this creates significant friction.

## Solution

A new MCP server that uploads agent-produced files to an existing Nextcloud instance and returns clickable share links. Nextcloud's built-in PDF viewer renders files inline, eliminating the download step.

## Architecture

```
Agent writes file to workspace
    │
    ▼
Agent calls mcp__nextcloud__share_file(path, persistent=false)
    │
    ▼
Nextcloud MCP Server (localhost:9401)
    ├─ WebDAV PUT → uploads to /claub/ephemeral/{agent}/{filename}
    │                (or /claub/persistent/{agent}/)
    ├─ OCS POST  → creates public share link (with expireDate if ephemeral)
    └─ Returns share URL to agent
    │
    ▼
Agent includes URL in its Discord response
```

The MCP server runs inside the Claub container alongside the existing schedules MCP. All communication with Nextcloud goes through the existing HTTPS endpoint — no new ports exposed on Nextcloud.

## MCP Tools

### `share_file(file_path: str, persistent: bool = False, expire_days: int = 3) -> str`

Uploads a file from the agent's workspace to Nextcloud and creates a public share link.

- **`file_path`**: Absolute path to the file in the agent's workspace
- **`persistent`**: If `False`, uploads to `/claub/ephemeral/{agent}/`, share link gets `expireDate` set to `expire_days` from now. If `True`, uploads to `/claub/persistent/{agent}/`, no expiry.
- **`expire_days`**: Number of days until the share link expires (ephemeral only, default 3)
- **Returns**: The public share URL (e.g., `https://cloud.example.com/index.php/s/{token}`)

Implementation:
1. Ensure target directory exists (MKCOL, idempotent)
2. Upload file via WebDAV PUT to `/remote.php/dav/files/{user}/claub/{ephemeral|persistent}/{agent}/{filename}`
3. Create public share via OCS POST to `/ocs/v2.php/apps/files_sharing/api/v1/shares` with `shareType=3`, `permissions=1`, and optional `expireDate`
4. Return `data.url` from OCS response

If a file with the same name exists, it is overwritten (WebDAV PUT returns 204). The old share link continues to work if one existed.

### `list_shares(persistent: bool = False) -> list`

Lists the calling agent's current shared files.

- **`persistent`**: Which folder to list (`ephemeral` or `persistent`)
- **Returns**: List of `{name, url, expiration}` objects

Implementation: OCS GET `/ocs/v2.php/apps/files_sharing/api/v1/shares` filtered by `path=/claub/{ephemeral|persistent}/{agent}`.

### `delete_shared_file(file_path: str) -> bool`

Deletes a specific file and its associated share link from Nextcloud.

- **`file_path`**: The filename to delete (e.g., `resume_v3.pdf`). The server searches both `/claub/ephemeral/{agent}/` and `/claub/persistent/{agent}/` for the file.
- **Returns**: Success boolean

Implementation:
1. Find and delete any share links for the file via OCS DELETE `/ocs/v2.php/apps/files_sharing/api/v1/shares/{id}`
2. Delete the file via WebDAV DELETE

## Folder Structure on Nextcloud

```
claub/
  ephemeral/
    career/
      resume_v3.pdf
    journalist/
      weekly_report.pdf
  persistent/
    career/
      final_resume_2026.pdf
```

- `claub/` root and `ephemeral/`/`persistent/` subdirectories are created on MCP server startup
- Agent subdirectories are created on-demand during upload
- Filename collisions overwrite the existing file (expected for iterative workflows)

## Cleanup Mechanism

TTL-based sweep for the ephemeral folder:

- Runs as a background `asyncio` task inside the MCP server process
- Executes on server startup and then every 24 hours
- PROPFIND on `/claub/ephemeral/` with `Depth: infinity` to get all files and their `getlastmodified` timestamps
- Deletes files where age exceeds the TTL (default 3 days)
- Deletes associated share links (OCS list shares by path, then delete each)
- Removes empty agent subdirectories
- `/claub/persistent/` is never touched by cleanup

The TTL is configurable via `NEXTCLOUD_EPHEMERAL_TTL_DAYS` env var.

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `NEXTCLOUD_URL` | Yes | — | Base URL (e.g., `https://cloud.example.com:39640`) |
| `NEXTCLOUD_LOGIN` | Yes | — | Dedicated Nextcloud user for the bot |
| `NEXTCLOUD_TOKEN` | Yes | — | App password for the Nextcloud user |
| `NEXTCLOUD_MCP_PORT` | No | `9401` | Localhost port for the MCP server |
| `NEXTCLOUD_EPHEMERAL_TTL_DAYS` | No | `3` | Days before ephemeral files are deleted |

### Nextcloud Setup (One-Time)

1. Create a dedicated Nextcloud user (e.g., `claub`)
2. Log in as that user, go to Settings > Security > App Passwords
3. Generate an app password
4. Set the three required env vars in `.env`

### Integration with Claub

- New files: `bot/src/claude_assistant/nextcloud_mcp.py` (FastMCP server + cleanup task), `bot/src/claude_assistant/nextcloud_client.py` (httpx wrapper for WebDAV/OCS)
- MCP server started in `main.py` alongside the schedules MCP server
- Agent name resolved via `X-Agent-Name` header (same pattern as schedules MCP)
- Add `mcp__nextcloud__*` to `settings.json` permissions allow list
- MCP config wired the same way as schedules — added to the shared config automatically

### Nextcloud API Details

All operations use HTTP Basic Auth with `NEXTCLOUD_LOGIN:NEXTCLOUD_TOKEN`.

**WebDAV endpoints** (on same HTTPS port as Nextcloud UI):
- `MKCOL /remote.php/dav/files/{user}/path/` — create directory
- `PUT /remote.php/dav/files/{user}/path/file` — upload file
- `DELETE /remote.php/dav/files/{user}/path/file` — delete file
- `PROPFIND /remote.php/dav/files/{user}/path/` — list files with metadata

**OCS Share API** (requires `OCS-APIRequest: true` header, append `?format=json` for JSON):
- `POST /ocs/v2.php/apps/files_sharing/api/v1/shares` — create share
- `GET /ocs/v2.php/apps/files_sharing/api/v1/shares` — list shares
- `DELETE /ocs/v2.php/apps/files_sharing/api/v1/shares/{id}` — delete share

## What This Does NOT Change

- The existing `[FILE:]` marker system and Discord attachment flow remain untouched
- Agents choose whether to use `[FILE:]` (Discord attachment), `share_file` (Nextcloud link), or both
- No changes to `file_sender.py`, `discord_bot.py`, or `claude_process.py`
- This is purely additive — a new MCP server with new tools
