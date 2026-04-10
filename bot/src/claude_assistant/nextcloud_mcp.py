"""FastMCP server for Nextcloud file sharing.

Provides tools for agents to upload files to Nextcloud and get share links.
Runs as an HTTP server alongside the schedules MCP.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import fastmcp
import fastmcp.server.dependencies
from starlette.requests import Request

from claude_assistant.nextcloud_client import NextcloudClient

log = logging.getLogger(__name__)


def _get_agent(request: Request) -> str:
    return request.headers.get("x-agent-name", "")


async def _share_file(
    client: NextcloudClient,
    agent: str,
    file_path: str,
    persistent: bool,
    expire_days: int,
) -> str:
    """Upload a file to Nextcloud and create a share link."""
    local = Path(file_path)
    if not local.exists():
        return f"Error: file not found: {file_path}"

    bucket = "persistent" if persistent else "ephemeral"
    remote_dir = f"claub/{bucket}/{agent}"
    remote_path = f"{remote_dir}/{local.name}"

    await client.mkdir_p(remote_dir)
    await client.upload(str(local), remote_path)

    expire_date = None
    if not persistent:
        expire_date = (datetime.now() + timedelta(days=expire_days)).strftime("%Y-%m-%d")

    share_data = await client.create_share(remote_path, expire_date=expire_date)

    return json.dumps({
        "url": share_data["url"],
        "file": local.name,
        "bucket": bucket,
        "expires": expire_date,
    })


async def _list_shares(client: NextcloudClient, agent: str, persistent: bool) -> str:
    """List all shares for an agent's files in the given bucket."""
    bucket = "persistent" if persistent else "ephemeral"
    remote_dir = f"claub/{bucket}/{agent}"

    try:
        shares = await client.list_shares(remote_dir)
    except Exception:
        return json.dumps([])

    result = []
    for share in shares:
        path = share.get("path", "")
        name = path.rsplit("/", 1)[-1] if "/" in path else path
        result.append({
            "name": name,
            "url": share.get("url", ""),
            "expiration": share.get("expiration"),
        })
    return json.dumps(result)


async def _delete_shared_file(client: NextcloudClient, agent: str, file_name: str) -> str:
    """Delete a file from Nextcloud, searching both ephemeral and persistent buckets."""
    for bucket in ("ephemeral", "persistent"):
        remote_dir = f"claub/{bucket}/{agent}"
        try:
            files = await client.list_files(remote_dir)
        except Exception:
            continue

        for f in files:
            if f["name"] == file_name:
                remote_path = f"{remote_dir}/{file_name}"
                # Delete associated shares
                try:
                    shares = await client.list_shares(remote_dir)
                    for share in shares:
                        if share.get("path", "").rstrip("/").endswith(file_name):
                            await client.delete_share(share["id"])
                except Exception:
                    log.warning("Failed to delete shares for %s", remote_path)
                # Delete the file
                await client.delete(remote_path)
                return f"Deleted {file_name} from {bucket}."

    return f"Error: {file_name} not found in ephemeral or persistent storage."


async def _run_cleanup(client: NextcloudClient, ttl_days: int) -> int:
    """Delete ephemeral files older than ttl_days. Returns count of deleted files."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    deleted = 0

    try:
        files = await client.list_files("claub/ephemeral")
    except Exception:
        log.warning("Cleanup: failed to list ephemeral files")
        return 0

    for f in files:
        if not f.get("last_modified"):
            continue
        try:
            file_time = parsedate_to_datetime(f["last_modified"])
        except (ValueError, TypeError):
            continue

        if file_time < cutoff:
            # Extract the relative path from href: everything after /files/{user}/
            href = f["href"]
            dav_marker = "/files/"
            idx = href.find(dav_marker)
            if idx == -1:
                continue
            after_files = href[idx + len(dav_marker):]
            remote_path = after_files.split("/", 1)[1] if "/" in after_files else after_files

            # Try to delete associated shares
            try:
                parent_dir = remote_path.rsplit("/", 1)[0]
                shares = await client.list_shares(parent_dir)
                for share in shares:
                    if share.get("path", "").rstrip("/").endswith(f["name"]):
                        await client.delete_share(share["id"])
            except Exception:
                log.warning("Cleanup: failed to delete shares for %s", remote_path)

            try:
                await client.delete(remote_path)
                deleted += 1
                log.info("Cleanup: deleted %s", remote_path)
            except Exception:
                log.warning("Cleanup: failed to delete %s", remote_path)

    return deleted


async def run_cleanup_loop(client: NextcloudClient, ttl_days: int) -> None:
    """Background loop: run cleanup on startup then every 24 hours."""
    while True:
        try:
            count = await _run_cleanup(client, ttl_days)
            if count:
                log.info("Cleanup: deleted %d ephemeral files", count)
        except Exception:
            log.exception("Cleanup: unexpected error")
        await asyncio.sleep(86400)  # 24 hours


def create_nextcloud_mcp(client: NextcloudClient) -> fastmcp.FastMCP:
    """Create and return a FastMCP server with Nextcloud file sharing tools."""
    mcp = fastmcp.FastMCP(name="claub-nextcloud")

    @mcp.tool()
    async def share_file(
        file_path: str,
        persistent: bool = False,
        expire_days: int = 3,
        request: Request = fastmcp.server.dependencies.CurrentRequest(),  # type: ignore[assignment]
    ) -> str:
        """Upload a file to Nextcloud and return a public share link.

        The link opens Nextcloud's built-in viewer (PDF viewer for PDFs, image preview, etc.).

        Args:
            file_path: Absolute path to the file in your workspace.
            persistent: If True, file is kept permanently. If False (default), auto-cleaned after expire_days.
            expire_days: Days until the share link expires (only for ephemeral files, default 3).
        """
        agent = _get_agent(request)
        if not agent:
            return "Error: missing X-Agent-Name header"
        return await _share_file(client, agent, file_path, persistent, expire_days)

    @mcp.tool()
    async def list_shares(
        persistent: bool = False,
        request: Request = fastmcp.server.dependencies.CurrentRequest(),  # type: ignore[assignment]
    ) -> str:
        """List shared files for the requesting agent.

        Args:
            persistent: If True, list persistent files. If False (default), list ephemeral files.
        """
        agent = _get_agent(request)
        if not agent:
            return "Error: missing X-Agent-Name header"
        return await _list_shares(client, agent, persistent)

    @mcp.tool()
    async def delete_shared_file(
        file_name: str,
        request: Request = fastmcp.server.dependencies.CurrentRequest(),  # type: ignore[assignment]
    ) -> str:
        """Delete a shared file from Nextcloud (searches both ephemeral and persistent).

        Args:
            file_name: The filename to delete (e.g., resume_v3.pdf).
        """
        agent = _get_agent(request)
        if not agent:
            return "Error: missing X-Agent-Name header"
        return await _delete_shared_file(client, agent, file_name)

    return mcp
