"""Unit tests for NextcloudClient — mocked httpx."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from claude_assistant.nextcloud_client import NextcloudClient

BASE_URL = "https://cloud.example.com"
USERNAME = "alice"
TOKEN = "app-password-123"


@pytest.fixture
def client() -> NextcloudClient:
    return NextcloudClient(BASE_URL, USERNAME, TOKEN)


def _mock_response(status_code: int = 200, json_data: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json = MagicMock(return_value=json_data)
    return resp


# --- WebDAV tests ---


@pytest.mark.asyncio
async def test_mkdir_sends_mkcol(client: NextcloudClient) -> None:
    resp = _mock_response(201)
    with patch.object(client, "_http") as mock_http:
        mock_http.request = AsyncMock(return_value=resp)
        await client.mkdir("reports/2024")

    mock_http.request.assert_called_once_with(
        "MKCOL",
        f"{BASE_URL}/remote.php/dav/files/{USERNAME}/reports/2024",
    )
    resp.raise_for_status.assert_called_once()


@pytest.mark.asyncio
async def test_upload_sends_put(client: NextcloudClient, tmp_path) -> None:
    local_file = tmp_path / "report.pdf"
    local_file.write_bytes(b"PDF content here")

    resp = _mock_response(201)
    with patch.object(client, "_http") as mock_http:
        mock_http.request = AsyncMock(return_value=resp)
        await client.upload(str(local_file), "reports/report.pdf")

    mock_http.request.assert_called_once_with(
        "PUT",
        f"{BASE_URL}/remote.php/dav/files/{USERNAME}/reports/report.pdf",
        content=b"PDF content here",
    )
    resp.raise_for_status.assert_called_once()


@pytest.mark.asyncio
async def test_delete_sends_delete(client: NextcloudClient) -> None:
    resp = _mock_response(204)
    with patch.object(client, "_http") as mock_http:
        mock_http.request = AsyncMock(return_value=resp)
        await client.delete("reports/old.pdf")

    mock_http.request.assert_called_once_with(
        "DELETE",
        f"{BASE_URL}/remote.php/dav/files/{USERNAME}/reports/old.pdf",
    )
    resp.raise_for_status.assert_called_once()


@pytest.mark.asyncio
async def test_mkdir_ignores_already_exists(client: NextcloudClient) -> None:
    resp = _mock_response(405)
    with patch.object(client, "_http") as mock_http:
        mock_http.request = AsyncMock(return_value=resp)
        # Should not raise
        await client.mkdir("existing-dir")

    mock_http.request.assert_called_once()
    resp.raise_for_status.assert_not_called()


# --- PROPFIND / list_files tests ---

_PROPFIND_XML = """\
<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/dav/files/alice/reports/</d:href>
    <d:propstat>
      <d:prop><d:getlastmodified>Mon, 01 Jan 2024 00:00:00 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/alice/reports/file1.pdf</d:href>
    <d:propstat>
      <d:prop><d:getlastmodified>Tue, 02 Jan 2024 10:00:00 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/alice/reports/subdir/</d:href>
    <d:propstat>
      <d:prop><d:getlastmodified>Wed, 03 Jan 2024 12:00:00 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/alice/reports/file2.txt</d:href>
    <d:propstat>
      <d:prop><d:getlastmodified>Thu, 04 Jan 2024 08:30:00 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""


@pytest.mark.asyncio
async def test_parse_propfind_extracts_files(client: NextcloudClient) -> None:
    resp = _mock_response(207, text=_PROPFIND_XML)
    with patch.object(client, "_http") as mock_http:
        mock_http.request = AsyncMock(return_value=resp)
        files = await client.list_files("reports")

    # Should include file1.pdf and file2.txt — skip the directory itself and subdir/
    assert len(files) == 2
    names = {f["name"] for f in files}
    assert names == {"file1.pdf", "file2.txt"}

    file1 = next(f for f in files if f["name"] == "file1.pdf")
    assert file1["last_modified"] == "Tue, 02 Jan 2024 10:00:00 GMT"
    assert "href" in file1


# --- OCS Share API tests ---


@pytest.mark.asyncio
async def test_create_share_returns_url(client: NextcloudClient) -> None:
    share_data = {"id": 42, "url": "https://cloud.example.com/s/abc123", "token": "abc123"}
    resp = _mock_response(200, json_data={"ocs": {"meta": {"status": "ok"}, "data": share_data}})
    with patch.object(client, "_http") as mock_http:
        mock_http.post = AsyncMock(return_value=resp)
        result = await client.create_share("reports/2024", expire_date="2024-12-31")

    assert result == share_data

    call_kwargs = mock_http.post.call_args
    assert call_kwargs.kwargs["data"]["shareType"] == "3"
    assert call_kwargs.kwargs["data"]["permissions"] == "1"
    assert call_kwargs.kwargs["data"]["expireDate"] == "2024-12-31"
    assert call_kwargs.kwargs["headers"]["OCS-APIRequest"] == "true"


@pytest.mark.asyncio
async def test_create_share_without_expiry(client: NextcloudClient) -> None:
    share_data = {"id": 7, "url": "https://cloud.example.com/s/xyz", "token": "xyz"}
    resp = _mock_response(200, json_data={"ocs": {"meta": {"status": "ok"}, "data": share_data}})
    with patch.object(client, "_http") as mock_http:
        mock_http.post = AsyncMock(return_value=resp)
        result = await client.create_share("docs/report.pdf")

    assert result == share_data

    call_kwargs = mock_http.post.call_args
    assert "expireDate" not in call_kwargs.kwargs["data"]
