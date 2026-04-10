# Nextcloud File Sharing MCP Server — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an MCP server that uploads agent-produced files to Nextcloud and returns clickable share links with built-in PDF preview.

**Architecture:** A new HTTP-based FastMCP server (`nextcloud_mcp.py`) runs alongside the existing schedules MCP on a separate port (9401). A thin `nextcloud_client.py` wraps httpx calls to Nextcloud's WebDAV and OCS APIs. Agents call `mcp__nextcloud__share_file` to upload files and get share URLs. A background asyncio task cleans up ephemeral files older than a configurable TTL.

**Tech Stack:** FastMCP, httpx, uvicorn (all existing or trivially added), Nextcloud WebDAV + OCS Share API

**Spec:** `docs/superpowers/specs/2026-04-10-nextcloud-file-sharing-design.md`

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `bot/src/claude_assistant/nextcloud_client.py` | Thin httpx wrapper for WebDAV (MKCOL, PUT, DELETE, PROPFIND) and OCS Share API (create, list, delete) |
| Create | `bot/src/claude_assistant/nextcloud_mcp.py` | FastMCP server factory with three tools + cleanup background task |
| Create | `bot/tests/test_nextcloud_client.py` | Unit tests for the client (mocked httpx) |
| Create | `bot/tests/test_nextcloud_mcp.py` | Unit tests for MCP tool logic (mocked client) |
| Modify | `bot/src/claude_assistant/discord_bot.py` | Start second MCP server, accept new constructor param |
| Modify | `bot/src/claude_assistant/main.py` | Read env vars, pass Nextcloud config to bot |
| Modify | `bot/pyproject.toml` | Add `httpx` dependency |

---

### Task 1: Add httpx dependency

**Files:**
- Modify: `bot/pyproject.toml:5-12`

- [ ] **Step 1: Add httpx to dependencies**

In `bot/pyproject.toml`, add `"httpx>=0.27"` to the `dependencies` list:

```toml
dependencies = [
    "discord.py>=2.4",
    "apscheduler>=3.10,<4",
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
    "fastmcp>=2.0",
    "uvicorn>=0.30",
    "croniter>=2.0",
    "httpx>=0.27",
]
```

- [ ] **Step 2: Install updated dependencies**

Run: `cd /Users/you/Claude/bot && uv sync`
Expected: resolves and installs httpx

- [ ] **Step 3: Commit**

```bash
git add bot/pyproject.toml bot/uv.lock
git commit -m "chore: add httpx dependency for Nextcloud integration"
```

---

### Task 2: Nextcloud client — WebDAV operations

**Files:**
- Create: `bot/src/claude_assistant/nextcloud_client.py`
- Create: `bot/tests/test_nextcloud_client.py`

- [ ] **Step 1: Write failing tests for WebDAV operations**

Create `bot/tests/test_nextcloud_client.py`:

```python
"""Tests for the Nextcloud WebDAV/OCS client."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from claude_assistant.nextcloud_client import NextcloudClient


@pytest.fixture
def client() -> NextcloudClient:
    return NextcloudClient(
        base_url="https://cloud.example.com",
        username="claub",
        token="test-token",
    )


# --- mkdir ---


@pytest.mark.asyncio
async def test_mkdir_sends_mkcol(client: NextcloudClient) -> None:
    mock_response = AsyncMock()
    mock_response.status_code = 201
    mock_response.raise_for_status = MagicMock()

    with patch.object(client, "_http") as mock_http:
        mock_http.request = AsyncMock(return_value=mock_response)
        await client.mkdir("/claub/ephemeral/main")

    mock_http.request.assert_called_once_with(
        "MKCOL",
        "https://cloud.example.com/remote.php/dav/files/claub/claub/ephemeral/main",
    )


# --- upload ---


@pytest.mark.asyncio
async def test_upload_sends_put(client: NextcloudClient, tmp_path) -> None:
    test_file = tmp_path / "resume.pdf"
    test_file.write_bytes(b"fake pdf content")

    mock_response = AsyncMock()
    mock_response.status_code = 201
    mock_response.raise_for_status = MagicMock()

    with patch.object(client, "_http") as mock_http:
        mock_http.request = AsyncMock(return_value=mock_response)
        await client.upload(str(test_file), "/claub/ephemeral/main/resume.pdf")

    call_args = mock_http.request.call_args
    assert call_args[0] == ("PUT",)
    assert "resume.pdf" in call_args[0][1] if len(call_args[0]) > 1 else "resume.pdf" in str(call_args)


# --- delete ---


@pytest.mark.asyncio
async def test_delete_sends_delete(client: NextcloudClient) -> None:
    mock_response = AsyncMock()
    mock_response.status_code = 204
    mock_response.raise_for_status = MagicMock()

    with patch.object(client, "_http") as mock_http:
        mock_http.request = AsyncMock(return_value=mock_response)
        await client.delete("/claub/ephemeral/main/resume.pdf")

    mock_http.request.assert_called_once_with(
        "DELETE",
        "https://cloud.example.com/remote.php/dav/files/claub/claub/ephemeral/main/resume.pdf",
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_nextcloud_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claude_assistant.nextcloud_client'`

