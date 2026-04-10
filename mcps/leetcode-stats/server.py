"""MCP server exposing LeetCode stats via GraphQL."""

import json
import os
import uuid

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("leetcode-stats")

API_URL = "https://leetcode.com/graphql"
DEFAULT_USERNAME = os.environ.get("LEETCODE_USERNAME", "")

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
    "cpp": 0, "java": 1, "python": 2, "c": 3, "csharp": 4,
    "javascript": 5, "ruby": 6, "swift": 7, "golang": 8, "kotlin": 9,
    "scala": 10, "python3": 11, "typescript": 12, "rust": 13, "php": 14,
    "racket": 15, "erlang": 16, "elixir": 17, "dart": 18,
}

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


if __name__ == "__main__":
    mcp.run(transport="stdio")
