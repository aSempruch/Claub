"""Tests for session selection and timeline rendering."""

import datetime as dt

import pytest

from report import find_latest_session, render_timeline
from session_store import append_event, create_session, finalize_session


def _session(root, problem, when, events=()):
    d = create_session(root, problem=problem, question_id=1, language="python3",
                       baseline_submission_ts=None, when=when)
    for ev in events:
        append_event(d, ev)
    return d


# ---- picking which session to report on ----

def test_find_latest_session_returns_most_recent(tmp_path):
    _session(tmp_path, "two-sum", dt.datetime(2026, 7, 26, 10, 0))
    newest = _session(tmp_path, "lru-cache", dt.datetime(2026, 7, 28, 19, 0))

    assert find_latest_session(tmp_path) == newest


def test_find_latest_session_filters_by_problem(tmp_path):
    wanted = _session(tmp_path, "two-sum", dt.datetime(2026, 7, 26, 10, 0))
    _session(tmp_path, "lru-cache", dt.datetime(2026, 7, 28, 19, 0))

    assert find_latest_session(tmp_path, problem="two-sum") == wanted


def test_find_latest_session_is_none_when_nothing_matches(tmp_path):
    _session(tmp_path, "two-sum", dt.datetime(2026, 7, 26, 10, 0))

    assert find_latest_session(tmp_path, problem="never-solved") is None
    assert find_latest_session(tmp_path / "missing") is None


def test_find_latest_session_does_not_match_on_slug_prefix(tmp_path):
    """'two-sum' must not match a session for 'two-sum-ii'."""
    _session(tmp_path, "two-sum-ii", dt.datetime(2026, 7, 28, 19, 0))

    assert find_latest_session(tmp_path, problem="two-sum") is None


# ---- rendering ----

def test_render_includes_problem_and_stop_reason(tmp_path):
    d = _session(tmp_path, "two-sum", dt.datetime(2026, 7, 28, 19, 0))
    finalize_session(d, "idle")

    out = render_timeline(d)

    assert "two-sum" in out
    assert "idle" in out
    assert str(d) in out


def test_render_shows_diff_counts_per_change(tmp_path):
    d = _session(tmp_path, "two-sum", dt.datetime(2026, 7, 28, 19, 0), events=[
        {"type": "code_change", "ts": "2026-07-28T19:01:00", "code_len": 40,
         "lines_added": 7, "lines_removed": 2, "snapshot_ref": "snapshots/save-0001.py"},
    ])

    out = render_timeline(d)

    assert "+7" in out and "-2" in out
    assert "save-0001.py" in out


def test_render_marks_submissions_with_status(tmp_path):
    d = _session(tmp_path, "two-sum", dt.datetime(2026, 7, 28, 19, 0), events=[
        {"type": "submission", "ts": "2026-07-28T19:10:00", "submission_id": "1",
         "status": "Wrong Answer", "lang": "python3"},
    ])

    out = render_timeline(d)

    assert "Wrong Answer" in out
    assert "SUBMIT" in out


def test_render_shows_idle_pauses(tmp_path):
    d = _session(tmp_path, "two-sum", dt.datetime(2026, 7, 28, 19, 0), events=[
        {"type": "idle", "ts": "2026-07-28T19:05:00", "duration_sec": 240},
    ])

    out = render_timeline(d)

    assert "4m" in out or "240" in out


def test_render_labels_a_still_running_session(tmp_path):
    """get_monitoring_results works on a live session, so say so."""
    d = _session(tmp_path, "two-sum", dt.datetime(2026, 7, 28, 19, 0), events=[
        {"type": "code_change", "ts": "2026-07-28T19:01:00", "code_len": 10,
         "lines_added": 1, "lines_removed": 0, "snapshot_ref": "snapshots/save-0001.py"},
    ])

    out = render_timeline(d)

    assert "still running" in out.lower()


def test_render_summarises_totals(tmp_path):
    d = _session(tmp_path, "two-sum", dt.datetime(2026, 7, 28, 19, 0), events=[
        {"type": "code_change", "ts": "2026-07-28T19:01:00", "code_len": 10,
         "lines_added": 1, "lines_removed": 0, "snapshot_ref": "snapshots/save-0001.py"},
        {"type": "code_change", "ts": "2026-07-28T19:02:00", "code_len": 20,
         "lines_added": 3, "lines_removed": 1, "snapshot_ref": "snapshots/save-0002.py"},
        {"type": "submission", "ts": "2026-07-28T19:10:00", "submission_id": "1",
         "status": "Accepted", "lang": "python3"},
    ])
    finalize_session(d, "stopped")

    out = render_timeline(d)

    assert "2 changes" in out
    assert "1 submission" in out


def test_render_handles_an_empty_session(tmp_path):
    d = _session(tmp_path, "two-sum", dt.datetime(2026, 7, 28, 19, 0))
    finalize_session(d, "idle")

    out = render_timeline(d)

    assert "0 changes" in out
