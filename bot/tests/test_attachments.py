from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from claude_assistant.attachments import (
    _format_size,
    _sanitize_filename,
    download_attachments,
    _dedupe_name,
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


@pytest.mark.asyncio
async def test_within_message_collision_deduplicated(tmp_path: Path) -> None:
    # Two filenames that sanitize to the same string
    a = FakeAttachment(filename="日本語.txt", size=3, payload=b"AAA")
    b = FakeAttachment(filename="한국어.txt", size=3, payload=b"BBB")
    msg = FakeMessage(id=11, attachments=[a, b])

    footer = await download_attachments(msg, "main", base_dir=tmp_path)

    first = tmp_path / "main" / "11" / "___.txt"
    second = tmp_path / "main" / "11" / "____2.txt"
    assert first.read_bytes() == b"AAA"
    assert second.read_bytes() == b"BBB"
    assert str(first) in footer
    assert str(second) in footer