- [ ] **Step 3: Implement NextcloudClient with WebDAV operations**

Create `bot/src/claude_assistant/nextcloud_client.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_nextcloud_client.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/nextcloud_client.py bot/tests/test_nextcloud_client.py
git commit -m "feat: add Nextcloud WebDAV/OCS client"
```

---

### Task 3: Nextcloud client — OCS share and PROPFIND tests

**Files:**
- Modify: `bot/tests/test_nextcloud_client.py`

- [ ] **Step 1: Add tests for create_share, list_shares, delete_share, and list_files**

Append to `bot/tests/test_nextcloud_client.py`:

```python
# --- create_share ---


@pytest.mark.asyncio
async def test_create_share_returns_url(client: NextcloudClient) -> None:
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={
        "ocs": {
            "data": {
                "url": "https://cloud.example.com/s/abc123",
                "token": "abc123",
                "id": 42,
            }
        }
    })

    with patch.object(client, "_http") as mock_http:
        mock_http.post = AsyncMock(return_value=mock_response)
        result = await client.create_share("claub/ephemeral/main/resume.pdf", expire_date="2026-04-13")

    assert result["url"] == "https://cloud.example.com/s/abc123"
    call_args = mock_http.post.call_args
    assert call_args[1]["data"]["expireDate"] == "2026-04-13"
    assert call_args[1]["data"]["shareType"] == "3"
    assert call_args[1]["data"]["permissions"] == "1"


@pytest.mark.asyncio
async def test_create_share_without_expiry(client: NextcloudClient) -> None:
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={
        "ocs": {"data": {"url": "https://cloud.example.com/s/xyz789", "token": "xyz789", "id": 43}}
    })

    with patch.object(client, "_http") as mock_http:
        mock_http.post = AsyncMock(return_value=mock_response)
        result = await client.create_share("claub/persistent/main/report.pdf")

    assert "expireDate" not in mock_http.post.call_args[1]["data"]


# --- list_files (PROPFIND parsing) ---


def test_parse_propfind_extracts_files(client: NextcloudClient) -> None:
    xml = """<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:">
      <d:response>
        <d:href>/remote.php/dav/files/claub/claub/ephemeral/main/</d:href>
        <d:propstat><d:prop><d:getlastmodified>Fri, 10 Apr 2026 16:56:38 GMT</d:getlastmodified></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
      </d:response>
      <d:response>
        <d:href>/remote.php/dav/files/claub/claub/ephemeral/main/resume.pdf</d:href>
        <d:propstat><d:prop><d:getlastmodified>Fri, 07 Apr 2026 10:00:00 GMT</d:getlastmodified></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
      </d:response>
    </d:multistatus>"""

    files = client._parse_propfind(xml, "claub/ephemeral/main")
    assert len(files) == 1
    assert files[0]["name"] == "resume.pdf"
    assert files[0]["last_modified"] == "Fri, 07 Apr 2026 10:00:00 GMT"


# --- mkdir ignores 405 ---


@pytest.mark.asyncio
async def test_mkdir_ignores_already_exists(client: NextcloudClient) -> None:
    mock_response = AsyncMock()
    mock_response.status_code = 405

    with patch.object(client, "_http") as mock_http:
        mock_http.request = AsyncMock(return_value=mock_response)
        await client.mkdir("/claub/ephemeral")  # should not raise
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_nextcloud_client.py -v`
Expected: All 7 tests PASS

- [ ] **Step 3: Commit**

