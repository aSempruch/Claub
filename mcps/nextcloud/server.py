"""MCP server for Nextcloud file sharing.

Uploads agent-produced files to Nextcloud and returns clickable share links.
Runs as a stdio subprocess — one instance per agent, spawned by Claude CLI.

Requires: CLAUB_AGENT_NAME, NEXTCLOUD_URL, NEXTCLOUD_LOGIN, NEXTCLOUD_TOKEN
Optional: NEXTCLOUD_EPHEMERAL_TTL_DAYS (default 3)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)

# --- Environment ---

AGENT_NAME = os.environ.get("CLAUB_AGENT_NAME")
if not AGENT_NAME:
    raise RuntimeError("CLAUB_AGENT_NAME environment variable is required")

NEXTCLOUD_URL = os.environ.get("NEXTCLOUD_URL")
NEXTCLOUD_LOGIN = os.environ.get("NEXTCLOUD_LOGIN")
NEXTCLOUD_TOKEN = os.environ.get("NEXTCLOUD_TOKEN")
if not (NEXTCLOUD_URL and NEXTCLOUD_LOGIN and NEXTCLOUD_TOKEN):
    raise RuntimeError("NEXTCLOUD_URL, NEXTCLOUD_LOGIN, and NEXTCLOUD_TOKEN are required")

TTL_DAYS = int(os.environ.get("NEXTCLOUD_EPHEMERAL_TTL_DAYS", "3"))

# --- Nextcloud client ---

DAV_NS = "DAV:"


class NextcloudClient:
    """Thin httpx wrapper for Nextcloud WebDAV and OCS Share APIs."""

    def __init__(self, base_url: str, username: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._dav_base = f"{self._base_url}/remote.php/dav/files/{username}"
        self._ocs_base = f"{self._base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
        self._http = httpx.AsyncClient(auth=(username, token), timeout=30.0)

    async def close(self) -> None:
        await self._http.aclose()

    def _dav_url(self, path: str) -> str:
        return f"{self._dav_base}/{path.lstrip('/')}"

    async def mkdir(self, path: str) -> None:
        """Create a directory via MKCOL. Ignores 405 (already exists)."""
        resp = await self._http.request("MKCOL", self._dav_url(path))
        if resp.status_code == 405:
            return
        resp.raise_for_status()

    async def mkdir_p(self, path: str) -> None:
        """Create a directory and all parents."""
        parts = Path(path).parts
        for i in range(1, len(parts) + 1):
            await self.mkdir("/".join(parts[:i]))

    async def upload(self, local_path: str, remote_path: str) -> None:
        """Upload a local file via WebDAV PUT."""
        with open(local_path, "rb") as f:
            content = f.read()
        resp = await self._http.request("PUT", self._dav_url(remote_path), content=content)
        resp.raise_for_status()

    async def delete(self, remote_path: str) -> None:
        """Delete a file or directory via WebDAV DELETE."""
        resp = await self._http.request("DELETE", self._dav_url(remote_path))
        resp.raise_for_status()

    async def list_files(self, remote_path: str) -> list[dict]:
        """PROPFIND a directory and return list of {name, href, last_modified} dicts."""
        body = (
            '<?xml version="1.0"?>'
            '<d:propfind xmlns:d="DAV:">'
            "<d:prop><d:getlastmodified/></d:prop>"
            "</d:propfind>"
        )
        resp = await self._http.request(
            "PROPFIND",
            self._dav_url(remote_path),
            headers={"Depth": "infinity", "Content-Type": "application/xml"},
            content=body,
        )
        resp.raise_for_status()
        return self._parse_propfind(resp.text, remote_path)

    def _parse_propfind(self, xml_text: str, base_path: str) -> list[dict]:
        root = ElementTree.fromstring(xml_text)
        files = []
        for response in root.findall(f"{{{DAV_NS}}}response"):
            href = response.findtext(f"{{{DAV_NS}}}href", "")
            if href.rstrip("/").endswith(base_path.rstrip("/")):
                continue
            if href.endswith("/"):
                continue
            last_mod_el = response.find(f".//{{{DAV_NS}}}getlastmodified")
            last_modified = last_mod_el.text if last_mod_el is not None else None
            name = href.rstrip("/").rsplit("/", 1)[-1]
            files.append({"name": name, "href": href, "last_modified": last_modified})
        return files

    async def create_share(self, remote_path: str, expire_date: str | None = None) -> dict:
        """Create a public read-only share link."""
        data = {
            "path": f"/{remote_path.lstrip('/')}",
            "shareType": "3",
            "permissions": "1",
        }
        if expire_date:
            data["expireDate"] = expire_date
        resp = await self._http.post(
            f"{self._ocs_base}?format=json",
            headers={"OCS-APIRequest": "true"},
            data=data,
        )
        resp.raise_for_status()
        return resp.json()["ocs"]["data"]

    async def list_shares(self, remote_path: str) -> list[dict]:
        """List all shares for files within the given directory path."""
        resp = await self._http.get(
            self._ocs_base,
            headers={"OCS-APIRequest": "true"},
            params={
                "path": f"/{remote_path.lstrip('/')}",
                "subfiles": "true",
                "format": "json",
            },
        )
        resp.raise_for_status()
        return resp.json()["ocs"]["data"]

    async def delete_share(self, share_id: int) -> None:
        resp = await self._http.delete(
            f"{self._ocs_base}/{share_id}",
            headers={"OCS-APIRequest": "true"},
        )
        resp.raise_for_status()


# --- Cleanup ---


async def run_cleanup(client: NextcloudClient, ttl_days: int) -> int:
    """Delete ephemeral files older than ttl_days. Returns count deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    deleted = 0

    try:
        files = await client.list_files("claub/ephemeral")
    except Exception:
        return 0

    for f in files:
        if not f.get("last_modified"):
            continue
        try:
            file_time = parsedate_to_datetime(f["last_modified"])
        except (ValueError, TypeError):
            continue

        if file_time < cutoff:
            href = f["href"]
            idx = href.find("/files/")
            if idx == -1:
                continue
            after_files = href[idx + len("/files/"):]
            remote_path = after_files.split("/", 1)[1] if "/" in after_files else after_files

            try:
                parent_dir = remote_path.rsplit("/", 1)[0]
                shares = await client.list_shares(parent_dir)
                for share in shares:
                    if share.get("path", "").rstrip("/").endswith(f["name"]):
                        await client.delete_share(share["id"])
            except Exception:
                pass

            try:
                await client.delete(remote_path)
                deleted += 1
            except Exception:
                pass

    return deleted


