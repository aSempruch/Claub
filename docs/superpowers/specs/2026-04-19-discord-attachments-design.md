# Discord Attachments → Agent

## Goal

Make Discord message attachments visible to agents. Today, only the message text reaches the agent process; any image, PDF, or other file the user attaches is silently dropped. After this change, the agent receives the message text plus a footer listing where each attached file was saved on disk, and can read or move those files using the tools it already has.

## Non-Goals

- **No native image content blocks.** v1 hands the agent file paths only. Inline image blocks via stream-json content arrays may come later.
- **No long-term storage.** Attachments are deliberately ephemeral.
- **No editing/deletion handling.** If a user edits or deletes a Discord message after the bot processed it, the downloaded files stay where they are. Out of scope.
- **No bot-side size cap.** Discord's per-tier upload limit (25 / 100 / 500 MB) is the natural ceiling.

## Lifecycle Decision

Attachments live in **container `/tmp`**, which is wiped on container restart. The agent can move anything it wants to keep into its workspace at `/claub/workspaces/{agent}/`. This was chosen over workspace-persistent storage with auto-prune because:

- Most attachment use is "look at this thing I just sent" — relevance is short-lived.
- Container restart is rare in practice but happens on every code deploy. Aligning ephemeral lifetime with container lifetime gives a clean reset point with no separate sweeper to maintain.
- The workspace already has `additionalDirectories: ["/tmp"]` permission and Bash, so move-to-keep works out of the box (verified during brainstorming with a live test against the running bot).

## Storage Layout

```
/tmp/claub-attachments/{agent_name}/{message_id}/{sanitized_filename}
```

- **Agent namespace** prevents one agent's downloads from being visible to another agent that happens to share `/tmp`.
- **Message ID** isolates each message; a filename collision across messages is impossible.
- **Sanitized filename**: keep characters in `[A-Za-z0-9._-]`, replace anything else with `_`. Strip path separators defensively. Discord allows spaces and unicode; canonicalizing before handing the path to a shell-aware agent avoids quoting bugs.

## Message Augmentation

After downloading, the bot appends a footer to the user's message text before passing it to `AgentProcess.send_message`:

```
{user's text — possibly empty}

[Attachments — saved to ephemeral /tmp (wiped on container restart). Move to your workspace if you want to keep them.
- /tmp/claub-attachments/main/1234567890/photo.png (image/png, 412 KB)
- /tmp/claub-attachments/main/1234567890/notes.pdf (application/pdf, 80 KB)]
```

Empty user text + attachments is a valid case (a message that's "just a photo"). The footer alone is non-empty content, so `send_message` accepts it.

If a download fails, that file appears as `- (failed: filename — reason)` in the footer. The agent sees what actually arrived; the bot does not surface download errors to the Discord channel.

## Code Shape

### New module: `bot/src/claude_assistant/attachments.py`

One async function:

```python
async def download_attachments(
    message: discord.Message,
    agent_name: str,
    base_dir: Path = Path("/tmp/claub-attachments"),
) -> str:
    """Download all attachments from a Discord message, return footer text.

    Returns "" when message.attachments is empty. Otherwise downloads each
    attachment to base_dir/{agent_name}/{message.id}/{sanitized_filename} and
    returns a footer string (with leading "\n\n") ready to append to the
    message content.

    Per-file failures are caught, logged, and surfaced in the footer as
    "- (failed: <name> — <reason>)" lines.
    """
```

Pure-ish — only Discord I/O is `attachment.save(path)`. Easy to unit-test by passing fake attachment objects with `.url`, `.filename`, `.size`, `.content_type`, and an async `.save(path)` method.

### Wiring: `discord_bot._handle_agent_message`

After `content = message.content.strip()` and the agent name is known, but before `_send_with_restart`:

```python
footer = await download_attachments(message, agent_name)
if footer:
    content = (content + footer) if content else footer.lstrip()
```

That's the entire integration. No changes to `AgentProcess`, `Router`, or anything downstream — the footer is just text in the user message.

### Skip cases

- `/clear` and `/stop` already early-return in `_handle_message` before the agent path. No change.
- Channels that don't route to an agent return early. No change.
- Bot/webhook authors are filtered. No change.

## Documentation

Two short additions:

- **`example/config/CLAUDE.md`** — new "Discord Attachments" subsection under the existing structure. Tells agents the marker means real files at the listed paths, that they're ephemeral, and to move into workspace if keeping.
- **`CLAUDE.md`** (project root) — one-paragraph note in the "Message Flow" section describing the download step.

## Testing

### Unit tests — `bot/tests/test_attachments.py`

Covers:

- No attachments → empty footer string.
- One attachment → footer formatted correctly, file written to expected path, sanitized name.
- Multiple attachments → footer lists all in order, all files written.
- Sanitization → filenames with `/`, spaces, unicode, leading dot, very long names map to safe paths.
- One of two attachments fails to download → successful one in footer normally, failed one as `(failed: ...)` line, no exception propagated.
- Filename collision impossible by construction (unique `message.id` dir), so not tested.

### Manual / integration

After deploy, send a message in a Discord channel that routes to an agent with one image and one PDF attached. Confirm the agent acknowledges them and can read both. Then ask it to move one into its workspace. Then `docker compose restart` and confirm `/tmp/claub-attachments/` is empty.

## Open Questions / Future Work

- **Native image vision.** A future iteration could re-encode images as stream-json image content blocks so the model sees them inline rather than via `Read`. Adds real complexity (per-image base64 sizing, model capability checks). Defer until v1 is in use.
- **Sweeper for long-running containers.** If a single container instance accumulates GBs of attachments before its next restart (unlikely given the current restart cadence), add a periodic sweep. Defer until observed.