```bash
git add bot/tests/test_nextcloud_client.py
git commit -m "test: add OCS share and PROPFIND parsing tests for Nextcloud client"
```

---

### Task 4: MCP server — share_file tool

**Files:**
- Create: `bot/src/claude_assistant/nextcloud_mcp.py`
- Create: `bot/tests/test_nextcloud_mcp.py`

- [ ] **Step 1: Write failing tests for share_file logic**

Create `bot/tests/test_nextcloud_mcp.py`:

```python
"""Tests for the Nextcloud file sharing MCP server."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from claude_assistant.nextcloud_mcp import create_nextcloud_mcp, _share_file


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.mkdir_p = AsyncMock()
    client.upload = AsyncMock()
    client.create_share = AsyncMock(return_value={
        "url": "https://cloud.example.com/s/abc123",
        "token": "abc123",
        "id": 42,
    })
    return client


# --- create_nextcloud_mcp factory ---


def test_create_nextcloud_mcp_returns_fastmcp(mock_client: AsyncMock) -> None:
    import fastmcp
    server = create_nextcloud_mcp(mock_client)
    assert isinstance(server, fastmcp.FastMCP)


@pytest.mark.asyncio
async def test_create_nextcloud_mcp_has_tools(mock_client: AsyncMock) -> None:
    server = create_nextcloud_mcp(mock_client)
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    assert "share_file" in tool_names
    assert "list_shares" in tool_names
    assert "delete_shared_file" in tool_names


# --- _share_file ---


@pytest.mark.asyncio
async def test_share_file_ephemeral(mock_client: AsyncMock, tmp_path: Path) -> None:
    test_file = tmp_path / "resume.pdf"
    test_file.write_bytes(b"fake pdf")

    result = await _share_file(
        client=mock_client,
        agent="career",
        file_path=str(test_file),
        persistent=False,
        expire_days=3,
    )

    parsed = json.loads(result)
    assert parsed["url"] == "https://cloud.example.com/s/abc123"

    # Should create directory
    mock_client.mkdir_p.assert_called_once_with("claub/ephemeral/career")
    # Should upload
    mock_client.upload.assert_called_once_with(
        str(test_file), "claub/ephemeral/career/resume.pdf"
    )
    # Should create share with expiry
    call_args = mock_client.create_share.call_args
    assert call_args[0][0] == "claub/ephemeral/career/resume.pdf"
    assert call_args[1]["expire_date"] is not None


@pytest.mark.asyncio
async def test_share_file_persistent(mock_client: AsyncMock, tmp_path: Path) -> None:
    test_file = tmp_path / "final_resume.pdf"
    test_file.write_bytes(b"fake pdf")

    result = await _share_file(
        client=mock_client,
        agent="career",
        file_path=str(test_file),
        persistent=True,
        expire_days=3,
    )

    parsed = json.loads(result)
    assert parsed["url"] == "https://cloud.example.com/s/abc123"

    mock_client.mkdir_p.assert_called_once_with("claub/persistent/career")
    mock_client.upload.assert_called_once_with(
        str(test_file), "claub/persistent/career/final_resume.pdf"
    )
    # Persistent: no expiry
    call_args = mock_client.create_share.call_args
    assert call_args[1]["expire_date"] is None


@pytest.mark.asyncio
async def test_share_file_missing_file(mock_client: AsyncMock) -> None:
    result = await _share_file(
        client=mock_client,
        agent="career",
        file_path="/nonexistent/file.pdf",
        persistent=False,
        expire_days=3,
    )
    assert "Error" in result
    mock_client.upload.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_nextcloud_mcp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claude_assistant.nextcloud_mcp'`

- [ ] **Step 3: Implement the MCP server with share_file**

Create `bot/src/claude_assistant/nextcloud_mcp.py`:

```python
"""FastMCP server for Nextcloud file sharing.

Provides tools for agents to upload files to Nextcloud and get share links.
Runs as an HTTP server alongside the schedules MCP.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
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
        """List shared files for the requesting agent."""
        agent = _get_agent(request)
        if not agent:
            return "Error: missing X-Agent-Name header"
        return await _list_shares(client, agent, persistent)

    @mcp.tool()
    async def delete_shared_file(
        file_name: str,
        request: Request = fastmcp.server.dependencies.CurrentRequest(),  # type: ignore[assignment]
    ) -> str:
        """Delete a shared file from Nextcloud (searches both ephemeral and persistent)."""
        agent = _get_agent(request)
        if not agent:
            return "Error: missing X-Agent-Name header"
        return await _delete_shared_file(client, agent, file_name)

    return mcp
```

