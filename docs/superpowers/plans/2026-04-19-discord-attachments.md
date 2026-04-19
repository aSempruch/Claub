# Discord Attachments → Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a Discord message has file attachments, download them to ephemeral container `/tmp` and append a footer to the message text so the agent can read or move the files.

**Architecture:** New pure-ish helper module `attachments.py` does the downloading and footer-building. `discord_bot._handle_agent_message` calls it once per message and concatenates the footer onto the user's text before invoking the agent process. No changes to `AgentProcess`, `Router`, scheduler, or session storage.

**Tech Stack:** Python 3.12, `discord.py` (Attachment.save async), pytest + pytest-asyncio, existing project conventions.

**Spec:** `docs/superpowers/specs/2026-04-19-discord-attachments-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `bot/src/claude_assistant/attachments.py` | Create | Filename sanitization, human-readable size formatting, footer construction, per-attachment download with per-file error capture. Exports `download_attachments(message, agent_name, base_dir=...)`. |
| `bot/src/claude_assistant/discord_bot.py` | Modify (`_handle_agent_message`, ~line 296) | Call `download_attachments` after routing succeeds; concatenate footer onto `content` before sending to the agent. |
| `bot/tests/test_attachments.py` | Create | Unit tests for `download_attachments` and its helpers. Uses fakes that mimic `discord.Attachment` (no real network I/O). |
| `bot/tests/test_discord_bot.py` | Modify (append) | One test verifying `_handle_agent_message` appends the footer to the message content sent to the process. |
| `example/config/CLAUDE.md` | Modify | New short subsection "Discord Attachments" telling agents what the marker means and where the files live. |
| `CLAUDE.md` (project root) | Modify | One-paragraph note in the "Message Flow" section. |

---

## Task 1: Create attachments helper with full test coverage (TDD)

**Files:**
- Create: `bot/src/claude_assistant/attachments.py`
- Create: `bot/tests/test_attachments.py`

### Background for the implementer

`discord.Attachment` (from `discord.py`) has these attributes the helper uses:

- `filename: str` — original name as uploaded.
- `size: int` — bytes.
- `content_type: str | None` — e.g. `"image/png"`. May be `None` for some types.
- `url: str` — CDN URL. Not used directly; we let the SDK do the fetch.
- `async save(fp: PathLike | BufferedIOBase) -> int` — downloads and writes the bytes.

Tests use a `FakeAttachment` dataclass whose `save` writes pre-set bytes (or raises) so no network is needed.

The "agent name" passed in becomes a path component, but agent names in this project are already filesystem-safe (lowercase letters, used elsewhere as directory names — see `workspaces/{name}/`), so no extra sanitization is required for that field.

### TDD steps

- [ ] **Step 1: Write the failing tests**

Create `bot/tests/test_attachments.py`:

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from claude_assistant.attachments import (
    _format_size,
    _sanitize_filename,
    download_attachments,
)


# --- Fakes ---

@dataclass
class FakeAttachment:
    filename: str
    size: int
    content_type: str | None = "application/octet-stream"
    payload: bytes = b"data"
    raises: Exception | None = None

    async def save(self, fp: Any) -> int:
        if self.raises is not None:
            raise self.raises
        Path(fp).write_bytes(self.payload)
        return len(self.payload)


@dataclass
class FakeMessage:
    id: int = 1234567890
    attachments: list[FakeAttachment] = field(default_factory=list)


# --- _sanitize_filename ---

def test_sanitize_passthrough_safe_chars() -> None:
    assert _sanitize_filename("photo.png") == "photo.png"
    assert _sanitize_filename("my-file_v2.tar.gz") == "my-file_v2.tar.gz"


def test_sanitize_replaces_unsafe_chars() -> None:
    assert _sanitize_filename("my photo (1).png") == "my_photo__1_.png"


def test_sanitize_strips_path_components() -> None:
    assert _sanitize_filename("../etc/passwd") == "passwd"
    assert _sanitize_filename("a/b/c.txt") == "c.txt"
    assert _sanitize_filename(r"C:\windows\evil.exe") == "evil.exe"


def test_sanitize_replaces_unicode() -> None:
    # Each non-ASCII char becomes one underscore
    assert _sanitize_filename("日本語.txt") == "___.txt"


def test_sanitize_empty_falls_back() -> None:
    assert _sanitize_filename("") == "attachment"
    # All-unsafe stripped to nothing also falls back
    assert _sanitize_filename("///") == "attachment"


# --- _format_size ---

def test_format_size_bytes() -> None:
    assert _format_size(0) == "0 B"
    assert _format_size(512) == "512 B"
    assert _format_size(1023) == "1023 B"


def test_format_size_kb() -> None:
    assert _format_size(1024) == "1.0 KB"
    assert _format_size(412 * 1024) == "412.0 KB"


def test_format_size_mb() -> None:
    assert _format_size(1024 * 1024) == "1.0 MB"
    assert _format_size(int(2.5 * 1024 * 1024)) == "2.5 MB"


# --- download_attachments ---

@pytest.mark.asyncio
async def test_no_attachments_returns_empty(tmp_path: Path) -> None:
    msg = FakeMessage(attachments=[])
    footer = await download_attachments(msg, "main", base_dir=tmp_path)
    assert footer == ""
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_single_attachment_writes_file_and_returns_footer(tmp_path: Path) -> None:
    att = FakeAttachment(filename="photo.png", size=412 * 1024, content_type="image/png", payload=b"PNGDATA")
    msg = FakeMessage(id=42, attachments=[att])

    footer = await download_attachments(msg, "main", base_dir=tmp_path)

    saved = tmp_path / "main" / "42" / "photo.png"
    assert saved.is_file()
    assert saved.read_bytes() == b"PNGDATA"
    assert footer.startswith("\n\n[Attachments")
    assert "ephemeral /tmp" in footer
    assert str(saved) in footer
    assert "(image/png, 412.0 KB)" in footer
    assert footer.endswith("]")


@pytest.mark.asyncio
async def test_multiple_attachments_each_listed_in_order(tmp_path: Path) -> None:
    a = FakeAttachment(filename="a.txt", size=10, content_type="text/plain", payload=b"a" * 10)
    b = FakeAttachment(filename="b.pdf", size=2048, content_type="application/pdf", payload=b"b" * 2048)
    msg = FakeMessage(id=99, attachments=[a, b])

    footer = await download_attachments(msg, "journalist", base_dir=tmp_path)

    assert (tmp_path / "journalist" / "99" / "a.txt").read_bytes() == b"a" * 10
    assert (tmp_path / "journalist" / "99" / "b.pdf").read_bytes() == b"b" * 2048
    # Both files appear, a before b
    a_pos = footer.find("a.txt")
    b_pos = footer.find("b.pdf")
    assert 0 < a_pos < b_pos


@pytest.mark.asyncio
async def test_unsafe_filename_sanitized_in_path_and_footer(tmp_path: Path) -> None:
    att = FakeAttachment(filename="my photo (1).png", size=4, payload=b"data")
    msg = FakeMessage(id=7, attachments=[att])

    footer = await download_attachments(msg, "main", base_dir=tmp_path)

    saved = tmp_path / "main" / "7" / "my_photo__1_.png"
    assert saved.is_file()
    assert "my_photo__1_.png" in footer
    # Original unsafe name not used as a real path component
    assert not (tmp_path / "main" / "7" / "my photo (1).png").exists()


@pytest.mark.asyncio
async def test_one_failure_does_not_block_others(tmp_path: Path) -> None:
    good = FakeAttachment(filename="good.txt", size=4, payload=b"good")
    bad = FakeAttachment(filename="bad.bin", size=8, raises=RuntimeError("network down"))
    msg = FakeMessage(id=5, attachments=[good, bad])

    footer = await download_attachments(msg, "main", base_dir=tmp_path)

    assert (tmp_path / "main" / "5" / "good.txt").read_bytes() == b"good"
    assert "good.txt" in footer
    assert "(failed: bad.bin" in footer
    assert "network down" in footer


@pytest.mark.asyncio
async def test_missing_content_type_omitted_gracefully(tmp_path: Path) -> None:
    att = FakeAttachment(filename="thing", size=3, content_type=None, payload=b"abc")
    msg = FakeMessage(id=1, attachments=[att])

    footer = await download_attachments(msg, "main", base_dir=tmp_path)

    # Footer should not contain the literal string "None"
    assert "None" not in footer
    assert "thing" in footer
    assert "3 B" in footer
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /Users/you/Claude/bot
uv run --extra dev pytest tests/test_attachments.py -v
```

