"""Tests for the Nextcloud file sharing MCP server."""
from __future__ import annotations

import json
import os
import pytest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from claude_assistant.nextcloud_mcp import (
    create_nextcloud_mcp,
    _share_file,
    _list_shares,
    _delete_shared_file,
    _run_cleanup,
)


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
    assert parsed["bucket"] == "ephemeral"
    assert parsed["expires"] is not None

    mock_client.mkdir_p.assert_called_once_with("claub/ephemeral/career")
    mock_client.upload.assert_called_once_with(
        str(test_file), "claub/ephemeral/career/resume.pdf"
    )
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
    assert parsed["bucket"] == "persistent"
    assert parsed["expires"] is None

    mock_client.mkdir_p.assert_called_once_with("claub/persistent/career")
    mock_client.upload.assert_called_once_with(
        str(test_file), "claub/persistent/career/final_resume.pdf"
    )
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


# --- _list_shares ---


@pytest.mark.asyncio
async def test_list_shares_ephemeral(mock_client: AsyncMock) -> None:
    mock_client.list_shares = AsyncMock(return_value=[
        {"path": "/claub/ephemeral/career/resume.pdf", "url": "https://cloud.example.com/s/abc", "expiration": "2026-04-13 00:00:00", "id": 1},
        {"path": "/claub/ephemeral/career/cover.pdf", "url": "https://cloud.example.com/s/def", "expiration": "2026-04-14 00:00:00", "id": 2},
    ])

    result = await _list_shares(mock_client, "career", persistent=False)
    parsed = json.loads(result)
    assert len(parsed) == 2
    assert parsed[0]["name"] == "resume.pdf"
    assert parsed[0]["url"] == "https://cloud.example.com/s/abc"


@pytest.mark.asyncio
async def test_list_shares_empty(mock_client: AsyncMock) -> None:
    mock_client.list_shares = AsyncMock(return_value=[])

    result = await _list_shares(mock_client, "career", persistent=False)
    parsed = json.loads(result)
    assert parsed == []


# --- _delete_shared_file ---


@pytest.mark.asyncio
async def test_delete_shared_file_found_in_ephemeral(mock_client: AsyncMock) -> None:
    mock_client.list_files = AsyncMock(side_effect=[
        [{"name": "resume.pdf", "href": "/dav/files/claub/claub/ephemeral/career/resume.pdf", "last_modified": "Fri, 10 Apr 2026"}],
    ])
    mock_client.list_shares = AsyncMock(return_value=[
        {"id": 42, "path": "/claub/ephemeral/career/resume.pdf"},
    ])
    mock_client.delete_share = AsyncMock()
    mock_client.delete = AsyncMock()

    result = await _delete_shared_file(mock_client, "career", "resume.pdf")
    assert "Deleted" in result

    mock_client.delete_share.assert_called_once_with(42)
    mock_client.delete.assert_called_once_with("claub/ephemeral/career/resume.pdf")


@pytest.mark.asyncio
async def test_delete_shared_file_not_found(mock_client: AsyncMock) -> None:
    mock_client.list_files = AsyncMock(return_value=[])

    result = await _delete_shared_file(mock_client, "career", "nonexistent.pdf")
    assert "not found" in result.lower()


# --- cleanup ---


@pytest.mark.asyncio
async def test_cleanup_deletes_old_files(mock_client: AsyncMock) -> None:
    old_date = format_datetime(datetime.now(timezone.utc) - timedelta(days=5))
    recent_date = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))

    mock_client.list_files = AsyncMock(return_value=[
        {"name": "old.pdf", "href": "/remote.php/dav/files/claub/claub/ephemeral/main/old.pdf", "last_modified": old_date},
        {"name": "recent.pdf", "href": "/remote.php/dav/files/claub/claub/ephemeral/main/recent.pdf", "last_modified": recent_date},
    ])
    mock_client.list_shares = AsyncMock(return_value=[
        {"id": 10, "path": "/claub/ephemeral/main/old.pdf"},
    ])
    mock_client.delete_share = AsyncMock()
    mock_client.delete = AsyncMock()

    deleted = await _run_cleanup(mock_client, ttl_days=3)

    assert deleted == 1
    mock_client.delete.assert_called_once_with("claub/ephemeral/main/old.pdf")
    mock_client.delete_share.assert_called_once_with(10)


@pytest.mark.asyncio
async def test_cleanup_skips_when_all_recent(mock_client: AsyncMock) -> None:
    recent_date = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))

    mock_client.list_files = AsyncMock(return_value=[
        {"name": "fresh.pdf", "href": "/remote.php/dav/files/claub/claub/ephemeral/main/fresh.pdf", "last_modified": recent_date},
    ])

    deleted = await _run_cleanup(mock_client, ttl_days=3)

    assert deleted == 0
    mock_client.delete.assert_not_called()


# --- Integration tests (require real Nextcloud) ---

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