Note: `_list_shares` and `_delete_shared_file` are stub references — they'll be implemented in the next tasks. For now, add placeholder implementations at the bottom of the file so the module loads:

```python
async def _list_shares(client: NextcloudClient, agent: str, persistent: bool) -> str:
    """List shares for an agent. Implemented in Task 5."""
    raise NotImplementedError


async def _delete_shared_file(client: NextcloudClient, agent: str, file_name: str) -> str:
    """Delete a shared file. Implemented in Task 6."""
    raise NotImplementedError
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_nextcloud_mcp.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/nextcloud_mcp.py bot/tests/test_nextcloud_mcp.py
git commit -m "feat: add Nextcloud MCP server with share_file tool"
```

---

### Task 5: MCP server — list_shares tool

**Files:**
- Modify: `bot/src/claude_assistant/nextcloud_mcp.py`
- Modify: `bot/tests/test_nextcloud_mcp.py`

- [ ] **Step 1: Write failing test for list_shares**

Append to `bot/tests/test_nextcloud_mcp.py`:

```python
# --- _list_shares ---


@pytest.mark.asyncio
async def test_list_shares_ephemeral(mock_client: AsyncMock) -> None:
    mock_client.list_shares = AsyncMock(return_value=[
        {"path": "/claub/ephemeral/career/resume.pdf", "url": "https://cloud.example.com/s/abc", "expiration": "2026-04-13 00:00:00", "id": 1},
        {"path": "/claub/ephemeral/career/cover.pdf", "url": "https://cloud.example.com/s/def", "expiration": "2026-04-14 00:00:00", "id": 2},
    ])

    from claude_assistant.nextcloud_mcp import _list_shares
    result = await _list_shares(mock_client, "career", persistent=False)
    parsed = json.loads(result)
    assert len(parsed) == 2
    assert parsed[0]["name"] == "resume.pdf"
    assert parsed[0]["url"] == "https://cloud.example.com/s/abc"


@pytest.mark.asyncio
async def test_list_shares_empty(mock_client: AsyncMock) -> None:
    mock_client.list_shares = AsyncMock(return_value=[])

    from claude_assistant.nextcloud_mcp import _list_shares
    result = await _list_shares(mock_client, "career", persistent=False)
    parsed = json.loads(result)
    assert parsed == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_nextcloud_mcp.py::test_list_shares_ephemeral -v`
Expected: FAIL with `NotImplementedError`

- [ ] **Step 3: Implement _list_shares**

Replace the placeholder `_list_shares` in `bot/src/claude_assistant/nextcloud_mcp.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_nextcloud_mcp.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/nextcloud_mcp.py bot/tests/test_nextcloud_mcp.py
git commit -m "feat: add list_shares tool to Nextcloud MCP"
```

---

### Task 6: MCP server — delete_shared_file tool

**Files:**
- Modify: `bot/src/claude_assistant/nextcloud_mcp.py`
- Modify: `bot/tests/test_nextcloud_mcp.py`

- [ ] **Step 1: Write failing test for delete_shared_file**

Append to `bot/tests/test_nextcloud_mcp.py`:

```python
# --- _delete_shared_file ---


@pytest.mark.asyncio
async def test_delete_shared_file_found_in_ephemeral(mock_client: AsyncMock) -> None:
    # list_files finds the file in ephemeral
    mock_client.list_files = AsyncMock(side_effect=[
        [{"name": "resume.pdf", "href": "/dav/files/claub/claub/ephemeral/career/resume.pdf", "last_modified": "Fri, 10 Apr 2026"}],
    ])
    mock_client.list_shares = AsyncMock(return_value=[
        {"id": 42, "path": "/claub/ephemeral/career/resume.pdf"},
    ])
    mock_client.delete_share = AsyncMock()
    mock_client.delete = AsyncMock()

    from claude_assistant.nextcloud_mcp import _delete_shared_file
    result = await _delete_shared_file(mock_client, "career", "resume.pdf")
    assert "Deleted" in result

    mock_client.delete_share.assert_called_once_with(42)
    mock_client.delete.assert_called_once_with("claub/ephemeral/career/resume.pdf")


@pytest.mark.asyncio
async def test_delete_shared_file_not_found(mock_client: AsyncMock) -> None:
    mock_client.list_files = AsyncMock(return_value=[])

    from claude_assistant.nextcloud_mcp import _delete_shared_file
    result = await _delete_shared_file(mock_client, "career", "nonexistent.pdf")
    assert "not found" in result.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_nextcloud_mcp.py::test_delete_shared_file_found_in_ephemeral -v`