Expected: All tests fail with `ModuleNotFoundError: No module named 'claude_assistant.attachments'` (or similar import error).

- [ ] **Step 3: Write the implementation**

Create `bot/src/claude_assistant/attachments.py`:

```python
"""Download Discord message attachments to ephemeral /tmp and build a footer
listing them for the agent.

The bot lives in a container; /tmp is wiped on container restart. Agents have
permission to read/write /tmp and Bash, so they can move attachments into
their workspace if they want to keep them.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

DEFAULT_BASE_DIR = Path("/tmp/claub-attachments")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


class _AttachmentLike(Protocol):
    filename: str
    size: int
    content_type: str | None

    async def save(self, fp: Any) -> int: ...


def _sanitize_filename(name: str) -> str:
    """Make a Discord-supplied filename safe to use as a path component.

    Strips any path separators (defensive against malicious filenames),
    then replaces anything outside [A-Za-z0-9._-] with underscore.
    Returns "attachment" if the result would be empty.
    """
    # Strip directory components from either separator style.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _SAFE_NAME_RE.sub("_", name)
    return name or "attachment"


def _format_size(n: int) -> str:
    """Human-readable byte count (B / KB / MB)."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _format_line(path: Path, content_type: str | None, size: int) -> str:
    parts = []
    if content_type:
        parts.append(content_type)
    parts.append(_format_size(size))
    return f"- {path} ({', '.join(parts)})"


def _build_footer(lines: list[str]) -> str:
    return (
        "\n\n[Attachments — saved to ephemeral /tmp "
        "(wiped on container restart). Move to your workspace if you "
        "want to keep them.\n"
        + "\n".join(lines)
        + "]"
    )


async def download_attachments(
    message: Any,
    agent_name: str,
    base_dir: Path = DEFAULT_BASE_DIR,
) -> str:
    """Download attachments from a Discord message, return footer text.

    Returns "" when the message has no attachments. Otherwise writes each
    attachment to base_dir/{agent_name}/{message.id}/{sanitized_filename}
    and returns a footer (with leading "\\n\\n") to append to the message.

    Per-attachment failures are caught and surfaced in the footer as
    "- (failed: <filename> — <reason>)" lines; they never raise out.
    """
    attachments: list[_AttachmentLike] = list(getattr(message, "attachments", []) or [])
    if not attachments:
        return ""

    target_dir = base_dir / agent_name / str(message.id)
    target_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for att in attachments:
        safe_name = _sanitize_filename(att.filename)
        dest = target_dir / safe_name
        try:
            await att.save(dest)
        except Exception as exc:
            log.warning(
                "Failed to download attachment %r for agent %s: %s",
                att.filename, agent_name, exc,
            )
            lines.append(f"- (failed: {att.filename} — {exc})")
            continue
        lines.append(_format_line(dest, att.content_type, att.size))

    return _build_footer(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

```
cd /Users/you/Claude/bot
uv run --extra dev pytest tests/test_attachments.py -v
```

Expected: All 13 tests PASS.

- [ ] **Step 5: Commit**

```
cd /Users/you/Claude
git add bot/src/claude_assistant/attachments.py bot/tests/test_attachments.py
git commit -m "feat: download Discord attachments to ephemeral /tmp"
```

---

## Task 2: Wire downloader into _handle_agent_message

**Files:**
- Modify: `bot/src/claude_assistant/discord_bot.py` (imports, `_handle_agent_message`)
- Modify: `bot/tests/test_discord_bot.py` (append one test)

### TDD steps

- [ ] **Step 1: Write the failing test**

Append to `bot/tests/test_discord_bot.py`:

```python
@pytest.mark.asyncio
async def test_handle_agent_message_appends_attachment_footer(
    bot: AssistantBot, tmp_path: Path
) -> None:
    """When a Discord message has attachments, the bot downloads them and
    appends a footer to the content passed to the agent process."""
    from claude_assistant.attachments import DEFAULT_BASE_DIR

    captured: dict[str, str] = {}

    async def fake_send_with_restart(agent_name: str, content: str) -> str:
        captured["agent"] = agent_name
        captured["content"] = content
        return "ok"

    # FakeAttachment mirrors discord.Attachment surface; writes payload on save.
    class FakeAttachment:
        def __init__(self) -> None:
            self.filename = "hi.txt"
            self.size = 5
            self.content_type = "text/plain"

        async def save(self, fp: Any) -> int:  # type: ignore[name-defined]
            Path(fp).write_bytes(b"hello")
            return 5

    msg = MagicMock()
    msg.channel.id = 100  # routes to "main"
    msg.channel.send = AsyncMock()
    msg.channel.typing = MagicMock(return_value=AsyncMock().__aenter__.__self__)
    # Need a real async-context-manager for `async with channel.typing()`
    typing_cm = AsyncMock()
    typing_cm.__aenter__ = AsyncMock(return_value=None)
    typing_cm.__aexit__ = AsyncMock(return_value=None)
    msg.channel.typing = MagicMock(return_value=typing_cm)
    msg.id = 555
    msg.attachments = [FakeAttachment()]

    # Patch session lookup to a no-op
    bot.sessions.get = MagicMock(return_value=None)
    # Patch the attachments base dir to tmp_path so we don't write to /tmp
    with patch(
        "claude_assistant.discord_bot.download_attachments",
        side_effect=lambda m, a: __import__("claude_assistant.attachments", fromlist=["download_attachments"]).download_attachments(m, a, base_dir=tmp_path),
    ):
        with patch.object(bot, "_send_with_restart", side_effect=fake_send_with_restart):
            with patch.object(bot, "_send_chunked", new=AsyncMock()):
                await bot._handle_agent_message(msg, "main", "hello agent")

    assert captured["agent"] == "main"
    assert captured["content"].startswith("hello agent")
    assert "[Attachments" in captured["content"]
    assert "hi.txt" in captured["content"]
    # File was actually written to the patched base_dir
    assert (tmp_path / "main" / "555" / "hi.txt").read_bytes() == b"hello"
