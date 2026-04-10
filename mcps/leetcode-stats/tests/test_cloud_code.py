"""Tests for LeetCode cloud code tools."""

import json
import os
import uuid
from unittest.mock import patch

import pytest

from server import LANG_IDS


def test_lang_ids_contains_python3():
    assert LANG_IDS["python3"] == 11


def test_lang_ids_contains_all_expected():
    assert len(LANG_IDS) == 19
    assert LANG_IDS["cpp"] == 0
    assert LANG_IDS["dart"] == 18


from server import _auth_headers


def test_auth_headers_returns_complete_headers():
    with patch.dict(os.environ, {
        "LEETCODE_SESSION": "test-session",
        "LEETCODE_CSRF_TOKEN": "test-csrf",
    }):
        headers = _auth_headers("two-sum")

    assert headers["cookie"] == "LEETCODE_SESSION=test-session; csrftoken=test-csrf"
    assert headers["x-csrftoken"] == "test-csrf"
    assert headers["referer"] == "https://leetcode.com/problems/two-sum/description/"
    assert headers["origin"] == "https://leetcode.com"
    assert headers["content-type"] == "application/json"
    assert headers["sec-ch-ua-platform"] == '"macOS"'
    # random-uuid should be a valid uuid4
    uuid.UUID(headers["random-uuid"])


def test_auth_headers_raises_when_missing_env():
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("LEETCODE_SESSION", None)
        os.environ.pop("LEETCODE_CSRF_TOKEN", None)
        with pytest.raises(ValueError, match="LEETCODE_SESSION and LEETCODE_CSRF_TOKEN env vars required"):
            _auth_headers("two-sum")


from unittest.mock import AsyncMock, MagicMock

from server import _resolve_question_id, QUESTION_DETAIL_QUERY, SYNCED_CODE_QUERY, UPDATE_SYNCED_CODE_MUTATION


@pytest.mark.asyncio
async def test_resolve_question_id_returns_int():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": {"question": {"questionId": "42"}}
    }
    mock_resp.status_code = 200
    mock_resp.raise_for_status = lambda: None

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp

    result = await _resolve_question_id(mock_client, "two-sum", {"content-type": "application/json"})
    assert result == 42
    assert isinstance(result, int)


@pytest.mark.asyncio
async def test_resolve_question_id_raises_on_not_found():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": {"question": None}
    }
    mock_resp.status_code = 200
    mock_resp.raise_for_status = lambda: None

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp

    with pytest.raises(ValueError, match="Problem 'nonexistent' not found"):
        await _resolve_question_id(mock_client, "nonexistent", {"content-type": "application/json"})


from server import get_cloud_code


@pytest.mark.asyncio
async def test_get_cloud_code_returns_code():
    with patch.dict(os.environ, {
        "LEETCODE_SESSION": "s",
        "LEETCODE_CSRF_TOKEN": "c",
    }), patch("server.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        # First call: resolve question ID
        question_resp = MagicMock()
        question_resp.json.return_value = {"data": {"question": {"questionId": "1"}}}
        question_resp.status_code = 200
        question_resp.raise_for_status = lambda: None

        # Second call: synced code
        code_resp = MagicMock()
        code_resp.json.return_value = {
            "data": {"syncedCode": {"timestamp": 123, "code": "class Solution: pass"}}
        }
        code_resp.status_code = 200
        code_resp.raise_for_status = lambda: None

        mock_client.post = AsyncMock(side_effect=[question_resp, code_resp])

        result = await get_cloud_code("two-sum")
        assert result == "class Solution: pass"


@pytest.mark.asyncio
async def test_get_cloud_code_no_save_exists():
    with patch.dict(os.environ, {
        "LEETCODE_SESSION": "s",
        "LEETCODE_CSRF_TOKEN": "c",
    }), patch("server.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        question_resp = MagicMock()
        question_resp.json.return_value = {"data": {"question": {"questionId": "1"}}}
        question_resp.status_code = 200
        question_resp.raise_for_status = lambda: None

        code_resp = MagicMock()
        code_resp.json.return_value = {"data": {"syncedCode": None}}
        code_resp.status_code = 200
        code_resp.raise_for_status = lambda: None

        mock_client.post = AsyncMock(side_effect=[question_resp, code_resp])

        result = await get_cloud_code("two-sum", "python3")
        assert "No cloud-saved code" in result


@pytest.mark.asyncio
async def test_get_cloud_code_invalid_language():
    with patch.dict(os.environ, {
        "LEETCODE_SESSION": "s",
        "LEETCODE_CSRF_TOKEN": "c",
    }):
        result = await get_cloud_code("two-sum", "brainfuck")
        assert "Invalid language" in result


from server import save_cloud_code


@pytest.mark.asyncio
async def test_save_cloud_code_success():
    with patch.dict(os.environ, {
        "LEETCODE_SESSION": "s",
        "LEETCODE_CSRF_TOKEN": "c",
    }), patch("server.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        question_resp = MagicMock()
        question_resp.json.return_value = {"data": {"question": {"questionId": "1"}}}
        question_resp.raise_for_status = lambda: None
        question_resp.status_code = 200

        save_resp = MagicMock()
        save_resp.json.return_value = {"data": {"updateSyncedCode": {"ok": True}}}
        save_resp.raise_for_status = lambda: None
        save_resp.status_code = 200

        mock_client.post.side_effect = [question_resp, save_resp]

        result = await save_cloud_code("two-sum", "class Solution: pass")
        assert "Saved" in result


@pytest.mark.asyncio
async def test_save_cloud_code_invalid_language():
    with patch.dict(os.environ, {
        "LEETCODE_SESSION": "s",
        "LEETCODE_CSRF_TOKEN": "c",
    }):
        result = await save_cloud_code("two-sum", "code", "brainfuck")
        assert "Invalid language" in result


from server import get_submissions


@pytest.mark.asyncio
async def test_get_submissions_returns_list():
    with patch.dict(os.environ, {
        "LEETCODE_SESSION": "s",
        "LEETCODE_CSRF_TOKEN": "c",
    }), patch("server.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = MagicMock()
        resp.json.return_value = {
            "data": {
                "questionSubmissionList": {
                    "hasNext": False,
                    "submissions": [
                        {"id": "123", "statusDisplay": "Accepted", "lang": "python3", "runtime": "7 ms"},
                    ],
                }
            }
        }
        resp.status_code = 200
        resp.raise_for_status = lambda: None

        mock_client.post = AsyncMock(return_value=resp)

        result = await get_submissions("two-sum")
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["statusDisplay"] == "Accepted"
