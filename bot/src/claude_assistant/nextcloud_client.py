"""Thin httpx wrapper for Nextcloud WebDAV and OCS Share APIs."""
from __future__ import annotations

import logging
from pathlib import Path
from xml.etree import ElementTree

import httpx

log = logging.getLogger(__name__)

DAV_NS = "DAV:"


class NextcloudClient:
    """Client for Nextcloud WebDAV file operations and OCS share management."""

    def __init__(self, base_url: str, username: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._dav_base = f"{self._base_url}/remote.php/dav/files/{username}"
        self._ocs_base = f"{self._base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
        self._http = httpx.AsyncClient(
            auth=(username, token),
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._http.aclose()

    def _dav_url(self, path: str) -> str:
        """Build full WebDAV URL from a Nextcloud-relative path."""
        return f"{self._dav_base}/{path.lstrip('/')}"

    async def mkdir(self, path: str) -> None:
        """Create a directory via MKCOL. Ignores 405 (already exists)."""
        resp = await self._http.request("MKCOL", self._dav_url(path))
        if resp.status_code == 405:
            return  # already exists
        resp.raise_for_status()

    async def mkdir_p(self, path: str) -> None:
        """Create a directory and all parents (like mkdir -p)."""
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
        """PROPFIND a directory and return list of {name, last_modified} dicts."""
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
        """Parse PROPFIND XML response into a list of file entries."""
        root = ElementTree.fromstring(xml_text)
        files = []
        for response in root.findall(f"{{{DAV_NS}}}response"):
            href = response.findtext(f"{{{DAV_NS}}}href", "")
            # Skip the directory itself
            if href.rstrip("/").endswith(base_path.rstrip("/")):
                continue
            # Skip subdirectories (end with /)
            if href.endswith("/"):
                continue
            last_mod_el = response.find(f".//{{{DAV_NS}}}getlastmodified")
            last_modified = last_mod_el.text if last_mod_el is not None else None
            # Extract filename from href
            name = href.rstrip("/").rsplit("/", 1)[-1]
            files.append({"name": name, "href": href, "last_modified": last_modified})
        return files

    # --- OCS Share API ---

    async def create_share(self, remote_path: str, expire_date: str | None = None) -> dict:
        """Create a public read-only share link. Returns OCS response data dict."""
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
        """List all shares for a given path."""
        resp = await self._http.get(
            f"{self._ocs_base}?format=json",
            headers={"OCS-APIRequest": "true"},
            params={"path": f"/{remote_path.lstrip('/')}", "reshares": "true"},
        )
        resp.raise_for_status()
        return resp.json()["ocs"]["data"]

    async def delete_share(self, share_id: int) -> None:
        """Delete a share by its ID."""
        resp = await self._http.delete(
            f"{self._ocs_base}/{share_id}",
            headers={"OCS-APIRequest": "true"},
        )
        resp.raise_for_status()
