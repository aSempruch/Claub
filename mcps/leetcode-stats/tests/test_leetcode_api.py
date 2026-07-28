"""Tests for the shared LeetCode client used by both server and monitor."""

import json

import httpx
import pytest

from leetcode_api import AuthError, LeetCodeClient, latest_submission_ts


@pytest.fixture
def token_path(tmp_path):
    p = tmp_path / "token.json"
    p.write_text(json.dumps({"session": "sess-1", "csrf": "csrf-1"}))
    return p


def _client(token_path, handler):
    return LeetCodeClient(token_path=token_path, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_synced_code_returns_timestamp_and_code(token_path):
    def handler(request):
        return httpx.Response(200, json={
            "data": {"syncedCode": {"timestamp": 1785198365, "code": "class Solution: pass"}}
        })

    async with _client(token_path, handler) as c:
        result = await c.synced_code(question_id=1, lang_id=11)

    assert result == {"timestamp": 1785198365, "code": "class Solution: pass"}


@pytest.mark.asyncio
async def test_synced_code_returns_none_when_no_cloud_save(token_path):
    """Starting a problem before writing any code is the normal case."""
    def handler(request):
        return httpx.Response(200, json={"data": {"syncedCode": None}})

    async with _client(token_path, handler) as c:
        assert await c.synced_code(question_id=1, lang_id=11) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_synced_code_raises_auth_error(token_path, status):
    def handler(request):
        return httpx.Response(status, json={})

    async with _client(token_path, handler) as c:
        with pytest.raises(AuthError):
            await c.synced_code(question_id=1, lang_id=11)


@pytest.mark.asyncio
async def test_token_is_reread_each_call_so_refreshes_take_effect(token_path):
    """A 90min session can outlive its token; update_session mid-run must work."""
    seen = []

    def handler(request):
        seen.append(request.headers["cookie"])
        return httpx.Response(200, json={"data": {"syncedCode": None}})

    async with _client(token_path, handler) as c:
        await c.synced_code(question_id=1, lang_id=11)
        token_path.write_text(json.dumps({"session": "sess-2", "csrf": "csrf-2"}))
        await c.synced_code(question_id=1, lang_id=11)

    assert "LEETCODE_SESSION=sess-1" in seen[0]
    assert "LEETCODE_SESSION=sess-2" in seen[1]


@pytest.mark.asyncio
async def test_submissions_paginates_until_exhausted(token_path):
    pages = [
        {"hasNext": True, "submissions": [{"id": "1", "timestamp": "100"}]},
        {"hasNext": False, "submissions": [{"id": "2", "timestamp": "200"}]},
    ]
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["variables"]["offset"])
        return httpx.Response(200, json={
            "data": {"questionSubmissionList": pages[len(calls) - 1]}
        })

    async with _client(token_path, handler) as c:
        result = await c.submissions("two-sum")

    assert [s["id"] for s in result] == ["1", "2"]
    assert calls == [0, 1]


@pytest.mark.asyncio
async def test_submissions_raises_auth_error(token_path):
    def handler(request):
        return httpx.Response(401, json={})

    async with _client(token_path, handler) as c:
        with pytest.raises(AuthError):
            await c.submissions("two-sum")


@pytest.mark.asyncio
async def test_network_failure_propagates(token_path):
    def handler(request):
        raise httpx.ConnectError("boom")

    async with _client(token_path, handler) as c:
        with pytest.raises(httpx.ConnectError):
            await c.synced_code(question_id=1, lang_id=11)


# ---- latest_submission_ts ----

def test_latest_submission_ts_picks_the_maximum():
    subs = [{"timestamp": "100"}, {"timestamp": "300"}, {"timestamp": "200"}]
    assert latest_submission_ts(subs) == 300


def test_latest_submission_ts_is_none_for_no_submissions():
    assert latest_submission_ts([]) is None


# ---- API endpoint override (lets integration tests run without the real site) ----

def test_api_url_defaults_to_leetcode(monkeypatch):
    monkeypatch.delenv("LEETCODE_API_URL", raising=False)
    from leetcode_api import api_url
    assert api_url() == "https://leetcode.com/graphql"


def test_api_url_honours_the_env_override(monkeypatch):
    monkeypatch.setenv("LEETCODE_API_URL", "http://127.0.0.1:8123/graphql")
    from leetcode_api import api_url
    assert api_url() == "http://127.0.0.1:8123/graphql"


@pytest.mark.asyncio
async def test_requests_go_to_the_overridden_url(token_path, monkeypatch):
    monkeypatch.setenv("LEETCODE_API_URL", "http://127.0.0.1:8123/graphql")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": {"syncedCode": None}})

    async with _client(token_path, handler) as c:
        await c.synced_code(question_id=1, lang_id=11)

    assert seen == ["http://127.0.0.1:8123/graphql"]