```

Imports already at top of file: `Any` is needed — extend the import line:

```python
from typing import Any
```

(Add this at the top of `test_discord_bot.py` if not already present.)

- [ ] **Step 2: Run test to verify it fails**

```
cd /Users/you/Claude/bot
uv run --extra dev pytest tests/test_discord_bot.py::test_handle_agent_message_appends_attachment_footer -v
```

Expected: FAIL — either `ImportError` (no `download_attachments` in `discord_bot`) or assertion failure (footer not appended).

- [ ] **Step 3: Implement the wiring**

In `bot/src/claude_assistant/discord_bot.py`, add to imports near the top (with the other `claude_assistant.*` imports):

```python
from claude_assistant.attachments import download_attachments
```

Modify `_handle_agent_message` (currently at ~line 296). The existing body is:

```python
    async def _handle_agent_message(
        self, message: discord.Message, agent_name: str, content: str
    ) -> None:
        async with message.channel.typing():
            try:
                result = await self._send_with_restart(agent_name, content)
            except AuthenticationError:
                ...
```

Replace with:

```python
    async def _handle_agent_message(
        self, message: discord.Message, agent_name: str, content: str
    ) -> None:
        async with message.channel.typing():
            footer = await download_attachments(message, agent_name)
            if footer:
                content = (content + footer) if content else footer.lstrip()
            try:
                result = await self._send_with_restart(agent_name, content)
            except AuthenticationError:
                log.error("Claude authentication failed for %s", agent_name)
                await message.channel.send(
                    "Claude authentication expired. Re-authenticate with `claude` on the host and restart."
                )
                return

            process = self._processes.get(agent_name)
            if process and process.session_id:
                self.sessions.set(agent_name, process.session_id)

        await self._send_chunked(message.channel, agent_name, result)
