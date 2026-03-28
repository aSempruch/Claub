# LeetCode Cloud Code Tools — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `get_cloud_code` and `save_cloud_code` tools to the existing `leetcode-stats` MCP server so the leetcode agent can read and write LeetCode cloud-saved code.

**Architecture:** Two new MCP tools added to the existing `server.py`, sharing a new auth header builder and question ID resolver. Auth via `LEETCODE_SESSION` and `LEETCODE_CSRF_TOKEN` env vars. Existing `get_stats` tool unchanged.

**Tech Stack:** Python 3.12, FastMCP, httpx (all already in place)

**Spec:** `docs/superpowers/specs/2026-03-28-leetcode-cloud-code-design.md`

---

## File Map

- **Modify:** `/Users/you/docker/claub/mcps/leetcode-stats/server.py` — add constants, helpers, and two new tools
- **Create:** `/Users/you/docker/claub/mcps/leetcode-stats/tests/test_cloud_code.py` — unit tests for new functionality
- **Modify:** `/Users/you/docker/claub/config/agents/leetcode-coach.mcp.json` — add env vars for auth
- **Modify:** `/Users/you/docker/claub/mcps/leetcode-stats/pyproject.toml` — add pytest dev dependency

---

### Task 1: Add test infrastructure and language ID map

**Files:**
- Modify: `/Users/you/docker/claub/mcps/leetcode-stats/pyproject.toml`
- Modify: `/Users/you/docker/claub/mcps/leetcode-stats/server.py`
- Create: `/Users/you/docker/claub/mcps/leetcode-stats/tests/test_cloud_code.py`

- [ ] **Step 1: Add pytest to dev dependencies**

In `pyproject.toml`, add an optional dev dependency group:

```toml
[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio"]
```

- [ ] **Step 2: Add the language ID map to server.py**

Add after the existing `HEADERS` dict (around line 21), before the query constants:

```python
LANG_IDS = {
    "cpp": 0, "java": 1, "python": 2, "c": 3, "csharp": 4,
    "javascript": 5, "ruby": 6, "swift": 7, "golang": 8, "kotlin": 9,
    "scala": 10, "python3": 11, "typescript": 12, "rust": 13, "php": 14,
    "racket": 15, "erlang": 16, "elixir": 17, "dart": 18,
}
```

- [ ] **Step 3: Write test for language ID map**

Create `tests/test_cloud_code.py`:

```python
"""Tests for LeetCode cloud code tools."""

import pytest

from server import LANG_IDS


def test_lang_ids_contains_python3():
    assert LANG_IDS["python3"] == 11


def test_lang_ids_contains_all_expected():
    assert len(LANG_IDS) == 19
    assert LANG_IDS["cpp"] == 0
    assert LANG_IDS["dart"] == 18
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/docker/claub/mcps/leetcode-stats && uv run --extra dev pytest tests/test_cloud_code.py -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mcps/leetcode-stats/pyproject.toml mcps/leetcode-stats/server.py mcps/leetcode-stats/tests/test_cloud_code.py
git commit -m "feat(leetcode-mcp): add test infra and language ID map"
```

---

### Task 2: Add auth header builder

**Files:**
- Modify: `/Users/you/docker/claub/mcps/leetcode-stats/server.py`
- Modify: `/Users/you/docker/claub/mcps/leetcode-stats/tests/test_cloud_code.py`

- [ ] **Step 1: Write failing tests for _auth_headers**

Append to `tests/test_cloud_code.py`:

```python
import os
import uuid
from unittest.mock import patch

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/docker/claub/mcps/leetcode-stats && uv run --extra dev pytest tests/test_cloud_code.py::test_auth_headers_returns_complete_headers tests/test_cloud_code.py::test_auth_headers_raises_when_missing_env -v`
Expected: FAIL — `_auth_headers` not defined

- [ ] **Step 3: Implement _auth_headers in server.py**

Add these imports at the top of `server.py`:

```python
import os
import uuid
```

Add this function after the `LANG_IDS` dict, before the query constants:

