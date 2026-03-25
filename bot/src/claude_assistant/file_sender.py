"""Extract [FILE:/path] markers from agent responses and convert to Discord files."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import discord

log = logging.getLogger(__name__)

_FILE_PATTERN = re.compile(r"\[FILE:(.*?)\]")


def extract_files(text: str) -> tuple[str, list[discord.File]]:
    """Parse [FILE:/path] markers from text.

    Returns the cleaned text (markers removed) and a list of discord.File objects
    for paths that exist and are readable.
    """
    matches = _FILE_PATTERN.findall(text)
    if not matches:
        return text, []

    files: list[discord.File] = []
    for path_str in matches:
        path = Path(path_str.strip())
        if not path.is_file():
            log.warning("File attachment not found: %s", path)
            continue
        try:
            files.append(discord.File(path))
        except Exception:
            log.exception("Failed to open file for attachment: %s", path)

    cleaned = _FILE_PATTERN.sub("", text).strip()
    return cleaned, files