Expected: FAIL with `NotImplementedError`

- [ ] **Step 3: Implement _delete_shared_file**

Replace the placeholder `_delete_shared_file` in `bot/src/claude_assistant/nextcloud_mcp.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_nextcloud_mcp.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/nextcloud_mcp.py bot/tests/test_nextcloud_mcp.py
git commit -m "feat: add delete_shared_file tool to Nextcloud MCP"
```

---

### Task 7: Cleanup background task

**Files:**
- Modify: `bot/src/claude_assistant/nextcloud_mcp.py`
- Modify: `bot/tests/test_nextcloud_mcp.py`

- [ ] **Step 1: Write failing test for cleanup logic**

Append to `bot/tests/test_nextcloud_mcp.py`:

```python
# --- cleanup ---

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime


@pytest.mark.asyncio
async def test_cleanup_deletes_old_files(mock_client: AsyncMock) -> None:
    old_date = format_datetime(datetime.now(timezone.utc) - timedelta(days=5))
    recent_date = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))

    mock_client.list_files = AsyncMock(return_value=[
        {"name": "old.pdf", "href": "/dav/files/claub/claub/ephemeral/main/old.pdf", "last_modified": old_date},
        {"name": "recent.pdf", "href": "/dav/files/claub/claub/ephemeral/main/recent.pdf", "last_modified": recent_date},
    ])
    mock_client.list_shares = AsyncMock(return_value=[
        {"id": 10, "path": "/claub/ephemeral/main/old.pdf"},
    ])
    mock_client.delete_share = AsyncMock()
    mock_client.delete = AsyncMock()

    from claude_assistant.nextcloud_mcp import _run_cleanup
    deleted = await _run_cleanup(mock_client, ttl_days=3)

    assert deleted == 1
    mock_client.delete.assert_called_once_with("claub/ephemeral/main/old.pdf")
    mock_client.delete_share.assert_called_once_with(10)


@pytest.mark.asyncio
async def test_cleanup_skips_when_all_recent(mock_client: AsyncMock) -> None:
    recent_date = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))

    mock_client.list_files = AsyncMock(return_value=[
        {"name": "fresh.pdf", "href": "/dav/files/claub/claub/ephemeral/main/fresh.pdf", "last_modified": recent_date},
    ])

    from claude_assistant.nextcloud_mcp import _run_cleanup
    deleted = await _run_cleanup(mock_client, ttl_days=3)

    assert deleted == 0
    mock_client.delete.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_nextcloud_mcp.py::test_cleanup_deletes_old_files -v`
Expected: FAIL with `ImportError: cannot import name '_run_cleanup'`

- [ ] **Step 3: Implement _run_cleanup and the background task launcher**

Add to `bot/src/claude_assistant/nextcloud_mcp.py`:

```python
import asyncio
from email.utils import parsedate_to_datetime


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
            # href looks like /remote.php/dav/files/claub/claub/ephemeral/agent/file.pdf
            # We need: claub/ephemeral/agent/file.pdf
            dav_marker = "/files/"
            idx = href.find(dav_marker)
            if idx == -1:
                continue
            # Skip past /files/{username}/
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
```

