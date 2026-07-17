"""Tests for LeetCode cloud code tools."""

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server import (
    LANG_IDS,
    QUESTION_DETAIL_QUERY,
    SYNCED_CODE_QUERY,
    UPDATE_SYNCED_CODE_MUTATION,
    _auth_headers,
    _resolve_question_id,
    _validate_csrf,
    _validate_session,
    get_cloud_code,
    get_submissions,
    save_cloud_code,
    update_session,
)


# Real-world JWT shape: 3 dot-separated base64url segments.
VALID_SESSION = "eyJ" + "a" * 33 + "." + "b" * 720 + "." + "c" * 43
VALID_CSRF = "A" * 16 + "b" * 16  # 32 alphanumeric chars


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    """Point the MCP at a tmp token file pre-loaded with valid creds."""
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"session": "test-session", "csrf": "test-csrf"}))
    monkeypatch.setenv("LEETCODE_TOKEN_FILE", str(path))
    return path


def test_lang_ids_contains_python3():
    assert LANG_IDS["python3"] == 11


def test_lang_ids_contains_all_expected():
    # Values verified against LeetCode's own `languageList` GraphQL query.
    assert len(LANG_IDS) == 25
    assert LANG_IDS["cpp"] == 0
    assert LANG_IDS["mysql"] == 3
    assert LANG_IDS["c"] == 4
    assert LANG_IDS["javascript"] == 6
    assert LANG_IDS["golang"] == 10
    assert LANG_IDS["mssql"] == 14
    assert LANG_IDS["rust"] == 18
    assert LANG_IDS["dart"] == 24
    assert LANG_IDS["postgresql"] == 28


def test_auth_headers_returns_complete_headers(token_file):
    headers = _auth_headers("two-sum")

    assert headers["cookie"] == "LEETCODE_SESSION=test-session; csrftoken=test-csrf"
    assert headers["x-csrftoken"] == "test-csrf"
    assert headers["referer"] == "https://leetcode.com/problems/two-sum/description/"
    assert headers["origin"] == "https://leetcode.com"
    assert headers["content-type"] == "application/json"
    assert headers["sec-ch-ua-platform"] == '"macOS"'
    # random-uuid should be a valid uuid4
    uuid.UUID(headers["random-uuid"])


def test_auth_headers_raises_when_token_file_missing(tmp_path, monkeypatch):
    missing = tmp_path / "nope.json"
    monkeypatch.setenv("LEETCODE_TOKEN_FILE", str(missing))
    with pytest.raises(ValueError, match="not found"):
        _auth_headers("two-sum")


def test_auth_headers_raises_when_token_file_malformed(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    path.write_text("not-json")
    monkeypatch.setenv("LEETCODE_TOKEN_FILE", str(path))
    with pytest.raises(ValueError, match="not valid JSON"):
        _auth_headers("two-sum")


def test_auth_headers_raises_when_token_file_missing_fields(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"session": "x"}))
    monkeypatch.setenv("LEETCODE_TOKEN_FILE", str(path))
    with pytest.raises(ValueError, match="missing 'session' or 'csrf'"):
        _auth_headers("two-sum")


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


@pytest.mark.asyncio
async def test_get_cloud_code_returns_code(token_file):
    with patch("server.httpx.AsyncClient") as MockClient:
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
async def test_get_cloud_code_no_save_exists(token_file):
    with patch("server.httpx.AsyncClient") as MockClient:
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
async def test_get_cloud_code_invalid_language(token_file):
    result = await get_cloud_code("two-sum", "brainfuck")
    assert "Invalid language" in result


@pytest.mark.asyncio
async def test_save_cloud_code_success(token_file):
    with patch("server.httpx.AsyncClient") as MockClient:
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
async def test_save_cloud_code_invalid_language(token_file):
    result = await save_cloud_code("two-sum", "code", "brainfuck")
    assert "Invalid language" in result


