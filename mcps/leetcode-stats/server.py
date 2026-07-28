"""MCP server exposing LeetCode stats via GraphQL."""

import json
import os
import re

import httpx
from mcp.server.fastmcp import FastMCP

import monitor_control
from leetcode_api import (
    API_URL,
    LANG_IDS,
    QUESTION_DETAIL_QUERY,
    SUBMISSION_LIST_QUERY,
    SYNCED_CODE_QUERY,
    UPDATE_SYNCED_CODE_MUTATION,
)
from leetcode_api import auth_headers as _auth_headers
from leetcode_api import fetch_question_id as _resolve_question_id
from leetcode_api import read_token as _read_token
from leetcode_api import token_path as _token_path

mcp = FastMCP("leetcode-stats")

DEFAULT_USERNAME = os.environ.get("LEETCODE_USERNAME", "")

# JWT segment: base64url chars, optional padding. LeetCode's session is a 3-segment JWT.
_JWT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+=*$")
# Django csrftoken: 32 alphanumeric chars.
_CSRF_RE = re.compile(r"^[A-Za-z0-9]{32}$")


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


@mcp.tool()
async def start_monitoring(problem: str, language: str = "python3") -> str:
    """Start recording how the user works through a problem, not just the result.

    Polls their LeetCode cloud-saved editor buffer in the background and writes
    a timestamped timeline: every distinct save with its diff, the long pauses,
    and every submission with its verdict. Call this when they say they're
    starting a problem.

    Only one problem can be monitored at a time. Monitoring survives your own
    process restarting — it runs detached.

    Args:
        problem: Problem title slug (e.g. "two-sum")
        language: Language slug they're solving in. Defaults to "python3".

    Stops on stop_monitoring(), 15 minutes without a code change, or 4 hours.
    Read the recording with get_monitoring_results().
    """
    return await monitor_control.start(problem, language)


@mcp.tool()
def stop_monitoring() -> str:
    """Stop the active monitoring session and return its timeline.

    Takes no arguments — only one session can be active. Safe to call when
    nothing is running.
    """
    return monitor_control.stop()


@mcp.tool()
def get_monitoring_results(problem: str | None = None) -> str:
    """Read back a recorded solve as a compact timeline.

    Works on a session that is still running, so you can review progress
    without stopping the recording first.

    Args:
        problem: Title slug to look up. Omit for the most recent session.

    Returns the timeline plus the directory path, where snapshots/ holds the
    full code at each save if you want to diff two points yourself.
    """
    return monitor_control.results(problem=problem)


if __name__ == "__main__":
    mcp.run(transport="stdio")
