import pytest
from pathlib import Path
from claude_assistant.session import SessionStore


def test_get_nonexistent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    assert store.get("main") is None


def test_set_and_get(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set("main", "uuid-123")
    assert store.get("main") == "uuid-123"


def test_persistence_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store1 = SessionStore(path)
    store1.set("main", "uuid-123")

    store2 = SessionStore(path)
    assert store2.get("main") == "uuid-123"


def test_delete(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set("main", "uuid-123")
    store.delete("main")
    assert store.get("main") is None


def test_delete_nonexistent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.delete("main")  # should not raise