Also add `from datetime import timezone` to the existing datetime import at the top of the file, and add `import asyncio` if not already present.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_nextcloud_mcp.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/nextcloud_mcp.py bot/tests/test_nextcloud_mcp.py
git commit -m "feat: add ephemeral file cleanup task for Nextcloud MCP"
```

---

### Task 8: Wire MCP server into the bot

**Files:**
- Modify: `bot/src/claude_assistant/main.py`
- Modify: `bot/src/claude_assistant/discord_bot.py`

- [ ] **Step 1: Add Nextcloud config to main.py**

In `bot/src/claude_assistant/main.py`, after the existing env var reads (around line 59), add:

```python
    # Nextcloud file sharing (optional)
    nc_url = os.environ.get("NEXTCLOUD_URL")
    nc_login = os.environ.get("NEXTCLOUD_LOGIN")
    nc_token = os.environ.get("NEXTCLOUD_TOKEN")
    nc_mcp_port = int(os.environ.get("NEXTCLOUD_MCP_PORT", "9401"))
    nc_ttl_days = int(os.environ.get("NEXTCLOUD_EPHEMERAL_TTL_DAYS", "3"))
    nc_config = None
    if nc_url and nc_login and nc_token:
        nc_config = {
            "url": nc_url,
            "login": nc_login,
            "token": nc_token,
            "mcp_port": nc_mcp_port,
            "ttl_days": nc_ttl_days,
        }
    else:
        log.info("Nextcloud config not set, file sharing MCP disabled")
```

Pass `nc_config` to the `AssistantBot` constructor:

```python
    bot = AssistantBot(
        config=config,
        workspaces_dir=workspaces_dir,
        session_store=sessions,
        schedule_store=schedules,
        firing_history=firing_history,
        mcp_config=mcp_config if mcp_config.exists() else None,
        agents_dir=agents_dir if agents_dir.exists() else None,
        mcp_port=mcp_port,
        all_skills=all_skills,
        nextcloud_config=nc_config,
    )
```

- [ ] **Step 2: Add Nextcloud MCP startup to discord_bot.py**

In `bot/src/claude_assistant/discord_bot.py`, update the `__init__` to accept `nextcloud_config`:

Add parameter to `__init__`:
```python
        nextcloud_config: dict | None = None,
```

Store it:
```python
        self._nextcloud_config = nextcloud_config
```

Add a new `_nextcloud_mcp_task` instance variable:
```python
        self._nextcloud_mcp_task: asyncio.Task | None = None
```

Add a startup method (next to `_start_mcp_server`):

```python
    async def _start_nextcloud_mcp(self) -> None:
        import uvicorn
        from claude_assistant.nextcloud_client import NextcloudClient
        from claude_assistant.nextcloud_mcp import create_nextcloud_mcp, run_cleanup_loop

        cfg = self._nextcloud_config
        client = NextcloudClient(cfg["url"], cfg["login"], cfg["token"])

        # Ensure base directories exist
        await client.mkdir_p("claub/ephemeral")
        await client.mkdir_p("claub/persistent")

        mcp = create_nextcloud_mcp(client)
        app = mcp.http_app()
        port = cfg["mcp_port"]
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)

        # Start cleanup loop as background task
        asyncio.create_task(run_cleanup_loop(client, cfg["ttl_days"]))

        log.info("Starting Nextcloud MCP server on 127.0.0.1:%d", port)
        await server.serve()
```

In the `on_ready` event handler (around line 105), add after the schedules MCP task:

```python
            if self._nextcloud_config:
                self._nextcloud_mcp_task = asyncio.create_task(self._start_nextcloud_mcp())
```

- [ ] **Step 3: Run existing tests to verify no regressions**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: All existing tests PASS (the new `nextcloud_config` param defaults to `None`)

- [ ] **Step 4: Commit**

```bash
git add bot/src/claude_assistant/main.py bot/src/claude_assistant/discord_bot.py
git commit -m "feat: wire Nextcloud MCP server into bot startup"
```

---

### Task 9: Update MCP config and permissions

**Files:**
- Modify: `/Users/you/docker/claub/config/mcp.json`
- Modify: `/Users/you/docker/claub/config/settings.json`

- [ ] **Step 1: Add Nextcloud MCP to shared mcp.json**

Add the `nextcloud` entry to `/Users/you/docker/claub/config/mcp.json`:

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
      "type": "http",
      "url": "http://localhost:9401/mcp",
      "headers": {
        "X-Agent-Name": "${CLAUB_AGENT_NAME}"
      }
    },
    "git": {
      "command": "uv",
      "args": ["--directory", "/claub/mcps/git", "run", "server.py"]
    }
  }
}
```

- [ ] **Step 2: Add Nextcloud tools to settings.json allow list**

Read the current `settings.json` and add `"mcp__nextcloud__*"` to the `permissions.allow` array.

- [ ] **Step 3: Add Nextcloud env vars to docker-compose.yml**

