"""Shared LeetCode GraphQL client.

Imported by both ``server.py`` (FastMCP tools) and ``monitor.py`` (the detached
poller), so this module must not import FastMCP — the monitor runs as a bare
Python process with no MCP machinery.

The token is re-read on every request rather than cached at construction. A
long solve can outlive its LeetCode session, and re-reading means a mid-session
``update_session`` is picked up by the very next poll instead of requiring the
monitor to be restarted.
"""

import json
import os
import uuid
from pathlib import Path

import httpx

DEFAULT_API_URL = "https://leetcode.com/graphql"
API_URL = DEFAULT_API_URL


def api_url() -> str:
    """Resolved per request so integration tests can point at a local stub."""
    return os.environ.get("LEETCODE_API_URL", DEFAULT_API_URL)


DEFAULT_TOKEN_FILE = "/claub/mcps/leetcode-stats/token.json"

LANG_IDS = {
    "cpp": 0, "java": 1, "python": 2, "mysql": 3, "c": 4,
    "csharp": 5, "javascript": 6, "ruby": 7, "bash": 8, "swift": 9,
    "golang": 10, "python3": 11, "scala": 12, "kotlin": 13, "mssql": 14,
    "oraclesql": 15, "rust": 18, "php": 19, "typescript": 20, "racket": 21,
    "erlang": 22, "elixir": 23, "dart": 24, "pythondata": 25, "postgresql": 28,
}


class AuthError(ValueError):
    """LeetCode rejected the session cookie (401/403).

    Subclasses ValueError so the server's existing ``except ValueError`` paths
    surface it as a message to the agent unchanged, while the monitor can still
    catch it specifically to drive its auth-recovery window.
    """


def token_path(explicit=None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get("LEETCODE_TOKEN_FILE", DEFAULT_TOKEN_FILE))


def read_token(explicit=None) -> tuple[str, str]:
    """Read session/csrf from the token file. Raises ValueError on any problem."""
    path = token_path(explicit)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ValueError(
            f"LeetCode token file not found at {path} — ask the user for fresh "
            "session/csrf cookies and call update_session"
        )
    except json.JSONDecodeError as e:
        raise ValueError(f"LeetCode token file at {path} is not valid JSON: {e}")
    session = data.get("session")
    csrf = data.get("csrf")
    if not isinstance(session, str) or not isinstance(csrf, str) or not session or not csrf:
        raise ValueError(
            f"LeetCode token file at {path} is missing 'session' or 'csrf' — "
            "call update_session with fresh cookies"
        )
    return session, csrf


def auth_headers(slug: str, explicit_token=None) -> dict[str, str]:
    """Build browser-realistic headers for authenticated LeetCode requests."""
    session, csrf = read_token(explicit_token)
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

SUBMISSION_LIST_QUERY = """query submissionList($offset: Int!, $limit: Int!, $questionSlug: String!, $lang: Int, $status: Int) {
  questionSubmissionList(
    offset: $offset
    limit: $limit
    questionSlug: $questionSlug
    lang: $lang
    status: $status
  ) {
    lastKey
    hasNext
    submissions {
      id
      statusDisplay
      lang
      langName
      runtime
      timestamp
      memory
    }
  }
}"""

UPDATE_SYNCED_CODE_MUTATION = """mutation updateSyncedCode($code: String!, $lang: Int!, $questionId: Int!) {
  updateSyncedCode(code: $code, lang: $lang, questionId: $questionId) {
    ok
  }
}"""


def extract_question_id(payload: dict, slug: str) -> int:
    question = payload.get("data", {}).get("question")
    if question is None:
        raise ValueError(f"Problem '{slug}' not found")
    return int(question["questionId"])


async def fetch_question_id(client, slug: str, headers: dict[str, str]) -> int:
    """Resolve a title slug to its numeric questionId using a caller's client."""
    resp = await client.post(
        api_url(),
        json={"query": QUESTION_DETAIL_QUERY, "variables": {"titleSlug": slug}},
        headers=headers,
        timeout=15,
    )
    if resp.status_code in (401, 403):
        raise AuthError("LeetCode session expired — re-extract cookies from browser")
    resp.raise_for_status()
    return extract_question_id(resp.json(), slug)


def latest_submission_ts(submissions: list[dict]) -> int | None:
    """Newest submission timestamp, or None if there are no submissions."""
    stamps = [int(s["timestamp"]) for s in submissions if s.get("timestamp") is not None]
    return max(stamps) if stamps else None


class LeetCodeClient:
    """Authenticated LeetCode GraphQL client with one connection pool."""

    def __init__(self, token_path=None, transport=None, timeout: float = 15.0):
        self._token_path = token_path
        self._transport = transport
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "LeetCodeClient":
        if self._transport is not None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            )
        else:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, query: str, variables: dict, slug: str) -> dict:
        assert self._client is not None, "use LeetCodeClient as an async context manager"
        resp = await self._client.post(
            api_url(),
            json={"query": query, "variables": variables},
            headers=auth_headers(slug, self._token_path),
        )
        if resp.status_code in (401, 403):
            raise AuthError("LeetCode session expired — re-extract cookies from browser")
        resp.raise_for_status()
        return resp.json()

    async def resolve_question_id(self, slug: str) -> int:
        data = await self._post(QUESTION_DETAIL_QUERY, {"titleSlug": slug}, slug)
        return extract_question_id(data, slug)

    async def synced_code(self, question_id: int, lang_id: int, slug: str = "") -> dict | None:
        data = await self._post(
            SYNCED_CODE_QUERY,
            {"questionId": question_id, "lang": lang_id},
            slug,
        )
        return data.get("data", {}).get("syncedCode")

    async def submissions(self, problem: str) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            data = await self._post(
                SUBMISSION_LIST_QUERY,
                {"questionSlug": problem, "offset": offset, "limit": 50},
                problem,
            )
            page = data.get("data", {}).get("questionSubmissionList", {})
            batch = page.get("submissions", [])
            out.extend(batch)
            if not page.get("hasNext", False):
                return out
            offset += len(batch)
