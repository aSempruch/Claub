"""Rendering a solve as edit runs rather than one row per autosave.

These run against real recorded sessions in ``fixtures/sessions/``, because the
behaviour worth protecting is a property of real LeetCode autosave data: saves
arrive every 5-10s regardless of what the solver is doing, so the render has to
recover intent from a uniform sampler. Synthetic events can't reproduce that.

The fixtures have had byte-identical saves stripped, so they look like sessions
recorded after the dedup gate landed. Legacy duplicate handling is covered by a
synthetic test in ``test_report.py``.
"""

import shutil
from pathlib import Path

from report import render_timeline

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"

LRU = FIXTURES / "2026-07-28-1851-lru-cache"
TWITTER = FIXTURES / "2026-07-28-2033-design-twitter"
EMPTY = FIXTURES / "2026-07-28-1908-design-twitter"


def _run_headers(out: str) -> list[str]:
    return [ln.strip() for ln in out.splitlines()
            if "WROTE" in ln or "DELETED" in ln]


# ---- coalescing ----

def test_consecutive_growth_collapses_into_one_run():
    """29 saves of uninterrupted typing are one edit, not 29 events."""
    out = render_timeline(LRU)

    first = _run_headers(out)[0]
    assert "WROTE" in first
    assert "29 saves" in first


def test_run_count_is_far_below_save_count():
    """The point of the format: 70+ autosaves become a readable handful."""
    out = render_timeline(TWITTER)

    runs = _run_headers(out)
    assert 3 <= len(runs) < 20, f"got {len(runs)} runs"


def test_no_per_save_rows_remain():
    """No row should reference an individual snapshot file."""
    out = render_timeline(TWITTER)

    assert "save-0042" not in out


# ---- the behaviour this format exists for ----

def test_abandoned_approach_appears_as_removed_code():
    """The solver wrote a flatten-then-sort merge and deleted it before ever
    submitting. Submission- or pause-anchored formats bury this; it is the
    single most coaching-relevant moment in the session."""
    out = render_timeline(TWITTER)

    assert "-        tweets = [*self.tweets[userId]" in out


def test_deletions_are_labelled_separately_from_writing():
    out = render_timeline(TWITTER)

    assert any("DELETED" in h for h in _run_headers(out))
    assert any("WROTE" in h for h in _run_headers(out))


def test_deletion_run_reports_a_negative_char_delta():
    out = render_timeline(TWITTER)

    deletions = [h for h in _run_headers(out) if "DELETED" in h]
    assert deletions
    assert all("-" in h.split("DELETED")[1] for h in deletions)


def test_multi_save_deletion_coalesces():
    """A block removed over several 5s saves is one deletion, not four."""
    out = render_timeline(TWITTER)

    multi = [h for h in _run_headers(out) if "DELETED" in h and "1 save]" not in h]
    assert multi, "expected at least one deletion spanning multiple saves"


# ---- ordering and non-code events ----

def test_submissions_appear_in_order_between_runs():
    out = render_timeline(TWITTER)

    verdicts = [ln for ln in out.splitlines() if "SUBMIT" in ln]
    assert len(verdicts) == 4
    assert "Runtime Error" in verdicts[0]
    assert "Wrong Answer" in verdicts[1]
    assert "Accepted" in verdicts[2]


def test_a_submission_ends_the_run_it_interrupts():
    """Edits before and after a verdict are different work and must not merge."""
    out = render_timeline(TWITTER)

    lines = out.splitlines()
    submit = next(i for i, ln in enumerate(lines) if "Runtime Error" in ln)
    after = [ln for ln in lines[submit + 1:] if "WROTE" in ln or "DELETED" in ln]
    assert after, "expected fresh runs after the first verdict"


def test_long_pauses_are_preserved():
    out = render_timeline(LRU)

    assert "paused" in out


# ---- degenerate sessions ----

def test_session_with_no_code_changes_renders():
    out = render_timeline(EMPTY)

    assert "design-twitter" in out
    assert not _run_headers(out)


def test_missing_snapshot_file_does_not_crash(tmp_path):
    """Old sessions get pruned; a dangling snapshot_ref must degrade, not raise."""
    d = tmp_path / TWITTER.name
    shutil.copytree(TWITTER, d)
    for snap in (d / "snapshots").glob("save-000[1-9].py"):
        snap.unlink()

    out = render_timeline(d)

    assert "design-twitter" in out
