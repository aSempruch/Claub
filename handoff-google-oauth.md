# Handoff: Set up Google OAuth credentials for Claub Gmail/Calendar MCP

## Goal

Produce two files and hand them back to the user:

1. **`client_secret.json`** — OAuth 2.0 Desktop client credentials
2. **`token.json`** — A refresh token authorized for `gmail.readonly` + `calendar.readonly` scopes

These will be mounted into a Docker container for a long-running personal bot (Claub) that reads — but never writes — Gmail and Calendar.

## Context you need

- This is for a **single user** (the user running you). It will never be distributed.
- Restricted scope: `https://www.googleapis.com/auth/gmail.readonly`
- Sensitive scope: `https://www.googleapis.com/auth/calendar.readonly`
- The OAuth app **must be published to "In production"** (not Testing) so refresh tokens don't expire after 7 days. Personal-use single-user apps do NOT need Google verification or a CASA security assessment as long as you stay under 100 users — you just click through an "unverified app" warning once during the initial consent flow. This is documented and intended behavior. See <https://support.google.com/cloud/answer/13464323>.
- You have `gcloud` CLI access. Some steps **require the web Console** (`console.cloud.google.com`) because gcloud doesn't fully cover OAuth consent screen config for non-Workspace projects. Those steps are clearly marked **[WEB UI]**. Tell the user when you hit one and need them to do it; don't try to fake it via undocumented API calls.

## Prerequisites — verify first

```bash
gcloud --version
gcloud auth list                # confirm logged in as the right Google account
gcloud config get-value account
```

If not authenticated: `gcloud auth login`. The account you log in as MUST be the same Google account whose Gmail/Calendar will be read.

## Steps

### 1. Create a dedicated GCP project

```bash
PROJECT_ID="claub-gmail-$(date +%s)"   # or any unique id the user prefers
gcloud projects create "$PROJECT_ID" --name="Claub Gmail Reader"
gcloud config set project "$PROJECT_ID"
```

Note the project ID and report it to the user.

### 2. Enable the required APIs

```bash
gcloud services enable gmail.googleapis.com calendar-json.googleapis.com
```

Verify:
```bash
gcloud services list --enabled | grep -E "gmail|calendar"
```

### 3. **[WEB UI]** Configure OAuth consent screen

Direct the user to:
<https://console.cloud.google.com/apis/credentials/consent?project=PROJECT_ID> (substitute the real project id)

They need to:
1. Choose **External** user type → Create
2. Fill in App name (e.g. "Claub Gmail Reader"), user support email, developer contact email. Everything else can be left blank.
3. **Scopes step:** click "Add or Remove Scopes" and manually add:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/calendar.readonly`
   (These won't appear in the suggested list — paste them into the "Manually add scopes" box at the bottom.)
4. Save and continue through the remaining steps (Test users, Summary).
5. **Critical:** Back on the OAuth consent screen overview, click **"PUBLISH APP"** → confirm. Status should change from "Testing" to "In production". If you skip this, refresh tokens will expire every 7 days.
6. Ignore any "Prepare for verification" prompts. We are NOT going through verification.

Wait for the user to confirm this is done before proceeding.

### 4. Create the OAuth client (Desktop type)

```bash
# gcloud doesn't have a clean command for creating Desktop OAuth clients in
# non-Workspace projects. Use the web UI as a fallback if the CLI path fails.
```

**Try CLI first** (may not work on all account types):
```bash
gcloud alpha iap oauth-clients create \
  --display_name="Claub Desktop Client" \
  projects/$(gcloud config get-value project)/brands/$(gcloud alpha iap oauth-brands list --format='value(name)' | head -1 | sed 's|.*/||')
```

**If that fails** (it often does for personal Gmail accounts — IAP brands are a Workspace thing), fall back to **[WEB UI]**:

Direct user to:
<https://console.cloud.google.com/apis/credentials?project=PROJECT_ID>

1. Click **+ CREATE CREDENTIALS** → **OAuth client ID**
2. Application type: **Desktop app**
3. Name: "Claub Desktop Client"
4. Click Create
5. In the modal, click **DOWNLOAD JSON**. Save it as `client_secret.json`.
6. Have the user place it at `/Users/you/Claude/client_secret.json` (or wherever you're working — confirm with them).

### 5. Mint the refresh token via one-shot OAuth flow

Write this script as `/tmp/mint_token.py`:

```python
#!/usr/bin/env python3
"""One-shot: run OAuth installed-app flow, save refresh token to token.json."""
import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

