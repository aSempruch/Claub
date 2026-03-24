import json
import pytest
from pathlib import Path
from claude_assistant.schedule_store import ScheduleStore


@pytest.fixture
def store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.json")


def test_list_empty(store: ScheduleStore) -> None:
    assert store.list("main") == []


def test_create_schedule(store: ScheduleStore) -> None:
    entry = store.create("main", cron="0 9 * * *", prompt="morning check", one_shot=False)
    assert entry["cron"] == "0 9 * * *"
    assert entry["prompt"] == "morning check"
    assert entry["one_shot"] is False
    assert len(entry["id"]) == 6


def test_list_returns_created(store: ScheduleStore) -> None:
    store.create("main", cron="0 9 * * *", prompt="morning", one_shot=False)
    store.create("main", cron="0 17 * * *", prompt="evening", one_shot=True)
    entries = store.list("main")
    assert len(entries) == 2


def test_delete_schedule(store: ScheduleStore) -> None:
    entry = store.create("main", cron="0 9 * * *", prompt="morning", one_shot=False)
    assert store.delete("main", entry["id"]) is True
    assert store.list("main") == []


def test_delete_nonexistent(store: ScheduleStore) -> None:
    assert store.delete("main", "aaaaaa") is False


def test_delete_wrong_agent(store: ScheduleStore) -> None:
    entry = store.create("main", cron="0 9 * * *", prompt="test", one_shot=False)
    assert store.delete("journalist", entry["id"]) is False
    assert len(store.list("main")) == 1


def test_persistence(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    store1 = ScheduleStore(path)
    store1.create("main", cron="0 9 * * *", prompt="test", one_shot=False)
    store2 = ScheduleStore(path)
    assert len(store2.list("main")) == 1


def test_all_schedules(store: ScheduleStore) -> None:
    store.create("main", cron="0 9 * * *", prompt="m1", one_shot=False)
    store.create("journalist", cron="0 10 * * *", prompt="j1", one_shot=True)
    all_entries = store.all()
    assert "main" in all_entries
    assert "journalist" in all_entries
    assert len(all_entries["main"]) == 1
    assert len(all_entries["journalist"]) == 1


def test_id_uniqueness(store: ScheduleStore) -> None:
    ids = set()
    for i in range(50):
        entry = store.create("main", cron="0 9 * * *", prompt=f"test {i}", one_shot=False)
        ids.add(entry["id"])
    assert len(ids) == 50


def test_missing_file_treated_as_empty(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "nonexistent" / "schedules.json")
    assert store.all() == {}