```

Only the two new lines (`footer = ...` and the `if footer:` block) are inserted; everything else is unchanged.

- [ ] **Step 4: Run the new test and the full discord_bot suite**

```
cd /Users/you/Claude/bot
uv run --extra dev pytest tests/test_discord_bot.py tests/test_attachments.py -v
```

Expected: All tests PASS, including the new one and the existing reset/route tests.

- [ ] **Step 5: Run the full unit test suite to confirm no regressions**

```
cd /Users/you/Claude/bot
uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py
```

Expected: All tests PASS.

- [ ] **Step 6: Commit**

```
cd /Users/you/Claude
git add bot/src/claude_assistant/discord_bot.py bot/tests/test_discord_bot.py
git commit -m "feat: surface Discord attachments to agents via message footer"
```

---

## Task 3: Update agent-facing and project documentation

**Files:**
- Modify: `example/config/CLAUDE.md`
- Modify: `CLAUDE.md` (project root)

### Steps

- [ ] **Step 1: Add agent-facing section to example CLAUDE.md**

In `example/config/CLAUDE.md`, locate the "### Sending files" subsection (under "## Discord Behavior"). Insert a new subsection **immediately after** the `### Sending files` block and **before** `### Opting out of posting`:

```markdown
### Receiving attachments

When a user sends a Discord message with files attached, the bot downloads them to ephemeral container `/tmp` before handing the message to you. Each file shows up at:

```
/tmp/claub-attachments/{your-agent-name}/{discord_message_id}/{filename}
```

You'll see a footer at the end of the user's message listing every attachment with its path, MIME type, and size. Read the files with the normal `Read` tool — images, PDFs, text, and notebooks all work.

These files live in `/tmp` and are wiped on container restart. If you want to keep one for later, move it into your workspace (e.g. `mv /tmp/claub-attachments/main/12345/photo.png /claub/workspaces/main/photo.png`) — otherwise it's gone the next time the bot deploys.

If a download failed, the footer line for that file looks like `(failed: filename — reason)` instead of a real path. Acknowledge it to the user; don't pretend the file is there.
```

