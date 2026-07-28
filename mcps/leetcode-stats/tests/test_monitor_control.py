"""Tests for the start/stop/results orchestration behind the MCP tools."""

import datetime as dt
import json
import os
import signal

import pytest

from monitor_control import results, sessions_root, start, stop
from session_store import acquire_lock, create_session, read_manifest


class FakeClient:
    def __init__(self, question_id=1, synced=None, submissions=None, resolve_error=None):
        self._question_id = question_id
        self._synced = synced
        self._submissions = submissions or []
        self._resolve_error = resolve_error

    async def resolve_question_id(self, slug):
        if self._resolve_error:
            raise self._resolve_error
        return self._question_id

    async def synced_code(self, question_id, lang_id, slug=""):
        return self._synced

    async def submissions(self, problem):
        return self._submissions


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "monitor.lock"


@pytest.fixture
def root(tmp_path):
    return tmp_path / "leetcode-sessions"


def _spawn_recorder():
    calls = []

    def spawn(**kwargs):
        calls.append(kwargs)
        return 4242

    return spawn, calls


# ---- sessions_root resolution ----

def test_sessions_root_defaults_to_the_agent_workspace(monkeypatch):
    monkeypatch.delenv("LEETCODE_SESSIONS_DIR", raising=False)
    monkeypatch.setenv("CLAUB_AGENT_NAME", "leetcode-coach")

    assert str(sessions_root()) == "/claub/workspaces/leetcode-coach/leetcode-sessions"


def test_sessions_root_env_override_wins(monkeypatch):
    monkeypatch.setenv("LEETCODE_SESSIONS_DIR", "/somewhere/else")

    assert str(sessions_root()) == "/somewhere/else"


# ---- start ----

@pytest.mark.asyncio
async def test_start_creates_session_and_spawns_monitor(root, lock_path):
    spawn, calls = _spawn_recorder()
    client = FakeClient(question_id=146, synced={"timestamp": 900, "code": "a\n"})

    out = await start("lru-cache", "python3", root=root, client=client, spawn=spawn,
                      lock_path=lock_path, now=dt.datetime(2026, 7, 28, 19, 5))

    assert "lru-cache" in out
    session_dir = root / "2026-07-28-1905-lru-cache"
    assert session_dir.is_dir()
    manifest = read_manifest(session_dir)
    assert manifest["question_id"] == 146
    assert manifest["language"] == "python3"
    assert len(calls) == 1
    assert calls[0]["session_dir"] == session_dir
    assert calls[0]["baseline_synced_ts"] == 900


@pytest.mark.asyncio
async def test_start_is_refused_while_a_monitor_holds_the_lock(root, lock_path, tmp_path):
    held = acquire_lock(lock_path)
    assert held is not None
    proc_root = tmp_path / "proc"
    (proc_root / "77").mkdir(parents=True)
    (proc_root / "77" / "cmdline").write_bytes(
        b"claub-leetcode-monitor\0monitor.py\0--session=S\0--problem=two-sum\0"
    )
    spawn, calls = _spawn_recorder()

    out = await start("lru-cache", "python3", root=root, client=FakeClient(),
                      spawn=spawn, lock_path=lock_path, proc_root=proc_root,
                      now=dt.datetime(2026, 7, 28, 19, 5))

    os.close(held)
    assert "two-sum" in out          # names what is actually running
    assert "stop_monitoring" in out  # tells the agent how to proceed
    assert calls == []               # and spawns nothing


@pytest.mark.asyncio
async def test_start_fails_fast_on_an_unknown_slug(root, lock_path):
    spawn, calls = _spawn_recorder()
    client = FakeClient(resolve_error=ValueError("Problem 'nope' not found"))

    out = await start("nope", "python3", root=root, client=client, spawn=spawn,
                      lock_path=lock_path, now=dt.datetime(2026, 7, 28, 19, 5))

    assert "not found" in out
    assert calls == []
    assert not root.exists() or list(root.iterdir()) == []


