"""Tests for monitor liveness, discovery, and session directory bookkeeping."""

import datetime as dt
import json
import os

from session_store import (
    MONITOR_TAG,
    acquire_lock,
    append_event,
    create_session,
    finalize_session,
    find_monitor,
    prune_sessions,
    read_manifest,
    reconcile_interrupted,
    session_dir_name,
)


def _fake_proc(root, pid: int, argv: list[str]):
    """Build a fake /proc/<pid>/cmdline entry."""
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


# ---- flock: the liveness authority ----

def test_lock_is_acquired_when_free(tmp_path):
    fd = acquire_lock(tmp_path / "m.lock")
    assert fd is not None
    os.close(fd)


def test_second_acquire_is_refused_while_held(tmp_path):
    path = tmp_path / "m.lock"
    first = acquire_lock(path)
    assert first is not None

    assert acquire_lock(path) is None

    os.close(first)


def test_lock_is_free_again_once_holder_releases(tmp_path):
    """Closing the fd is what process death does implicitly."""
    path = tmp_path / "m.lock"
    first = acquire_lock(path)
    os.close(first)

    second = acquire_lock(path)
    assert second is not None
    os.close(second)


def test_lock_file_left_on_disk_carries_no_stale_state(tmp_path):
    path = tmp_path / "m.lock"
    os.close(acquire_lock(path))
    assert path.exists()  # file persists...

    fd = acquire_lock(path)  # ...but grants no lock
    assert fd is not None
    os.close(fd)


# ---- tag scan: identity ----

def test_find_monitor_locates_tagged_process(tmp_path):
    _fake_proc(tmp_path, 42, [MONITOR_TAG, "monitor.py", "--session=S1", "--problem=two-sum"])

    found = find_monitor(proc_root=tmp_path)

    assert found is not None
    assert found["pid"] == 42
    assert found["session"] == "S1"
    assert found["problem"] == "two-sum"


def test_find_monitor_returns_none_when_absent(tmp_path):
    _fake_proc(tmp_path, 7, ["python3", "something_else.py"])
    assert find_monitor(proc_root=tmp_path) is None


def test_find_monitor_matches_argv0_exactly_not_substring(tmp_path):
    """Regression: a substring match hit the harness whose own cmdline
    contained the tag, and targeted the wrong PID entirely."""
    _fake_proc(tmp_path, 8, ["bash", "-c", f"echo {MONITOR_TAG} is the tag"])
    _fake_proc(tmp_path, 9, ["grep", MONITOR_TAG])

    assert find_monitor(proc_root=tmp_path) is None


def test_find_monitor_ignores_non_numeric_proc_entries(tmp_path):
    (tmp_path / "self").mkdir()
    (tmp_path / "meminfo").write_text("whatever")
    _fake_proc(tmp_path, 11, [MONITOR_TAG, "monitor.py", "--session=S2", "--problem=lru-cache"])

    found = find_monitor(proc_root=tmp_path)
    assert found is not None and found["session"] == "S2"


# ---- session directories ----

def test_session_dir_name_is_sortable_and_slugged():
    when = dt.datetime(2026, 7, 28, 19, 5)
    assert session_dir_name("two-sum", when) == "2026-07-28-1905-two-sum"


def test_create_session_writes_manifest(tmp_path):
    when = dt.datetime(2026, 7, 28, 19, 5)
    d = create_session(
        tmp_path, problem="two-sum", question_id=1, language="python3",
        baseline_submission_ts=1785000000, when=when,
    )

    m = read_manifest(d)
    assert m["problem"] == "two-sum"
    assert m["question_id"] == 1
    assert m["language"] == "python3"
    assert m["baseline_submission_ts"] == 1785000000
    assert m["ended_at"] is None
    assert m["stop_reason"] is None
    assert (d / "snapshots").is_dir()