- [ ] **Step 2: Add a Message Flow note to project CLAUDE.md**

In `CLAUDE.md` (project root), find the "## Message Flow" section. Insert a new step between the current step 2 ("Router checks channel ID...") and step 3 ("Bot gets or starts the agent's persistent stream-json process..."), renumbering the rest:

```markdown
3. If the message has Discord attachments, the bot downloads each to `/tmp/claub-attachments/{agent}/{message_id}/{sanitized_filename}` and appends a footer to the message text listing the paths, MIME types, and sizes. Failures are surfaced as `(failed: …)` lines in the footer rather than aborting the send. Files live in container `/tmp` and are wiped on container restart; agents move them into their workspace if they want to keep them.
```

(After insertion, the existing steps 3-6 become 4-7. Update those numbers.)

- [ ] **Step 3: Verify renders correctly (manual eyeball)**

Open both files and confirm:
- `example/config/CLAUDE.md` has "### Receiving attachments" cleanly between "### Sending files" and "### Opting out of posting".
- `CLAUDE.md` Message Flow section is now 7 numbered steps with the new step 3 in place.

- [ ] **Step 4: Commit**

```
cd /Users/you/Claude
git add example/config/CLAUDE.md CLAUDE.md
git commit -m "docs: document Discord attachment flow for agents and developers"
```