@pytest.mark.asyncio
async def test_get_submissions_returns_list(token_file):
    with patch("server.httpx.AsyncClient") as MockClient:
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


# ---- update_session validation ----

def test_validate_session_accepts_realistic_jwt():
    assert _validate_session(VALID_SESSION) is None


def test_validate_session_rejects_wrong_segment_count():
    err = _validate_session("a" * 100 + "." + "b" * 100)
    assert err is not None
    assert "3-segment" in err


def test_validate_session_rejects_too_short():
    err = _validate_session("a.b.c")
    assert err is not None
    assert "length" in err


def test_validate_session_rejects_bad_chars():
    bad = "eyJ$$$" + "." + "b" * 100 + "." + "c" * 50
    err = _validate_session(bad)
    assert err is not None
    assert "base64url" in err


def test_validate_session_rejects_empty_segment():
    bad = "eyJabc.." + "c" * 50
    err = _validate_session(bad)
    assert err is not None


def test_validate_csrf_accepts_32_alnum():
    assert _validate_csrf(VALID_CSRF) is None


def test_validate_csrf_rejects_wrong_length():
    assert _validate_csrf("A" * 31) is not None
    assert _validate_csrf("A" * 33) is not None


def test_validate_csrf_rejects_non_alnum():
    assert _validate_csrf("A" * 31 + "!") is not None


# ---- update_session tool ----

def test_update_session_writes_file(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    monkeypatch.setenv("LEETCODE_TOKEN_FILE", str(path))

    result = update_session(VALID_SESSION, VALID_CSRF)
    assert "ok" in result
    data = json.loads(path.read_text())
    assert data == {"session": VALID_SESSION, "csrf": VALID_CSRF}
    # File mode should be 0600.
    assert (path.stat().st_mode & 0o777) == 0o600


def test_update_session_creates_parent_dir(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "dir" / "token.json"
    monkeypatch.setenv("LEETCODE_TOKEN_FILE", str(path))

    result = update_session(VALID_SESSION, VALID_CSRF)
    assert "ok" in result
    assert path.exists()


def test_update_session_strips_whitespace_and_quotes(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    monkeypatch.setenv("LEETCODE_TOKEN_FILE", str(path))

    result = update_session(f'  "{VALID_SESSION}"  ', f"\n{VALID_CSRF}\n")
    assert "ok" in result
    data = json.loads(path.read_text())
    assert data["session"] == VALID_SESSION
    assert data["csrf"] == VALID_CSRF


def test_update_session_backs_up_previous(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    monkeypatch.setenv("LEETCODE_TOKEN_FILE", str(path))
    path.write_text(json.dumps({"session": "old-session", "csrf": "old-csrf"}))

    update_session(VALID_SESSION, VALID_CSRF)

    backup = tmp_path / "token.json.prev"
    assert backup.exists()
    backup_data = json.loads(backup.read_text())
    assert backup_data == {"session": "old-session", "csrf": "old-csrf"}


def test_update_session_rejects_invalid_session(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    monkeypatch.setenv("LEETCODE_TOKEN_FILE", str(path))

    result = update_session("not-a-jwt", VALID_CSRF)
    assert result.startswith("rejected:")
    assert not path.exists()


def test_update_session_rejects_invalid_csrf(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    monkeypatch.setenv("LEETCODE_TOKEN_FILE", str(path))

    result = update_session(VALID_SESSION, "too-short")
    assert result.startswith("rejected:")
    assert not path.exists()


def test_update_session_does_not_clobber_on_invalid(tmp_path, monkeypatch):
    """If validation fails, the existing token file must be left intact."""
    path = tmp_path / "token.json"
    monkeypatch.setenv("LEETCODE_TOKEN_FILE", str(path))
    original = {"session": "still-valid", "csrf": "do-not-touch"}
    path.write_text(json.dumps(original))

    result = update_session("garbage", "garbage")
    assert result.startswith("rejected:")
    assert json.loads(path.read_text()) == original
