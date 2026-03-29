# LeetCode API Knowledge Transfer

Everything needed to build an MCP that retrieves cloud-saved code from LeetCode.

## Authentication

LeetCode uses two cookies for API auth. There is no OAuth flow — you extract these from a logged-in browser session.

### Required Cookies

| Cookie | Description |
|--------|-------------|
| `LEETCODE_SESSION` | Session JWT. Long-lived (weeks/months). |
| `csrftoken` | CSRF token. Must also be sent as an `x-csrftoken` header on every request. |

### How to Get Them

1. Log in to leetcode.com in a browser
2. Open DevTools → Application → Cookies → `https://leetcode.com`
3. Copy `LEETCODE_SESSION` and `csrftoken` values

### Required Headers (Every Request)

```
Cookie: LEETCODE_SESSION=<session>; csrftoken=<csrf>
x-csrftoken: <csrf>
Referer: https://leetcode.com
Origin: https://leetcode.com
Content-Type: application/json
User-Agent: <any reasonable browser UA>
```

The `Referer` and `Origin` headers are required — requests without them get rejected. The `User-Agent` doesn't need to be real but must be present and look browser-like.

### Session Expiry

A 401 or 403 response means the session has expired. There's no refresh mechanism — the user must re-extract cookies from their browser.

## API Endpoint

All queries go through a single GraphQL endpoint:

```
POST https://leetcode.com/graphql
```

Request body is always JSON with `query` and `variables` fields.

## Cloud Code Sync

LeetCode has a "cloud save" feature that stores one code snapshot per (problem, language) pair. This is the "Restore" button in the LeetCode editor.

### Reading Cloud-Saved Code

**GraphQL Query:**

```graphql
query syncedCode($questionId: Int!, $lang: Int!) {
  syncedCode(questionId: $questionId, lang: $lang) {
    timestamp
    code
  }
}
```

**Variables:**

```json
{
  "questionId": 1,
  "lang": 11
}
```

**Response:**

```json
{
  "data": {
    "syncedCode": {
      "timestamp": 1710000000,
      "code": "class Solution:\n    def twoSum(self, nums, target):\n        ..."
    }
  }
}
```

If no cloud save exists for that (problem, language) pair, `syncedCode` is `null`.

### Writing Cloud-Saved Code

```graphql
mutation updateSyncedCode($code: String!, $lang: Int!, $questionId: Int!) {
  updateSyncedCode(code: $code, lang: $lang, questionId: $questionId) {
    ok
  }
}
```

**Variables:**

```json
{
  "code": "class Solution: ...",
  "lang": 11,
  "questionId": 1
}
```

**Response:**

```json
{
  "data": {
    "updateSyncedCode": {
      "ok": true
    }
  }
}
```

### Key Detail: `questionId` vs `titleSlug`

The cloud sync endpoints use **numeric `questionId`** (the internal ID, e.g. `1`), NOT the `titleSlug` (e.g. `"two-sum"`). To go from a slug to an ID, you need to fetch the problem first (see below).

### Key Detail: `lang` is a Numeric ID

The cloud sync endpoints use a **numeric language ID**, not the string slug. The mapping:

| Language | ID | Slug |
|----------|----|------|
| C++ | 0 | `cpp` |
| Java | 1 | `java` |
| Python 2 | 2 | `python` |
| Python 3 | 11 | `python3` |
| C | 3 | `c` |
| C# | 4 | `csharp` |
| JavaScript | 5 | `javascript` |
| TypeScript | 12 | `typescript` |
| Ruby | 6 | `ruby` |
| Swift | 7 | `swift` |
| Go | 8 | `golang` |
| Kotlin | 9 | `kotlin` |
| Scala | 10 | `scala` |
| Rust | 13 | `rust` |
| PHP | 14 | `php` |
| Racket | 15 | `racket` |
| Erlang | 16 | `erlang` |
| Elixir | 17 | `elixir` |
| Dart | 18 | `dart` |

## Fetching Problem Details

Needed to resolve `titleSlug` → `questionId`, and to get default code snippets per language.

**GraphQL Query:**

```graphql
query getQuestionDetail($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    questionId
    questionFrontendId
    title
    titleSlug
    difficulty
    content
    isPaidOnly
    codeSnippets { lang langSlug code }
    exampleTestcases
    sampleTestCase
    metaData
    hints
    topicTags { name slug }
  }
}
```

**Variables:**

```json
{
  "titleSlug": "two-sum"
}
```

**Response shape:**

```json
{
  "data": {
    "question": {
      "questionId": "1",
      "questionFrontendId": "1",
      "title": "Two Sum",
      "titleSlug": "two-sum",
      "difficulty": "Easy",
      "content": "<p>Given an array of integers...</p>",
      "isPaidOnly": false,
      "codeSnippets": [
        { "lang": "Python3", "langSlug": "python3", "code": "class Solution:\n    def twoSum(self, nums: List[int], target: int) -> List[int]:\n" },
        { "lang": "JavaScript", "langSlug": "javascript", "code": "..." }
      ],
      "exampleTestcases": "[2,7,11,15]\n9",
      "sampleTestCase": "[2,7,11,15]\n9",
      "metaData": "{...}",
      "hints": [],
      "topicTags": [{ "name": "Array", "slug": "array" }]
    }
  }
}
```