# --- MCP server ---

client = NextcloudClient(NEXTCLOUD_URL, NEXTCLOUD_LOGIN, NEXTCLOUD_TOKEN)
mcp = FastMCP("nextcloud")


@mcp.tool()
async def share_file(
    file_path: str,
    persistent: bool = False,
    expire_days: int = 3,
) -> str:
    """Upload a file to Nextcloud and return a public share link.

    The link opens Nextcloud's built-in viewer (PDF viewer for PDFs, image preview, etc.).

    Args:
        file_path: Absolute path to the file in your workspace.
        persistent: If True, file is kept permanently. If False (default), auto-cleaned after expire_days.
        expire_days: Days until the share link expires (only for ephemeral files, default 3).
    """
    local = Path(file_path)
    if not local.exists():
        return f"Error: file not found: {file_path}"

    bucket = "persistent" if persistent else "ephemeral"
    remote_dir = f"claub/{bucket}/{AGENT_NAME}"
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


@mcp.tool()
async def list_shares(persistent: bool = False) -> str:
    """List shared files for this agent.

    Args:
        persistent: If True, list persistent files. If False (default), list ephemeral files.
    """
    bucket = "persistent" if persistent else "ephemeral"
    remote_dir = f"claub/{bucket}/{AGENT_NAME}"

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


@mcp.tool()
async def delete_shared_file(file_name: str) -> str:
    """Delete a shared file from Nextcloud (searches both ephemeral and persistent).

    Args:
        file_name: The filename to delete (e.g., resume_v3.pdf).
    """
    for bucket in ("ephemeral", "persistent"):
        remote_dir = f"claub/{bucket}/{AGENT_NAME}"
        try:
            files = await client.list_files(remote_dir)
        except Exception:
            continue

        for f in files:
            if f["name"] == file_name:
                remote_path = f"{remote_dir}/{file_name}"
                try:
                    shares = await client.list_shares(remote_dir)
                    for share in shares:
                        if share.get("path", "").rstrip("/").endswith(file_name):
                            await client.delete_share(share["id"])
                except Exception:
                    pass
                await client.delete(remote_path)
                return f"Deleted {file_name} from {bucket}."

    return f"Error: {file_name} not found in ephemeral or persistent storage."


@mcp.tool()
async def cleanup_ephemeral() -> str:
    """Delete expired ephemeral files. Runs automatically on startup, but can be called manually."""
    count = await run_cleanup(client, TTL_DAYS)
    return f"Cleaned up {count} expired file(s)." if count else "No expired files to clean up."


@mcp.lifespan()
async def startup():
    """Run cleanup on startup as a background task."""
    asyncio.create_task(run_cleanup(client, TTL_DAYS))
    yield


if __name__ == "__main__":
    mcp.run(transport="stdio")
