"""Tests for the monitor's poll loop, driven by an injected client and clock."""

import datetime as dt
import json

import httpx
import pytest

from leetcode_api import AuthError
from monitor import (
    AUTH_GRACE,
    IDLE_TIMEOUT,
    MAX_NETWORK_FAILURES,
    run_poll_loop,
)
from session_store import create_session, read_manifest


class FakeClock:
    """Virtual time: sleeping advances the clock instead of blocking."""

    def __init__(self, start: float = 1_000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeClient:
    """Scripted client. Callables take the zero-based call index.

    Returning an Exception instance raises it, so failure modes are scripted
    the same way as successes.
    """

    def __init__(self, synced, submissions=None):
        self._synced = synced
        self._submissions = submissions or (lambda i: [])
        self.synced_calls = 0
        self.submission_calls = 0

    async def synced_code(self, question_id, lang_id, slug=""):
        value = self._synced(self.synced_calls)
        self.synced_calls += 1
        if isinstance(value, Exception):
            raise value
        return value

    async def submissions(self, problem):
        value = self._submissions(self.submission_calls)
        self.submission_calls += 1
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def session(tmp_path):
    return create_session(
        tmp_path, problem="two-sum", question_id=1, language="python3",
        baseline_submission_ts=None, when=dt.datetime(2026, 7, 28, 19, 5),
    )


def events(session_dir, kind=None):
    path = session_dir / "events.jsonl"
    if not path.exists():
        return []
    out = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return [e for e in out if kind is None or e["type"] == kind]


async def _run(session, client, clock, **kw):
    return await run_poll_loop(
        client=client, session_dir=session, problem="two-sum",
        question_id=1, lang_id=11,
        baseline_submission_ts=kw.pop("baseline_submission_ts", None),
        baseline_synced_ts=kw.pop("baseline_synced_ts", None),
        baseline_code=kw.pop("baseline_code", ""),
        clock=clock.now, sleep=clock.sleep, **kw,
    )


# ---- change detection ----

@pytest.mark.asyncio
async def test_changed_timestamp_writes_snapshot_and_event(session):
    client = FakeClient(lambda i: {"timestamp": 100, "code": "a\nb\n"})

    reason = await _run(session, client, FakeClock())

    changes = events(session, "code_change")
    assert len(changes) == 1
    assert changes[0]["lines_added"] == 2
    assert changes[0]["lines_removed"] == 0
    assert changes[0]["code_len"] == 4
    assert changes[0]["snapshot_ref"] == "snapshots/save-0001.py"
    assert (session / "snapshots" / "save-0001.py").read_text() == "a\nb\n"
    assert reason == "idle"


@pytest.mark.asyncio
async def test_unchanged_timestamp_writes_nothing(session):
    """Dedupe is on timestamp: the poll interval bounds resolution, not writes."""
    client = FakeClient(lambda i: {"timestamp": 100, "code": "a\n"})

    await _run(session, client, FakeClock(), baseline_synced_ts=100, baseline_code="a\n")

    assert events(session, "code_change") == []
    assert list((session / "snapshots").iterdir()) == []


@pytest.mark.asyncio
async def test_identical_code_under_a_new_timestamp_writes_nothing(session):
    """LeetCode re-syncs the buffer and bumps the timestamp without the code
    changing. Recording that costs an event plus a full snapshot file and
    carries no information — five such saves appeared in a 19-minute session."""
    client = FakeClient(lambda i: {"timestamp": 100 + i, "code": "a\n"})

    await _run(session, client, FakeClock(), baseline_synced_ts=100, baseline_code="a\n")

    assert events(session, "code_change") == []
    assert list((session / "snapshots").iterdir()) == []


@pytest.mark.asyncio
async def test_a_timestamp_bump_does_not_keep_the_session_alive(session):
    """The idle timer tracks work, not sync traffic: if only the timestamp is
    moving, the solver has stopped and the session must still time out."""
    client = FakeClient(lambda i: {"timestamp": 100 + i, "code": "a\n"})

    reason = await _run(session, client, FakeClock(), baseline_synced_ts=100,
                        baseline_code="a\n")

    assert reason == "idle"


@pytest.mark.asyncio
async def test_a_real_edit_after_a_timestamp_bump_is_still_recorded(session):
    """The skip must not desynchronise change detection."""
    client = FakeClient(
        lambda i: {"timestamp": 100 + i, "code": "a\n" if i < 3 else "a\nb\n"}
    )

    await _run(session, client, FakeClock(), baseline_synced_ts=100, baseline_code="a\n")

    changes = events(session, "code_change")
    assert len(changes) == 1
    assert changes[0]["lines_added"] == 1
    assert (session / "snapshots" / "save-0001.py").read_text() == "a\nb\n"


@pytest.mark.asyncio
async def test_timestamp_type_wobble_is_not_treated_as_an_edit(session):
    """Verified against the live API: syncedCode.timestamp comes back as int
    while submission timestamps come back as str. Normalise, so a type change
    can never masquerade as an edit and spam snapshots."""
    client = FakeClient(lambda i: {"timestamp": "100" if i % 2 else 100, "code": "a\n"})

    await _run(session, client, FakeClock(), baseline_synced_ts=100, baseline_code="a\n")

    assert events(session, "code_change") == []


@pytest.mark.asyncio
async def test_snapshots_are_sequentially_numbered(session):
    client = FakeClient(lambda i: {"timestamp": 100 + i, "code": "x\n" * (i + 1)})

    await _run(session, client, FakeClock(), max_duration=60)

    refs = [e["snapshot_ref"] for e in events(session, "code_change")]
    assert refs[:3] == [
        "snapshots/save-0001.py",
        "snapshots/save-0002.py",
        "snapshots/save-0003.py",
    ]


@pytest.mark.asyncio
async def test_missing_cloud_save_is_not_a_change(session):
    """Starting before any code exists must not fabricate an event."""
    client = FakeClient(lambda i: None)

    reason = await _run(session, client, FakeClock())

    assert events(session, "code_change") == []
    assert reason == "idle"


# ---- idle events (pauses are signal, not noise) ----

@pytest.mark.asyncio
async def test_long_pause_before_a_change_emits_an_idle_event(session):
    # No change for a long stretch, then one.
    client = FakeClient(lambda i: None if i < 20 else {"timestamp": 500, "code": "z\n"})

    await _run(session, client, FakeClock())

    idles = events(session, "idle")
    assert len(idles) == 1
    assert idles[0]["duration_sec"] >= 60


@pytest.mark.asyncio
async def test_rapid_changes_emit_no_idle_events(session):
    client = FakeClient(lambda i: {"timestamp": 100 + i, "code": "x\n" * (i + 1)})

    await _run(session, client, FakeClock(), max_duration=60)

    assert events(session, "idle") == []


# ---- stop conditions ----

@pytest.mark.asyncio
async def test_stops_idle_after_the_timeout(session):
    clock = FakeClock()
    client = FakeClient(lambda i: {"timestamp": 100, "code": "a\n"})

    reason = await _run(session, client, clock, baseline_synced_ts=100, baseline_code="a\n")

    assert reason == "idle"
    assert clock.now() - 1000.0 >= IDLE_TIMEOUT


@pytest.mark.asyncio
async def test_stops_at_max_duration_even_while_still_changing(session):
    client = FakeClient(lambda i: {"timestamp": 100 + i, "code": "x\n" * (i + 1)})

    reason = await _run(session, client, FakeClock(), max_duration=120)

    assert reason == "max_duration"


@pytest.mark.asyncio
async def test_writes_monitor_end_and_finalizes_manifest(session):
    client = FakeClient(lambda i: {"timestamp": 100, "code": "a\n"})

    reason = await _run(session, client, FakeClock())

    end = events(session, "monitor_end")
    assert len(end) == 1
    assert end[0]["stop_reason"] == reason
    manifest = read_manifest(session)
    assert manifest["stop_reason"] == reason
    assert manifest["ended_at"] is not None


# ---- submissions are timeline events, never control flow ----

@pytest.mark.asyncio
async def test_records_new_submissions_of_every_status(session):
    subs = [
        {"id": "1", "timestamp": "50", "statusDisplay": "Accepted", "lang": "python3"},
        {"id": "2", "timestamp": "150", "statusDisplay": "Wrong Answer", "lang": "python3"},
        {"id": "3", "timestamp": "160", "statusDisplay": "Time Limit Exceeded", "lang": "python3"},
    ]
    client = FakeClient(lambda i: None, submissions=lambda i: subs)

    await _run(session, client, FakeClock(), baseline_submission_ts=100)

    recorded = events(session, "submission")
    assert [e["status"] for e in recorded] == ["Wrong Answer", "Time Limit Exceeded"]


@pytest.mark.asyncio
async def test_accepted_submission_does_not_stop_monitoring(session):
    """Measured ~7min of post-accept editing; stopping on accept would lose it."""
    subs = [{"id": "9", "timestamp": "150", "statusDisplay": "Accepted", "lang": "python3"}]
    client = FakeClient(lambda i: None, submissions=lambda i: subs)

    reason = await _run(session, client, FakeClock(), baseline_submission_ts=100)

    assert reason == "idle"
    assert len(events(session, "submission")) == 1


@pytest.mark.asyncio
async def test_submission_is_recorded_once_not_on_every_poll(session):
    subs = [{"id": "9", "timestamp": "150", "statusDisplay": "Accepted", "lang": "python3"}]
    client = FakeClient(lambda i: None, submissions=lambda i: subs)

    await _run(session, client, FakeClock(), baseline_submission_ts=100)

    assert len(events(session, "submission")) == 1


# ---- error handling ----

@pytest.mark.asyncio
async def test_auth_failure_is_recorded_then_recovered_from(session):
    """A refreshed token mid-session must resume without ending the session."""
    def synced(i):
        if i < 3:
            return AuthError("expired")
        return {"timestamp": 200, "code": "recovered\n"}

    reason = await _run(session, FakeClient(synced), FakeClock())

    assert events(session, "error")
    assert len(events(session, "code_change")) == 1
    assert reason == "idle"


@pytest.mark.asyncio
async def test_sustained_auth_failure_stops_with_auth_expired(session):
    clock = FakeClock()
    client = FakeClient(lambda i: AuthError("expired"))

    reason = await _run(session, client, clock)

    assert reason == "auth_expired"
    assert clock.now() - 1000.0 >= AUTH_GRACE


@pytest.mark.asyncio
async def test_consecutive_network_failures_stop_the_loop(session):
    client = FakeClient(lambda i: httpx.ConnectError("boom"))

    reason = await _run(session, client, FakeClock())

    assert reason == "network"
    assert client.synced_calls == MAX_NETWORK_FAILURES


@pytest.mark.asyncio
async def test_intermittent_network_failures_do_not_stop_the_loop(session):
    """Only *consecutive* failures should end a session."""
    def synced(i):
        if i % 2 == 0:
            return httpx.ConnectError("flaky")
        return {"timestamp": 100, "code": "a\n"}

    reason = await _run(session, FakeClient(synced), FakeClock(),
                        baseline_synced_ts=100, baseline_code="a\n")

    assert reason == "idle"


@pytest.mark.asyncio
async def test_submission_poll_failure_does_not_kill_the_session(session):
    """Submissions are secondary; losing them must not lose the code timeline."""
    client = FakeClient(
        lambda i: {"timestamp": 100, "code": "a\n"},
        submissions=lambda i: httpx.ConnectError("boom"),
    )

    reason = await _run(session, client, FakeClock(),
                        baseline_synced_ts=100, baseline_code="a\n")

    assert reason == "idle"