@pytest.mark.asyncio
async def test_start_rejects_an_invalid_language(root, lock_path):
    spawn, calls = _spawn_recorder()

    out = await start("two-sum", "brainfuck", root=root, client=FakeClient(),
                      spawn=spawn, lock_path=lock_path,
                      now=dt.datetime(2026, 7, 28, 19, 5))

    assert "Invalid language" in out
    assert calls == []


@pytest.mark.asyncio
async def test_start_notes_when_there_is_no_cloud_code_yet(root, lock_path):
    """Not an error -- starting before writing anything is the normal case."""
    spawn, calls = _spawn_recorder()
    client = FakeClient(synced=None)

    out = await start("two-sum", "python3", root=root, client=client, spawn=spawn,
                      lock_path=lock_path, now=dt.datetime(2026, 7, 28, 19, 5))

    assert len(calls) == 1
    assert "no cloud-saved code" in out.lower()
    assert calls[0]["baseline_synced_ts"] is None


@pytest.mark.asyncio
async def test_start_reconciles_interrupted_sessions_first(root, lock_path):
    orphan = create_session(root, problem="old", question_id=1, language="python3",
                            baseline_submission_ts=None,
                            when=dt.datetime(2026, 7, 27, 10, 0))
    spawn, _ = _spawn_recorder()

    await start("two-sum", "python3", root=root, client=FakeClient(), spawn=spawn,
                lock_path=lock_path, now=dt.datetime(2026, 7, 28, 19, 5))

    assert read_manifest(orphan)["stop_reason"] == "interrupted"


@pytest.mark.asyncio
async def test_start_prunes_old_sessions(root, lock_path):
    for day in range(1, 5):
        d = create_session(root, problem=f"p{day}", question_id=1, language="python3",
                           baseline_submission_ts=None,
                           when=dt.datetime(2026, 7, day, 10, 0))
        (d / "manifest.json").write_text(json.dumps({
            "session_id": d.name, "started_at": "x", "ended_at": "y",
            "stop_reason": "stopped",
        }))
    spawn, _ = _spawn_recorder()

    await start("two-sum", "python3", root=root, client=FakeClient(), spawn=spawn,
                lock_path=lock_path, keep=2, now=dt.datetime(2026, 7, 28, 19, 5))

    names = sorted(p.name for p in root.iterdir())
    assert "2026-07-01-1000-p1" not in names
    assert "2026-07-04-1000-p4" in names
    assert "2026-07-28-1905-two-sum" in names


@pytest.mark.asyncio
async def test_start_records_the_submission_baseline(root, lock_path):
    spawn, calls = _spawn_recorder()
    client = FakeClient(submissions=[
        {"id": "1", "timestamp": "100"}, {"id": "2", "timestamp": "300"},
    ])

    await start("two-sum", "python3", root=root, client=client, spawn=spawn,
                lock_path=lock_path, now=dt.datetime(2026, 7, 28, 19, 5))

    assert calls[0]["baseline_submission_ts"] == 300


# ---- stop ----

def test_stop_reports_when_nothing_is_running(root, tmp_path):
    out = stop(root=root, proc_root=tmp_path / "empty-proc")
    assert "not monitoring" in out.lower()


def test_stop_signals_the_process_group_and_finalizes(root, tmp_path):
    session = create_session(root, problem="two-sum", question_id=1, language="python3",
                             baseline_submission_ts=None,
                             when=dt.datetime(2026, 7, 28, 19, 5))
    proc_root = tmp_path / "proc"
    (proc_root / "77").mkdir(parents=True)
    (proc_root / "77" / "cmdline").write_bytes(
        f"claub-leetcode-monitor\0monitor.py\0--session={session.name}\0--problem=two-sum\0".encode()
    )
    signals = []

    def fake_killpg(pgid, sig):
        signals.append((pgid, sig))

    out = stop(root=root, proc_root=proc_root, killpg=fake_killpg,
               is_alive=lambda pid: False)

    assert signals == [(77, signal.SIGTERM)]
    assert read_manifest(session)["stop_reason"] == "stopped"
    assert "two-sum" in out