---

## Task 4: Live verification with debug_agent

**Files:** none.

This is a manual smoke test against the deployed bot. It does **not** require running the full Discord client — `debug_agent` exercises the same agent process configuration with a fresh session, and we can simulate the footer by hand to confirm the agent reads the file as expected. The end-to-end Discord-message path is verified by the unit tests in Task 2; this task just confirms the agent's tool behavior on a real path.

- [ ] **Step 1: Rebuild and restart the container with the new code**

```
cd /Users/you/Claude
docker compose up -d --build
```

Expected: build succeeds, container comes up. Tail the logs briefly to confirm:

```
docker compose logs --tail 30
```

Look for `Discord connected as ...`. No tracebacks.

- [ ] **Step 2: Stage a fake attachment in the container**

```
docker exec claude-claub-1 sh -c 'mkdir -p /tmp/claub-attachments/main/999 && echo "this is a fake attachment" > /tmp/claub-attachments/main/999/note.txt'
```

- [ ] **Step 3: Run the agent with a simulated attachment-style prompt**

```
docker exec claude-claub-1 uv run --project /app/bot python -m claude_assistant.debug_agent main -p "I sent you something.

[Attachments — saved to ephemeral /tmp (wiped on container restart). Move to your workspace if you want to keep them.
- /tmp/claub-attachments/main/999/note.txt (text/plain, 25 B)]

Read the file and tell me what it says, in one short sentence."
```

Expected: the agent's reply (last line of output) quotes or paraphrases "this is a fake attachment". If the agent says it can't find the file or asks where to look, the wiring is broken.

- [ ] **Step 4: Clean up and confirm restart wipes /tmp**

```
docker exec claude-claub-1 rm -rf /tmp/claub-attachments
docker compose restart
docker exec claude-claub-1 ls /tmp/claub-attachments 2>&1 || echo "(gone, as expected)"
```

Expected: directory does not exist after restart.

- [ ] **Step 5: Mark plan complete**

No commit — this task is verification only.

---

## Self-Review

**Spec coverage:**
- Storage layout (`/tmp/claub-attachments/{agent}/{message_id}/{file}`) — Task 1, sanitization tests + implementation.
- Footer format — Task 1, multiple tests verify exact strings.
- Empty-text + attachments — handled in Task 2 implementation (`(content + footer) if content else footer.lstrip()`); also covered structurally by `_build_footer` always returning non-empty when called.
- Skip cases (`/clear`, `/stop`, unknown channel) — no new code needed; existing early returns in `_handle_message` already short-circuit before `_handle_agent_message`. No regression risk.
- Per-file failure handling — Task 1 test `test_one_failure_does_not_block_others`.
- No size cap, no cleanup task — explicitly nothing to implement.
- Docs (example CLAUDE.md + project CLAUDE.md) — Task 3.
- Tests — Tasks 1 & 2.

**Placeholder scan:** No "TBD"/"TODO"/vague-error-handling phrasing. Each step has either runnable code or a runnable command.

**Type consistency:** `download_attachments(message, agent_name, base_dir=...)` signature is identical in attachments.py, every test, and the discord_bot.py call site. Helper names `_sanitize_filename`, `_format_size`, `_build_footer`, `_format_line`, and the constant `DEFAULT_BASE_DIR` are consistent across module and tests.

**Notes:**
- All commits land on `main` (matches existing project workflow — recent history shows direct-to-main commits, no feature-branch convention).
- No worktree is being used; the bot project doesn't appear to use them and the spec was committed directly to main with the user's approval.
