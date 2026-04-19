"""Download Discord message attachments to ephemeral /tmp and build a footer
listing them for the agent.

The bot lives in a container; /tmp is wiped when the container is rebuilt. Agents have
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


def _dedupe_name(name: str, seen: set[str]) -> str:
    """Return *name* unchanged if not in *seen*, else append _2, _3, … before the extension."""
    if name not in seen:
        seen.add(name)
        return name
    stem, dot, ext = name.rpartition(".")
    base = stem if dot else name
    suffix = f".{ext}" if dot else ""
    i = 2
    while True:
        candidate = f"{base}_{i}{suffix}"
        if candidate not in seen:
            seen.add(candidate)
            return candidate
        i += 1


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
        "(wiped on container rebuild). Move to your workspace if you "
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
    seen: set[str] = set()
    for att in attachments:
        safe_name = _dedupe_name(_sanitize_filename(att.filename), seen)
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
