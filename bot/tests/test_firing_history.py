import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from claude_assistant.firing_history import FiringHistory, FIRING_HISTORY_RETENTION_DAYS


@pytest.fixture
def history(tmp_path: Path) -> FiringHistory:
    return FiringHistory(tmp_path / "firing_history.json")


def test_all_empty(history: FiringHistory) -> None:
    assert history.all() == []


def test_record_appends_entry(history: FiringHistory) -> None:
    history.record(
        agent="main",
        schedule_id="abc123",
        cron="0 9 * * *",
        prompt="morning check",
        one_shot=False,
    )
    entries = history.all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["agent"] == "main"
    assert entry["schedule_id"] == "abc123"
    assert entry["cron"] == "0 9 * * *"
    assert entry["prompt"] == "morning check"
    assert entry["one_shot"] is False
    assert "fired_at" in entry
    # fired_at should be parseable as ISO datetime
    datetime.fromisoformat(entry["fired_at"])


def test_record_multiple(history: FiringHistory) -> None:
    history.record("main", "aaa", "0 9 * * *", "first", False)
    history.record("journalist", "bbb", "0 10 * * *", "second", True)
    assert len(history.all()) == 2


def test_recent_filters_by_days(history: FiringHistory) -> None:
    history.record("main", "aaa", "0 9 * * *", "recent", False)
    # Manually inject an old entry
    old_time = (datetime.now() - timedelta(days=10)).isoformat()
    history._data["firings"].append({
        "agent": "main",
        "schedule_id": "old",
        "cron": "0 9 * * *",
        "prompt": "old entry",
        "one_shot": False,
        "fired_at": old_time,
    })
    assert len(history.all()) == 2
    recent = history.recent(days=7)
    assert len(recent) == 1
    assert recent[0]["schedule_id"] == "aaa"


def test_record_prunes_old_entries(tmp_path: Path) -> None:
    history = FiringHistory(tmp_path / "firing_history.json", retention_days=5)
    # Inject an entry that's 10 days old
    old_time = (datetime.now() - timedelta(days=10)).isoformat()
    history._data["firings"].append({
        "agent": "main",
        "schedule_id": "old",
        "cron": "0 9 * * *",
        "prompt": "stale",
        "one_shot": False,
        "fired_at": old_time,
    })
    # Recording a new entry should prune the old one
    history.record("main", "new", "0 10 * * *", "fresh", False)
    entries = history.all()
    assert len(entries) == 1
    assert entries[0]["schedule_id"] == "new"


def test_persistence(tmp_path: Path) -> None:
    path = tmp_path / "firing_history.json"
    h1 = FiringHistory(path)
    h1.record("main", "aaa", "0 9 * * *", "test", False)
    h2 = FiringHistory(path)
    assert len(h2.all()) == 1


def test_missing_file_treated_as_empty(tmp_path: Path) -> None:
    history = FiringHistory(tmp_path / "nonexistent" / "history.json")
    assert history.all() == []


def test_default_retention_days() -> None:
    assert FIRING_HISTORY_RETENTION_DAYS == 30


def test_corrupt_file_treated_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "firing_history.json"
    path.write_text("not valid json {{{")
    history = FiringHistory(path)
    assert history.all() == []


def test_wrong_structure_treated_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "firing_history.json"
    path.write_text('{"firings": "not a list"}')
    history = FiringHistory(path)
    assert history.all() == []