```python
def _auth_headers(slug: str) -> dict[str, str]:
    """Build browser-realistic headers for authenticated LeetCode API requests."""
    session = os.environ.get("LEETCODE_SESSION")
    csrf = os.environ.get("LEETCODE_CSRF_TOKEN")
    if not session or not csrf:
        raise ValueError("LEETCODE_SESSION and LEETCODE_CSRF_TOKEN env vars required")
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://leetcode.com",
        "referer": f"https://leetcode.com/problems/{slug}/description/",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Brave";v="146"',
        "sec-ch-ua-arch": '"arm"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-platform-version": '"15.7.3"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "sec-gpc": "1",
        "random-uuid": str(uuid.uuid4()),
        "cookie": f"LEETCODE_SESSION={session}; csrftoken={csrf}",
        "x-csrftoken": csrf,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/docker/claub/mcps/leetcode-stats && uv run --extra dev pytest tests/test_cloud_code.py -v`
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mcps/leetcode-stats/server.py mcps/leetcode-stats/tests/test_cloud_code.py
git commit -m "feat(leetcode-mcp): add authenticated header builder"
```

---

### Task 3: Add GraphQL queries and _resolve_question_id helper

**Files:**
- Modify: `/Users/you/docker/claub/mcps/leetcode-stats/server.py`
- Modify: `/Users/you/docker/claub/mcps/leetcode-stats/tests/test_cloud_code.py`

- [ ] **Step 1: Write failing test for _resolve_question_id**

Append to `tests/test_cloud_code.py`:

```python
from unittest.mock import AsyncMock

from server import _resolve_question_id, QUESTION_DETAIL_QUERY, SYNCED_CODE_QUERY, UPDATE_SYNCED_CODE_MUTATION


@pytest.mark.asyncio
async def test_resolve_question_id_returns_int():
    mock_client = AsyncMock()
    mock_client.post.return_value.json.return_value = {
        "data": {"question": {"questionId": "42"}}
    }
    mock_client.post.return_value.status_code = 200
    mock_client.post.return_value.raise_for_status = lambda: None

    result = await _resolve_question_id(mock_client, "two-sum", {"content-type": "application/json"})
    assert result == 42
    assert isinstance(result, int)