Read the current `docker-compose.yml` and add the three Nextcloud env vars to the `environment` section (referencing `.env`):

```yaml
      NEXTCLOUD_URL: ${NEXTCLOUD_URL}
      NEXTCLOUD_LOGIN: ${NEXTCLOUD_LOGIN}
      NEXTCLOUD_TOKEN: ${NEXTCLOUD_TOKEN}
```

- [ ] **Step 4: Commit**

```bash
git add /Users/you/docker/claub/config/mcp.json /Users/you/docker/claub/config/settings.json docker-compose.yml
git commit -m "feat: add Nextcloud MCP to shared config and permissions"
```

---

### Task 10: Integration test

**Files:**
- Modify: `bot/tests/test_nextcloud_mcp.py`

- [ ] **Step 1: Add integration test that hits real Nextcloud (gated by env var)**

Append to `bot/tests/test_nextcloud_mcp.py`:

```python
# --- Integration tests (require real Nextcloud) ---

import os

NEXTCLOUD_INTEGRATION = os.environ.get("NEXTCLOUD_INTEGRATION_TEST")


@pytest.mark.asyncio
@pytest.mark.skipif(not NEXTCLOUD_INTEGRATION, reason="Set NEXTCLOUD_INTEGRATION_TEST=1")
async def test_integration_upload_share_delete() -> None:
    """Full round-trip: upload, share, verify link, delete."""
    from claude_assistant.nextcloud_client import NextcloudClient
    import tempfile

    client = NextcloudClient(
        base_url=os.environ["NEXTCLOUD_URL"],
        username=os.environ["NEXTCLOUD_LOGIN"],
        token=os.environ["NEXTCLOUD_TOKEN"],
    )

    try:
        # Create a test file
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"Integration test content")
            local_path = f.name

        # Upload and share
        result = await _share_file(
            client=client,
            agent="test",
            file_path=local_path,
            persistent=False,
            expire_days=1,
        )
        parsed = json.loads(result)
        assert "url" in parsed
        assert parsed["url"].startswith("http")

        # List shares
        shares_result = await _list_shares(client, "test", persistent=False)
        shares = json.loads(shares_result)
        assert any(s["name"] == Path(local_path).name for s in shares)

        # Delete
        delete_result = await _delete_shared_file(client, "test", Path(local_path).name)
        assert "Deleted" in delete_result

    finally:
        await client.close()
        Path(local_path).unlink(missing_ok=True)
```

- [ ] **Step 2: Run integration test (if Nextcloud env vars are set)**

Run: `cd /Users/you/Claude/bot && NEXTCLOUD_INTEGRATION_TEST=1 uv run --extra dev pytest tests/test_nextcloud_mcp.py::test_integration_upload_share_delete -v`
Expected: PASS (uploads to real Nextcloud, creates share, verifies URL, cleans up)

- [ ] **Step 3: Commit**

```bash
git add bot/tests/test_nextcloud_mcp.py
git commit -m "test: add Nextcloud integration test"
```

---

### Task 11: Update CLAUDE.md documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add Nextcloud MCP section to CLAUDE.md**

In the MCP Servers section of `CLAUDE.md`, after the Playwright MCP subsection, add:

```markdown
#### Nextcloud File Sharing MCP

An embedded HTTP MCP server (like schedules) that lets agents upload files to Nextcloud and get share links. Runs on `127.0.0.1:9401` (configurable via `NEXTCLOUD_MCP_PORT`).

**Setup:** Create a dedicated Nextcloud user, generate an app password (Settings > Security), set `NEXTCLOUD_URL`, `NEXTCLOUD_LOGIN`, and `NEXTCLOUD_TOKEN` in `.env`. Optional: `NEXTCLOUD_EPHEMERAL_TTL_DAYS` (default 3).

**Tools:** `mcp__nextcloud__share_file`, `mcp__nextcloud__list_shares`, `mcp__nextcloud__delete_shared_file`.

**File organization:** Files are stored under `claub/ephemeral/{agent}/` (auto-cleaned after TTL) or `claub/persistent/{agent}/` (permanent). Share links open Nextcloud's built-in viewer (PDF viewer for PDFs).

**Cleanup:** Background task runs on startup + every 24h, deleting ephemeral files older than the TTL.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add Nextcloud file sharing MCP to CLAUDE.md"
```