def test_stop_escalates_to_sigkill_when_the_monitor_will_not_die(root, tmp_path):
    create_session(root, problem="two-sum", question_id=1, language="python3",
                   baseline_submission_ts=None, when=dt.datetime(2026, 7, 28, 19, 5))
    proc_root = tmp_path / "proc"
    (proc_root / "77").mkdir(parents=True)
    (proc_root / "77" / "cmdline").write_bytes(
        b"claub-leetcode-monitor\0monitor.py\0--session=2026-07-28-1905-two-sum\0--problem=two-sum\0"
    )
    signals = []

    stop(root=root, proc_root=proc_root,
         killpg=lambda pgid, sig: signals.append(sig),
         is_alive=lambda pid: True, grace=0.01, poll=0.005)

    assert signal.SIGTERM in signals
    assert signal.SIGKILL in signals


# ---- results ----

def test_results_reports_when_there_is_nothing_recorded(root):
    assert "no monitoring sessions" in results(root=root).lower()


def test_results_renders_the_latest_session(root):
    create_session(root, problem="two-sum", question_id=1, language="python3",
                   baseline_submission_ts=None, when=dt.datetime(2026, 7, 28, 19, 5))

    out = results(root=root)

    assert "two-sum" in out
    assert "0 changes" in out


def test_results_can_select_by_problem(root):
    create_session(root, problem="two-sum", question_id=1, language="python3",
                   baseline_submission_ts=None, when=dt.datetime(2026, 7, 26, 10, 0))
    create_session(root, problem="lru-cache", question_id=2, language="python3",
                   baseline_submission_ts=None, when=dt.datetime(2026, 7, 28, 19, 5))

    assert "two-sum" in results(root=root, problem="two-sum")


def test_results_reports_when_that_problem_has_no_session(root):
    create_session(root, problem="two-sum", question_id=1, language="python3",
                   baseline_submission_ts=None, when=dt.datetime(2026, 7, 26, 10, 0))

    assert "no monitoring session" in results(root=root, problem="never-done").lower()


# ---- spawn command construction ----

def test_spawn_command_tags_argv0_and_passes_identity(tmp_path):
    from monitor_control import build_spawn_command

    argv, env = build_spawn_command(
        session_dir=tmp_path / "2026-07-28-1905-two-sum", lock_fd=7,
        problem="two-sum", question_id=1, lang_id=11, language="python3",
        baseline_synced_ts=900, baseline_submission_ts=100,
    )

    assert argv[0] == "claub-leetcode-monitor"
    assert argv[1].endswith("monitor.py")
    assert "--problem=two-sum" in argv
    assert "--session=2026-07-28-1905-two-sum" in argv
    assert "--lock-fd=7" in argv
    assert "--baseline-synced-ts=900" in argv
    assert "--baseline-submission-ts=100" in argv


def test_spawn_command_omits_absent_baselines(tmp_path):
    from monitor_control import build_spawn_command

    argv, _ = build_spawn_command(
        session_dir=tmp_path / "s", lock_fd=7, problem="two-sum",
        question_id=1, lang_id=11, language="python3",
        baseline_synced_ts=None, baseline_submission_ts=None,
    )

    assert not [a for a in argv if a.startswith("--baseline-")]


def test_spawn_command_carries_pythonpath_for_the_mangled_argv0(tmp_path):
    """Regression: tagging argv[0] with a non-path string defeats CPython's
    venv detection, so the child resolved to the system interpreter and lost
    every third-party import. The child must be told sys.path explicitly."""
    import pathlib

    import httpx

    from monitor_control import build_spawn_command

    _, env = build_spawn_command(
        session_dir=tmp_path / "s", lock_fd=7, problem="two-sum",
        question_id=1, lang_id=11, language="python3",
        baseline_synced_ts=None, baseline_submission_ts=None,
    )

    entries = env["PYTHONPATH"].split(os.pathsep)
    site_dir = str(pathlib.Path(httpx.__file__).resolve().parent.parent)
    assert site_dir in entries, f"{site_dir} missing from {entries}"
    assert str(pathlib.Path(__file__).resolve().parent.parent) in entries