@pytest.mark.asyncio
async def test_resolve_question_id_raises_on_not_found():
    mock_client = AsyncMock()
    mock_client.post.return_value.json.return_value = {
        "data": {"question": None}
    }
    mock_client.post.return_value.status_code = 200
    mock_client.post.return_value.raise_for_status = lambda: None

    with pytest.raises(ValueError, match="Problem 'nonexistent' not found"):
        await _resolve_question_id(mock_client, "nonexistent", {"content-type": "application/json"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/docker/claub/mcps/leetcode-stats && uv run --extra dev pytest tests/test_cloud_code.py::test_resolve_question_id_returns_int tests/test_cloud_code.py::test_resolve_question_id_raises_on_not_found -v`
Expected: FAIL — imports not found

- [ ] **Step 3: Add GraphQL constants and _resolve_question_id to server.py**

Add these three query constants after `_auth_headers`, before the existing `PROFILE_QUERY`:

```python
QUESTION_DETAIL_QUERY = """query getQuestionDetail($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    questionId
  }
}"""

SYNCED_CODE_QUERY = """query syncedCode($questionId: Int!, $lang: Int!) {
  syncedCode(questionId: $questionId, lang: $lang) {
    timestamp
    code
  }
}"""

UPDATE_SYNCED_CODE_MUTATION = """mutation updateSyncedCode($code: String!, $lang: Int!, $questionId: Int!) {
  updateSyncedCode(code: $code, lang: $lang, questionId: $questionId) {
    ok
  }
}"""
```

Add `_resolve_question_id` after the new constants:

```python
async def _resolve_question_id(
    client: httpx.AsyncClient, slug: str, headers: dict[str, str]
) -> int:
    """Resolve a problem's title slug to its numeric questionId."""
    resp = await client.post(
        API_URL,
        json={"query": QUESTION_DETAIL_QUERY, "variables": {"titleSlug": slug}},
        headers=headers,
        timeout=15,
    )
    if resp.status_code in (401, 403):
        raise ValueError("LeetCode session expired — re-extract cookies from browser")
    resp.raise_for_status()
    question = resp.json().get("data", {}).get("question")
    if question is None:
        raise ValueError(f"Problem '{slug}' not found")
    return int(question["questionId"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/docker/claub/mcps/leetcode-stats && uv run --extra dev pytest tests/test_cloud_code.py -v`
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mcps/leetcode-stats/server.py mcps/leetcode-stats/tests/test_cloud_code.py
git commit -m "feat(leetcode-mcp): add GraphQL queries and question ID resolver"
```

---

### Task 4: Add get_cloud_code tool

**Files:**
- Modify: `/Users/you/docker/claub/mcps/leetcode-stats/server.py`
- Modify: `/Users/you/docker/claub/mcps/leetcode-stats/tests/test_cloud_code.py`

- [ ] **Step 1: Write failing tests for get_cloud_code**

Append to `tests/test_cloud_code.py`:

```python
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
        question_resp = AsyncMock()
        question_resp.json.return_value = {"data": {"question": {"questionId": "1"}}}
        question_resp.raise_for_status = lambda: None

        # Second call: synced code
        code_resp = AsyncMock()
        code_resp.json.return_value = {
            "data": {"syncedCode": {"timestamp": 123, "code": "class Solution: pass"}}
        }
        code_resp.raise_for_status = lambda: None

        mock_client.post.side_effect = [question_resp, code_resp]

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

        question_resp = AsyncMock()
        question_resp.json.return_value = {"data": {"question": {"questionId": "1"}}}
        question_resp.raise_for_status = lambda: None

        code_resp = AsyncMock()
        code_resp.json.return_value = {"data": {"syncedCode": None}}
        code_resp.raise_for_status = lambda: None

        mock_client.post.side_effect = [question_resp, code_resp]

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/docker/claub/mcps/leetcode-stats && uv run --extra dev pytest tests/test_cloud_code.py::test_get_cloud_code_returns_code -v`
Expected: FAIL — `get_cloud_code` not defined

- [ ] **Step 3: Implement get_cloud_code in server.py**

Add after the existing `get_stats` tool, before the `if __name__` block:

```python
@mcp.tool()
async def get_cloud_code(problem: str, language: str = "python3") -> str:
    """Fetch cloud-saved code for a LeetCode problem.

    Args:
        problem: Problem title slug (e.g. "two-sum")
        language: Language slug (e.g. "python3", "javascript"). Defaults to "python3".

    Returns the saved code string, or a message if no cloud save exists.
    """
    if language not in LANG_IDS:
        valid = ", ".join(sorted(LANG_IDS))
        return f"Invalid language '{language}'. Valid options: {valid}"

    try:
        headers = _auth_headers(problem)
    except ValueError as e:
        return str(e)

    try:
        async with httpx.AsyncClient() as client:
            question_id = await _resolve_question_id(client, problem, headers)
            resp = await client.post(
                API_URL,
                json={
                    "query": SYNCED_CODE_QUERY,
                    "variables": {"questionId": question_id, "lang": LANG_IDS[language]},
                },
                headers=headers,
                timeout=15,
            )
            if resp.status_code in (401, 403):
                return "LeetCode session expired — re-extract cookies from browser"
            resp.raise_for_status()

            synced = resp.json().get("data", {}).get("syncedCode")
            if synced is None:
                return f"No cloud-saved code for '{problem}' in {language}"
            return synced["code"]
    except ValueError as e:
        return str(e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/docker/claub/mcps/leetcode-stats && uv run --extra dev pytest tests/test_cloud_code.py -v`
Expected: 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mcps/leetcode-stats/server.py mcps/leetcode-stats/tests/test_cloud_code.py
git commit -m "feat(leetcode-mcp): add get_cloud_code tool"
```

---

### Task 5: Add save_cloud_code tool

**Files:**
- Modify: `/Users/you/docker/claub/mcps/leetcode-stats/server.py`
- Modify: `/Users/you/docker/claub/mcps/leetcode-stats/tests/test_cloud_code.py`

- [ ] **Step 1: Write failing tests for save_cloud_code**

Append to `tests/test_cloud_code.py`:

```python
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

        question_resp = AsyncMock()
        question_resp.json.return_value = {"data": {"question": {"questionId": "1"}}}
        question_resp.raise_for_status = lambda: None

        save_resp = AsyncMock()
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/docker/claub/mcps/leetcode-stats && uv run --extra dev pytest tests/test_cloud_code.py::test_save_cloud_code_success -v`
Expected: FAIL — `save_cloud_code` not defined

- [ ] **Step 3: Implement save_cloud_code in server.py**

Add after `get_cloud_code`, before the `if __name__` block:

```python
@mcp.tool()
async def save_cloud_code(problem: str, code: str, language: str = "python3") -> str:
    """Save code to LeetCode cloud for a problem.

    Args:
        problem: Problem title slug (e.g. "two-sum")
        code: The code to save
        language: Language slug (e.g. "python3", "javascript"). Defaults to "python3".

    Returns a confirmation message.
    """
    if language not in LANG_IDS:
        valid = ", ".join(sorted(LANG_IDS))
        return f"Invalid language '{language}'. Valid options: {valid}"

    try:
        headers = _auth_headers(problem)
    except ValueError as e:
        return str(e)

    try:
        async with httpx.AsyncClient() as client:
            question_id = await _resolve_question_id(client, problem, headers)
            resp = await client.post(
                API_URL,
                json={
                    "query": UPDATE_SYNCED_CODE_MUTATION,
                    "variables": {
                        "code": code,
                        "lang": LANG_IDS[language],
                        "questionId": question_id,
                    },
                },
                headers=headers,
                timeout=15,
            )
            if resp.status_code in (401, 403):
                return "LeetCode session expired — re-extract cookies from browser"
            resp.raise_for_status()

            ok = resp.json().get("data", {}).get("updateSyncedCode", {}).get("ok")
            if ok:
                return f"Saved code to LeetCode cloud for '{problem}' in {language}"
            return f"Failed to save code for '{problem}' — LeetCode returned ok=false"
    except ValueError as e:
        return str(e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/docker/claub/mcps/leetcode-stats && uv run --extra dev pytest tests/test_cloud_code.py -v`
Expected: 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mcps/leetcode-stats/server.py mcps/leetcode-stats/tests/test_cloud_code.py
git commit -m "feat(leetcode-mcp): add save_cloud_code tool"
```

---

### Task 6: Wire auth env vars into agent MCP config

**Files:**
- Modify: `/Users/you/docker/claub/config/agents/leetcode-coach.mcp.json`

- [ ] **Step 1: Update the MCP config with env vars**

Replace the contents of `leetcode-coach.mcp.json` with:

```json
{
  "mcpServers": {
    "leetcode-stats": {
      "command": "uv",
      "args": ["--directory", "/claub/mcps/leetcode-stats", "run", "server.py"],
      "env": {
        "LEETCODE_SESSION": "<session-jwt>",
        "LEETCODE_CSRF_TOKEN": "<csrf-token>"
      }
    }
  }
}
```

Note: The user must replace `<session-jwt>` and `<csrf-token>` with real values extracted from their browser.

- [ ] **Step 2: Commit**

```bash
git add config/agents/leetcode-coach.mcp.json
git commit -m "feat(leetcode-mcp): wire auth env vars into agent MCP config"
```

---

### Task 7: Manual smoke test

- [ ] **Step 1: Set real env vars and run the MCP server**

```bash
cd /Users/you/docker/claub/mcps/leetcode-stats
LEETCODE_SESSION="<real-session>" LEETCODE_CSRF_TOKEN="<real-csrf>" uv run mcp run server.py
```

- [ ] **Step 2: Test get_cloud_code via MCP inspector (or restart the container and message the agent)**

Verify:
1. `get_cloud_code("two-sum")` returns saved Python 3 code (or "No cloud-saved code" message)
2. `save_cloud_code("two-sum", "# test")` returns "Saved code to LeetCode cloud..."
3. `get_cloud_code("two-sum")` now returns `"# test"`
4. Restore original code with another `save_cloud_code` call

- [ ] **Step 3: Verify get_stats still works unchanged**

Call `get_stats()` and confirm it returns profile JSON as before.
