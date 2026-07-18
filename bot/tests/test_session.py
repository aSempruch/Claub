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


def test_migrates_legacy_string_values(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text('{"main": "uuid-legacy"}')
    store = SessionStore(path)
    assert store.get("main") == "uuid-legacy"
    assert store.get_model("main") is None


def test_get_model_nonexistent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    assert store.get_model("main") is None


def test_set_model_before_any_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set_model("main", "opus")
    assert store.get_model("main") == "opus"
    assert store.get("main") is None


def test_set_preserves_model(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set_model("main", "opus")
    store.set("main", "uuid-123")
    assert store.get("main") == "uuid-123"
    assert store.get_model("main") == "opus"


def test_set_model_preserves_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set("main", "uuid-123")
    store.set_model("main", "opus")
    assert store.get("main") == "uuid-123"
    assert store.get_model("main") == "opus"


def test_delete_removes_session_and_model(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set("main", "uuid-123")
    store.set_model("main", "opus")
    store.delete("main")
    assert store.get("main") is None
    assert store.get_model("main") is None


def test_clear_model_keeps_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set("main", "uuid-123")
    store.set_model("main", "opus")
    store.clear_model("main")
    assert store.get_model("main") is None
    assert store.get("main") == "uuid-123"


def test_clear_model_nonexistent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.clear_model("main")  # should not raise


def test_model_persistence_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store1 = SessionStore(path)
    store1.set("main", "uuid-123")
    store1.set_model("main", "opus")

    store2 = SessionStore(path)
    assert store2.get("main") == "uuid-123"
    assert store2.get_model("main") == "opus"
