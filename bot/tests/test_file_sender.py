from __future__ import annotations

import tempfile
from pathlib import Path

from claude_assistant.file_sender import extract_files


def test_no_markers():
    text, files = extract_files("Hello world")
    assert text == "Hello world"
    assert files == []


def test_single_file_marker(tmp_path: Path):
    f = tmp_path / "test.txt"
    f.write_text("hello")
    text, files = extract_files(f"Here is the file [FILE:{f}] enjoy!")
    assert f"[FILE:{f}]" not in text
    assert "Here is the file" in text
    assert "enjoy!" in text
    assert len(files) == 1
    assert files[0].filename == "test.txt"


def test_multiple_file_markers(tmp_path: Path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.png"
    f1.write_text("a")
    f2.write_bytes(b"\x89PNG")
    text, files = extract_files(f"Files: [FILE:{f1}] and [FILE:{f2}]")
    assert len(files) == 2


def test_missing_file():
    text, files = extract_files("[FILE:/nonexistent/path.txt]")
    assert files == []
    assert text == ""


def test_files_only_no_text(tmp_path: Path):
    f = tmp_path / "img.png"
    f.write_bytes(b"\x89PNG")
    text, files = extract_files(f"[FILE:{f}]")
    assert text == ""
    assert len(files) == 1
