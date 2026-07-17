"""MCP server exposing LeetCode stats via GraphQL."""

import json
import os
import re
import uuid
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("leetcode-stats")

API_URL = "https://leetcode.com/graphql"
DEFAULT_USERNAME = os.environ.get("LEETCODE_USERNAME", "")
DEFAULT_TOKEN_FILE = "/claub/mcps/leetcode-stats/token.json"

# JWT segment: base64url chars, optional padding. LeetCode's session is a 3-segment JWT.
_JWT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+=*$")
# Django csrftoken: 32 alphanumeric chars.
_CSRF_RE = re.compile(r"^[A-Za-z0-9]{32}$")


def _token_path() -> Path:
    return Path(os.environ.get("LEETCODE_TOKEN_FILE", DEFAULT_TOKEN_FILE))


def _read_token() -> tuple[str, str]:
    """Read session/csrf from the token file. Raises ValueError on any problem."""
    path = _token_path()
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


def _validate_session(session: str) -> str | None:
    """Return None if valid, else an error message."""
    if not (100 <= len(session) <= 4000):
        return f"session length {len(session)} outside expected range 100..4000"
    parts = session.split(".")
    if len(parts) != 3:
        return f"session must be a 3-segment JWT (got {len(parts)} segments)"
    for i, part in enumerate(parts):
        if not part:
            return f"session JWT segment {i + 1} is empty"
        if not _JWT_SEGMENT_RE.match(part):
            return f"session JWT segment {i + 1} contains non-base64url chars"
    return None


def _validate_csrf(csrf: str) -> str | None:
    if not _CSRF_RE.match(csrf):
        return f"csrf must be exactly 32 alphanumeric chars (got {len(csrf)})"
    return None

HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://leetcode.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

LANG_IDS = {
    "cpp": 0, "java": 1, "python": 2, "mysql": 3, "c": 4,
    "csharp": 5, "javascript": 6, "ruby": 7, "bash": 8, "swift": 9,
    "golang": 10, "python3": 11, "scala": 12, "kotlin": 13, "mssql": 14,
    "oraclesql": 15, "rust": 18, "php": 19, "typescript": 20, "racket": 21,
    "erlang": 22, "elixir": 23, "dart": 24, "pythondata": 25, "postgresql": 28,
}

def _auth_headers(slug: str) -> dict[str, str]:
    """Build browser-realistic headers for authenticated LeetCode API requests."""
    session, csrf = _read_token()
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


PROFILE_QUERY = """query getUserProfile($username: String!) {
  allQuestionsCount { difficulty count }
  matchedUser(username: $username) {
    contributions { points }
    profile { reputation ranking }
    submissionCalendar
    submitStats {
      acSubmissionNum { difficulty count submissions }
      totalSubmissionNum { difficulty count submissions }
    }
  }
}"""

RECENT_QUERY = """query recentAcSubmissions($username: String!, $limit: Int!) {
  recentAcSubmissionList(username: $username, limit: $limit) {
    id title titleSlug timestamp
  }
}"""


async def _query(client: httpx.AsyncClient, gql: str, variables: dict) -> dict:
    resp = await client.post(
        API_URL,
        json={"query": gql, "variables": variables},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
async def get_stats(username: str = DEFAULT_USERNAME, recent_limit: int = 15) -> str:
    """Fetch LeetCode profile stats and recent accepted submissions.

    Returns JSON with: allQuestionsCount, matchedUser (profile, submissionCalendar,
    submitStats with acSubmissionNum/totalSubmissionNum), and recentAcSubmissionList.
    """
    async with httpx.AsyncClient() as client:
        profile = await _query(client, PROFILE_QUERY, {"username": username})
        recent = await _query(
            client, RECENT_QUERY, {"username": username, "limit": recent_limit}
        )

    result = {
        **profile.get("data", {}),
        "recentAcSubmissionList": recent.get("data", {}).get(
            "recentAcSubmissionList", []
        ),
    }
    return json.dumps(result, indent=2)


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


@mcp.tool()
async def get_submissions(problem: str) -> str:
    """Fetch all submission history for a LeetCode problem.

    Args:
        problem: Problem title slug (e.g. "two-sum")

    Returns JSON array of submissions with status, language, runtime, memory, and timestamp.
    """
    try:
        headers = _auth_headers(problem)
    except ValueError as e:
        return str(e)

    try:
        all_submissions = []
        offset = 0
        async with httpx.AsyncClient() as client:
            while True:
                resp = await client.post(
                    API_URL,
                    json={
                        "query": SUBMISSION_LIST_QUERY,
                        "variables": {"questionSlug": problem, "offset": offset, "limit": 50},
                    },
                    headers=headers,
                    timeout=15,
                )
                if resp.status_code in (401, 403):
                    return "LeetCode session expired — re-extract cookies from browser"
                resp.raise_for_status()

                data = resp.json().get("data", {}).get("questionSubmissionList", {})
                submissions = data.get("submissions", [])
                all_submissions.extend(submissions)
                if not data.get("hasNext", False):
                    break
                offset += len(submissions)

        if not all_submissions:
            return f"No submissions found for '{problem}'"
        return json.dumps(all_submissions, indent=2)
    except ValueError as e:
        return str(e)


@mcp.tool()
def update_session(session: str, csrf: str) -> str:
    """Replace the stored LeetCode session and csrf cookies.

    Use this when the user provides fresh cookies (typically after a session
    expires). Validates the shape before writing — won't overwrite the file
    with malformed input. The previous token is backed up as token.json.prev.

    Args:
        session: LEETCODE_SESSION cookie value — a JWT (3 dot-separated base64url segments).
        csrf: csrftoken cookie value — exactly 32 alphanumeric chars.

    Returns a status message. Does NOT echo the token values back.
    """
    session = session.strip().strip('"').strip("'")
    csrf = csrf.strip().strip('"').strip("'")

    err = _validate_session(session)
    if err:
        return f"rejected: {err}"
    err = _validate_csrf(csrf)
    if err:
        return f"rejected: {err}"

    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        backup = path.with_suffix(path.suffix + ".prev")
        backup.write_bytes(path.read_bytes())
        try:
            os.chmod(backup, 0o600)
        except OSError:
            pass

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"session": session, "csrf": csrf}))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)

    return (
        f"ok — wrote {path} (session len={len(session)}, csrf len={len(csrf)}). "
        "Previous token saved as .prev."
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
