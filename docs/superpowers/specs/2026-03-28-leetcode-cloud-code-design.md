# LeetCode Cloud Code Tools — Design Spec

**Date:** 2026-03-28
**Scope:** Add `get_cloud_code` and `save_cloud_code` tools to the existing `leetcode-stats` MCP server.

## Context

The `leetcode-stats` MCP server (`/claub/mcps/leetcode-stats/server.py`) currently has one tool (`get_stats`) that fetches public profile stats without authentication. LeetCode also has a "cloud save" feature that stores one code snapshot per (problem, language) pair. The agent needs tools to read and write these cloud saves.

## Tools

### `get_cloud_code(problem: str, language: str = "python3") -> str`

Fetches cloud-saved code for a given problem and language.

- `problem` — title slug (e.g. `"two-sum"`)
- `language` — language slug (e.g. `"python3"`), defaults to `"python3"`

**Internal flow:**
1. Call `_resolve_question_id(problem)` to get the numeric `questionId`
2. Map `language` slug to numeric language ID via static dict
3. Call `syncedCode` GraphQL query
4. Return the code string

**Return values:**
- Code string if cloud save exists
- `"No cloud-saved code for '{problem}' in {language}"` if `syncedCode` is `null`

### `save_cloud_code(problem: str, code: str, language: str = "python3") -> str`

Writes code to LeetCode cloud save for a given problem and language.

- `problem` — title slug
- `code` — the code to save
- `language` — language slug, defaults to `"python3"`

**Internal flow:**
1. Call `_resolve_question_id(problem)` to get the numeric `questionId`
2. Map `language` slug to numeric language ID
3. Call `updateSyncedCode` GraphQL mutation
4. Return confirmation message

## Authentication

### Env Vars

| Env Var | Description |
|---------|-------------|
| `LEETCODE_SESSION` | Session JWT from browser cookies |
| `LEETCODE_CSRF_TOKEN` | CSRF token from browser cookies |

Configured in the agent's MCP config (e.g. `leetcode.mcp.json`). The server reads them via `os.environ` at request time (not at startup), so updated values take effect without restart.

### Header Builder

A helper function `_auth_headers(slug: str)` returns headers mimicking a real Brave/Chrome browser session. The `slug` parameter is used to set the `Referer` dynamically.

**Headers included:**

```python
{
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": "https://leetcode.com",
    "referer": f"https://leetcode.com/problems/{slug}/description/",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
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
    "random-uuid": "<generated uuid4 per request>",
    "cookie": f"LEETCODE_SESSION={session}; csrftoken={csrf}",
    "x-csrftoken": csrf,
}
```

**Not included** (handled by httpx or unnecessary): `:authority`, `:method`, `:path`, `:scheme`, `accept-encoding`, `content-length`, `priority`, `authorization`, `uuuserid`, `x-operation-name`.

Raises a clear error string if either env var is missing.

## Shared Helpers

### `_resolve_question_id(slug: str) -> int`

Calls the `getQuestionDetail` GraphQL query to resolve a title slug to a numeric `questionId`. Uses auth headers. Note: the GraphQL response returns `questionId` as a string (e.g. `"1"`), so this helper parses it to `int` before returning.

### Language ID Map

Static dict mapping language slug to numeric ID:

```python
LANG_IDS = {
    "cpp": 0, "java": 1, "python": 2, "c": 3, "csharp": 4,
    "javascript": 5, "ruby": 6, "swift": 7, "golang": 8, "kotlin": 9,
    "scala": 10, "python3": 11, "typescript": 12, "rust": 13, "php": 14,
    "racket": 15, "erlang": 16, "elixir": 17, "dart": 18,
}
```

Invalid language slugs return an error listing valid options.

## GraphQL Queries

Three new query/mutation constants at module level:

```graphql
# QUESTION_DETAIL_QUERY — resolve slug to questionId
query getQuestionDetail($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    questionId
  }
}

# SYNCED_CODE_QUERY — fetch cloud-saved code
query syncedCode($questionId: Int!, $lang: Int!) {
  syncedCode(questionId: $questionId, lang: $lang) {
    timestamp
    code
  }
}

# UPDATE_SYNCED_CODE_MUTATION — write cloud-saved code
mutation updateSyncedCode($code: String!, $lang: Int!, $questionId: Int!) {
  updateSyncedCode(code: $code, lang: $lang, questionId: $questionId) {
    ok
  }
}
```

## Error Handling

| Condition | Behavior |
|-----------|----------|
| Missing env vars | Return error: "LEETCODE_SESSION and LEETCODE_CSRF_TOKEN env vars required" |
| 401/403 response | Return error: "LeetCode session expired — re-extract cookies from browser" |
| Problem not found (`question` is `null`) | Return error: "Problem '{slug}' not found" |
| No cloud save (`syncedCode` is `null`) | Return: "No cloud-saved code for '{slug}' in {language}" |
| Invalid language slug | Return error listing valid language slugs |

## What Stays Unchanged

- `get_stats` tool — keeps its own unauthenticated headers, no changes
- `pyproject.toml` — no new dependencies needed (already has `httpx` and `mcp[cli]`)
- Server transport (stdio) and name (`"leetcode-stats"`)

## MCP Config Wiring

The agent's MCP config needs the env vars added. Example for `leetcode.mcp.json`:

```json
{
  "mcpServers": {
    "leetcode-stats": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/claub/mcps/leetcode-stats", "mcp", "run", "server.py"],
      "env": {
        "LEETCODE_SESSION": "<session-jwt>",
        "LEETCODE_CSRF_TOKEN": "<csrf-token>"
      }
    }
  }
}
```