def test_append_event_is_one_json_object_per_line(tmp_path):
    d = create_session(tmp_path, problem="p", question_id=1, language="python3",
                       baseline_submission_ts=None, when=dt.datetime(2026, 7, 28, 19, 5))
    append_event(d, {"type": "code_change", "code_len": 10})
    append_event(d, {"type": "submission", "status": "Wrong Answer"})

    lines = (d / "events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "code_change"
    assert json.loads(lines[1])["status"] == "Wrong Answer"


def test_append_event_stamps_ts_when_absent(tmp_path):
    d = create_session(tmp_path, problem="p", question_id=1, language="python3",
                       baseline_submission_ts=None, when=dt.datetime(2026, 7, 28, 19, 5))
    append_event(d, {"type": "idle"})

    ev = json.loads((d / "events.jsonl").read_text().strip())
    assert "ts" in ev


def test_finalize_session_stamps_reason_and_end(tmp_path):
    d = create_session(tmp_path, problem="p", question_id=1, language="python3",
                       baseline_submission_ts=None, when=dt.datetime(2026, 7, 28, 19, 5))

    finalize_session(d, "stopped")

    m = read_manifest(d)
    assert m["stop_reason"] == "stopped"
    assert m["ended_at"] is not None


def test_finalize_session_does_not_overwrite_an_existing_reason(tmp_path):
    """stop_monitoring finalizes defensively; the child may have got there first."""
    d = create_session(tmp_path, problem="p", question_id=1, language="python3",
                       baseline_submission_ts=None, when=dt.datetime(2026, 7, 28, 19, 5))
    finalize_session(d, "idle")

    finalize_session(d, "stopped")

    assert read_manifest(d)["stop_reason"] == "idle"


# ---- retention ----

def _finished_session(root, name):
    d = root / name
    (d / "snapshots").mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "session_id": name, "started_at": "2026-07-28T19:05:00",
        "ended_at": "2026-07-28T19:45:00", "stop_reason": "stopped",
    }))
    return d


def test_prune_keeps_the_n_most_recent(tmp_path):
    for name in ["2026-07-01-1000-a", "2026-07-02-1000-b",
                 "2026-07-03-1000-c", "2026-07-04-1000-d"]:
        _finished_session(tmp_path, name)

    prune_sessions(tmp_path, keep=2)

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "2026-07-03-1000-c", "2026-07-04-1000-d",
    ]


def test_prune_is_a_noop_below_the_limit(tmp_path):
    _finished_session(tmp_path, "2026-07-01-1000-a")
    prune_sessions(tmp_path, keep=30)
    assert len(list(tmp_path.iterdir())) == 1


def test_prune_tolerates_a_missing_root(tmp_path):
    prune_sessions(tmp_path / "not-there", keep=30)  # must not raise


def test_prune_ignores_stray_files(tmp_path):
    _finished_session(tmp_path, "2026-07-01-1000-a")
    (tmp_path / "README.md").write_text("not a session")

    prune_sessions(tmp_path, keep=1)

    assert (tmp_path / "README.md").exists()


# ---- reconciliation of interrupted sessions ----

def test_reconcile_stamps_unfinished_session_from_last_event(tmp_path):
    d = create_session(tmp_path, problem="p", question_id=1, language="python3",
                       baseline_submission_ts=None, when=dt.datetime(2026, 7, 28, 19, 5))
    append_event(d, {"type": "code_change", "ts": "2026-07-28T19:30:00"})
    append_event(d, {"type": "code_change", "ts": "2026-07-28T19:33:00"})

    reconciled = reconcile_interrupted(tmp_path)

    assert reconciled == [d.name]
    m = read_manifest(d)
    assert m["stop_reason"] == "interrupted"
    assert m["ended_at"] == "2026-07-28T19:33:00"


def test_reconcile_falls_back_to_started_at_when_no_events(tmp_path):
    d = create_session(tmp_path, problem="p", question_id=1, language="python3",
                       baseline_submission_ts=None, when=dt.datetime(2026, 7, 28, 19, 5))

    reconcile_interrupted(tmp_path)

    m = read_manifest(d)
    assert m["ended_at"] == m["started_at"]


def test_reconcile_leaves_finished_sessions_alone(tmp_path):
    _finished_session(tmp_path, "2026-07-01-1000-a")

    assert reconcile_interrupted(tmp_path) == []
    m = json.loads((tmp_path / "2026-07-01-1000-a" / "manifest.json").read_text())
    assert m["stop_reason"] == "stopped"