CLIENT_SECRET = Path(sys.argv[1] if len(sys.argv) > 1 else "client_secret.json")
TOKEN_OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "token.json")

flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
# access_type=offline + prompt=consent forces a refresh token to be issued
creds = flow.run_local_server(
    port=0,
    access_type="offline",
    prompt="consent",
    open_browser=True,
)

TOKEN_OUT.write_text(json.dumps({
    "refresh_token": creds.refresh_token,
    "client_id": creds.client_id,
    "client_secret": creds.client_secret,
    "token_uri": creds.token_uri,
    "scopes": creds.scopes,
}, indent=2))
print(f"Wrote {TOKEN_OUT}")
print(f"Has refresh_token: {bool(creds.refresh_token)}")
```

Install deps and run:
```bash
uv run --with google-auth-oauthlib python /tmp/mint_token.py \
  /Users/you/Claude/client_secret.json \
  /Users/you/Claude/token.json
```

This will:
1. Open the user's default browser
2. Show Google's account picker → user picks the account
3. Show the **"Google hasn't verified this app"** warning → user clicks "Advanced" → "Go to Claub Gmail Reader (unsafe)"
4. Show consent for the two scopes → user clicks Allow
5. Redirect back to localhost; the script captures the code and writes `token.json`

**Verify the token has a refresh_token field.** If `Has refresh_token: False`, something went wrong (most commonly: the consent screen wasn't published, or the user previously consented and Google didn't re-issue one). Re-run with the user revoking access first at <https://myaccount.google.com/permissions>.

### 6. Smoke test

```bash
uv run --with google-auth --with google-api-python-client python -c "
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

data = json.load(open('/Users/you/Claude/token.json'))
creds = Credentials(
    token=None,
    refresh_token=data['refresh_token'],
    client_id=data['client_id'],
    client_secret=data['client_secret'],
    token_uri=data['token_uri'],
    scopes=data['scopes'],
)
gmail = build('gmail', 'v1', credentials=creds)
profile = gmail.users().getProfile(userId='me').execute()
print('Gmail OK:', profile['emailAddress'], 'messages:', profile['messagesTotal'])

cal = build('calendar', 'v3', credentials=creds)
cals = cal.calendarList().list(maxResults=3).execute()
print('Calendar OK:', len(cals.get('items', [])), 'calendars visible')
"
```

Both lines should print successfully. If you get a 403 with "insufficient authentication scopes", the consent screen wasn't configured with both scopes — go back to step 3.

### 7. Hand back

Report to the user:
- Path to `client_secret.json`
- Path to `token.json`
- Project ID
- Confirmation that the smoke test passed (paste the output)
- Reminder: `token.json` contains a long-lived refresh token. Treat it like a password. The bot will mount it read-only into the container.

## Failure modes to watch for

- **"This app isn't verified" with no Advanced link** → User is logged into a Workspace account that has admin restrictions. Switch to a personal account or get admin to allow.
- **`access_denied` after consent** → Consent screen still in Testing mode and the user's account isn't in the test users list. Either add them as a test user OR (preferred) publish the app per step 3.6.
- **`Has refresh_token: False`** → Forgot `prompt=consent`, or user had previously authorized this client. Have user revoke at <https://myaccount.google.com/permissions> and retry.
- **gcloud commands fail with permission errors** → User's gcloud account doesn't have the right org/project permissions. For a personal Gmail account this should "just work"; for Workspace accounts the org admin may need to allow project creation.

## Do NOT do

- Do NOT submit the app for verification. It's not needed and would block on a multi-week security review.
- Do NOT add the user's bot/Discord/whatever as a "test user" — that's the Testing-mode workaround which causes the 7-day expiry. Publishing the app is the correct path.
- Do NOT request any scopes beyond `gmail.readonly` and `calendar.readonly`. The user explicitly wants read-only.
- Do NOT use a service account. Service accounts can't access personal Gmail (only Workspace with domain-wide delegation).
- Do NOT commit `token.json` or `client_secret.json` to git. Confirm `.gitignore` covers them before finishing.