**Notes:**
- `questionId` is a **string** here (e.g. `"1"`), but the cloud sync endpoints expect it as an **int**. Parse it.
- `questionFrontendId` is the number users see (e.g. "1. Two Sum"). Usually the same as `questionId` but not always.
- `content` is HTML.
- `question` is `null` if the session is expired or the problem doesn't exist.
- `codeSnippets` gives you the default starter code per language. The `langSlug` values (e.g. `"python3"`, `"javascript"`) correspond to the keys in the language ID map above.

## Running Code / Submitting (Bonus)

These are REST endpoints, not GraphQL.

### Run Code (Test Against Custom Cases)

```
POST https://leetcode.com/problems/{titleSlug}/interpret_solution/
```

```json
{
  "lang": "python3",
  "question_id": "1",
  "typed_code": "class Solution:\n    ...",
  "data_input": "[2,7,11,15]\n9"
}
```

Returns `{ "interpret_id": "runcode_..." }`. Poll the check endpoint to get results.

### Submit Code

```
POST https://leetcode.com/problems/{titleSlug}/submit/
```

```json
{
  "lang": "python3",
  "question_id": "1",
  "typed_code": "class Solution:\n    ...",
  "judge_type": "large"
}
```

Returns `{ "submission_id": 123456789 }`. Poll the check endpoint to get results.

### Poll for Results

```
GET https://leetcode.com/submissions/detail/{id}/check/
```

Poll every ~1.5s until `state == "SUCCESS"`. Response fields include:

| Field | Type | Notes |
|-------|------|-------|
| `state` | string | `"PENDING"`, `"STARTED"`, `"SUCCESS"` |
| `status_code` | int | 10 = Accepted, 11 = Wrong Answer, 15 = Runtime Error, 20 = Compile Error, etc. |
| `status_msg` | string | Human-readable status |
| `status_runtime` | string | e.g. `"4 ms"` |
| `status_memory` | string | e.g. `"16.5 MB"` |
| `runtime_percentile` | float | e.g. `95.23` |
| `total_correct` | int | Test cases passed |
| `total_testcases` | int | Total test cases |
| `code_output` | string OR list | Output per test case. **String** for submissions, **List\<String\>** for test runs. |
| `code_answer` | string OR list | Same inconsistency as above |
| `compile_error` | string | Compiler error message if applicable |
| `runtime_error` | string | Runtime error message if applicable |
| `last_testcase` | string | The failing test input |
| `expected_output` | string | Expected answer for failing test |

**Gotcha:** `code_output` and `code_answer` are `List<String>` for test runs (`interpret_solution`) but a plain `String` for submissions (`submit`). Your deserializer needs to handle both.

## MCP Design Suggestions

For an MCP that fetches cloud-saved code:

### Minimal Tool Set

1. **`get_cloud_code(problem, language)`** — Takes a problem slug (e.g. `"two-sum"`) and language slug (e.g. `"python3"`). Internally: fetch problem to get `questionId`, look up the numeric `lang` ID, call `syncedCode` query. Return the code string.

2. **`get_problem(slug)`** — Fetch problem description, starter code snippets, test cases. Useful context for the AI.

3. **`save_cloud_code(problem, language, code)`** — Write code back to cloud save.

### Auth Configuration

Store `LEETCODE_SESSION` and `csrftoken` as MCP server config/env vars. The user provides them once; the MCP reuses them until they expire.

### Example Full Request (curl)

```bash
# Fetch cloud-saved Python 3 code for "two-sum"
# Step 1: Get questionId
curl -s 'https://leetcode.com/graphql' \
  -H 'Cookie: LEETCODE_SESSION=...; csrftoken=...' \
  -H 'x-csrftoken: ...' \
  -H 'Referer: https://leetcode.com' \
  -H 'Origin: https://leetcode.com' \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { question(titleSlug: \"two-sum\") { questionId } }","variables":{}}'

# Step 2: Fetch synced code (questionId=1, lang=11 for python3)
curl -s 'https://leetcode.com/graphql' \
  -H 'Cookie: LEETCODE_SESSION=...; csrftoken=...' \
  -H 'x-csrftoken: ...' \
  -H 'Referer: https://leetcode.com' \
  -H 'Origin: https://leetcode.com' \
  -H 'Content-Type: application/json' \
  -d '{"query":"query syncedCode($questionId: Int!, $lang: Int!) { syncedCode(questionId: $questionId, lang: $lang) { timestamp code } }","variables":{"questionId":1,"lang":11}}'
```
