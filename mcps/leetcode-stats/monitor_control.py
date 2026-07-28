"""Orchestration behind the start/stop/results MCP tools.

Kept out of ``server.py`` so the spawn and signal seams stay injectable for
tests without those parameters leaking into the MCP tool schema.
"""

import datetime as dt
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from leetcode_api import LANG_IDS, LeetCodeClient, latest_submission_ts
from monitor import ext_for_language, normalize_ts
from report import find_latest_session, render_timeline
from session_store import (
    DEFAULT_KEEP,
    DEFAULT_LOCK_PATH,
    MONITOR_TAG,
    acquire_lock,
    create_session,
    finalize_session,
    find_monitor,
    prune_sessions,
    reconcile_interrupted,
)

MONITOR_SCRIPT = Path(__file__).with_name("monitor.py")


def sessions_root() -> Path:
    override = os.environ.get("LEETCODE_SESSIONS_DIR")
    if override:
        return Path(override)
    agent = os.environ.get("CLAUB_AGENT_NAME", "leetcode-coach")
    return Path(f"/claub/workspaces/{agent}/leetcode-sessions")


def build_spawn_command(
    *,
    session_dir,
    lock_fd: int,
    problem: str,
    question_id: int,
    lang_id: int,
    language: str,
    baseline_synced_ts: int | None,
    baseline_submission_ts: int | None,
) -> tuple[list[str], dict[str, str]]:
    """Build the monitor's argv and environment.

    ``argv[0]`` is the tag rather than the interpreter path, which is what
    makes the process self-describing in ``/proc`` and ``ps``. That has one
    sharp edge, found by running it for real: CPython derives ``sys.prefix``
    from ``argv[0]``, so a non-path value defeats virtualenv detection and the
    child falls back to the system interpreter — losing every third-party
    import, ``httpx`` included. Passing ``sys.path`` explicitly as
    ``PYTHONPATH`` restores the child's imports without giving up the tag.
    """
    session_dir = Path(session_dir)

    argv = [
        MONITOR_TAG,
        str(MONITOR_SCRIPT),
        f"--session={session_dir.name}",
        f"--problem={problem}",
        f"--session-dir={session_dir}",
        f"--question-id={question_id}",
        f"--lang-id={lang_id}",
        f"--language={language}",
        f"--lock-fd={lock_fd}",
    ]
    if baseline_synced_ts is not None:
        argv.append(f"--baseline-synced-ts={baseline_synced_ts}")
    if baseline_submission_ts is not None:
        argv.append(f"--baseline-submission-ts={baseline_submission_ts}")

    entries = [str(MONITOR_SCRIPT.parent)]
    entries += [p for p in sys.path if p]
    if os.environ.get("PYTHONPATH"):
        entries += os.environ["PYTHONPATH"].split(os.pathsep)

    seen, ordered = set(), []
    for entry in entries:
        resolved = str(Path(entry).resolve())
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(resolved)

    env = {**os.environ, "PYTHONPATH": os.pathsep.join(ordered)}
    return argv, env


def spawn_monitor(
    *,
    session_dir,
    lock_fd: int,
    problem: str,
    question_id: int,
    lang_id: int,
    language: str,
    baseline_synced_ts: int | None,
    baseline_submission_ts: int | None,
) -> int:
    """Launch the detached monitor, handing it the already-held lock.

    The lock fd is inherited rather than re-acquired: ``flock`` binds to the
    open file description, so passing the fd keeps the lock held continuously
    across the spawn. A test-then-release handshake would leave a window in
    which two concurrent starts could both proceed.
    """
    session_dir = Path(session_dir)
    os.set_inheritable(lock_fd, True)

    argv, env = build_spawn_command(
        session_dir=session_dir, lock_fd=lock_fd, problem=problem,
        question_id=question_id, lang_id=lang_id, language=language,
        baseline_synced_ts=baseline_synced_ts,
        baseline_submission_ts=baseline_submission_ts,
    )

    log = open(session_dir / "monitor.log", "a")
    proc = subprocess.Popen(
        argv,
        executable=sys.executable,
        env=env,
        preexec_fn=os.setsid,  # own process group, so stop can killpg
        pass_fds=(lock_fd,),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(MONITOR_SCRIPT.parent),
    )
    return proc.pid


async def start(
    problem: str,
    language: str = "python3",
    *,
    root=None,
    client=None,
    spawn=None,
    lock_path=DEFAULT_LOCK_PATH,
    proc_root="/proc",
    now: dt.datetime | None = None,
    keep: int = DEFAULT_KEEP,
) -> str:
    if language not in LANG_IDS:
        return f"Invalid language '{language}'. Valid options: {', '.join(sorted(LANG_IDS))}"

    root = Path(root) if root is not None else sessions_root()
    spawn = spawn or spawn_monitor
    now = now or dt.datetime.now()

    lock_fd = acquire_lock(lock_path)
    if lock_fd is None:
        active = find_monitor(proc_root=proc_root)
        if active and active.get("problem"):
            return (
                f"Already monitoring '{active['problem']}' "
                f"(session {active.get('session')}). Call stop_monitoring() first."
            )
        return "A monitor is already running. Call stop_monitoring() first."

    try:
        reconcile_interrupted(root)
        prune_sessions(root, keep=keep)

        owned = client is None
        if owned:
            client = await LeetCodeClient().__aenter__()
        try:
            question_id = await client.resolve_question_id(problem)
            lang_id = LANG_IDS[language]
            synced = await client.synced_code(question_id, lang_id, problem)
            submissions = await client.submissions(problem)
        finally:
            if owned:
                await client.__aexit__(None, None, None)
    except ValueError as exc:
        os.close(lock_fd)
        return str(exc)
    except Exception as exc:
        os.close(lock_fd)
        return f"Failed to start monitoring '{problem}': {exc}"

    baseline_synced_ts = normalize_ts(synced.get("timestamp")) if synced else None
    baseline_submission_ts = latest_submission_ts(submissions)

    session_dir = create_session(
        root, problem=problem, question_id=question_id, language=language,
        baseline_submission_ts=baseline_submission_ts, when=now,
    )

    try:
        spawn(
            session_dir=session_dir, lock_fd=lock_fd, problem=problem,
            question_id=question_id, lang_id=lang_id, language=language,
            baseline_synced_ts=baseline_synced_ts,
            baseline_submission_ts=baseline_submission_ts,
        )
    except Exception as exc:
        os.close(lock_fd)
        finalize_session(session_dir, "spawn_failed")
        return f"Failed to spawn monitor for '{problem}': {exc}"

    # The child now holds the lock through its inherited fd; drop our copy.
    os.close(lock_fd)

    lines = [
        f"Monitoring '{problem}' ({language}). Recording to {session_dir}",
        "It stops on stop_monitoring(), 15min without a change, or 4h.",
    ]
    if synced is None:
        lines.append(
            f"Note: no cloud-saved code for '{problem}' in {language} yet — "
            f"that's normal for a fresh start, but confirm you're editing in {language}."
        )
    return "\n".join(lines)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop(
    *,
    root=None,
    proc_root="/proc",
    killpg=os.killpg,
    is_alive=None,
    grace: float = 5.0,
    poll: float = 0.2,
) -> str:
    root = Path(root) if root is not None else sessions_root()
    is_alive = is_alive or _pid_alive

    active = find_monitor(proc_root=proc_root)
    if active is None:
        return "Not monitoring anything right now."

    pid = active["pid"]
    try:
        killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass

    waited = 0.0
    while waited < grace and is_alive(pid):
        time.sleep(poll)
        waited += poll
    if is_alive(pid):
        try:
            killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # Finalize here rather than trusting the child: a wedged monitor must not
    # be able to leave a session unstamped.
    session_dir = None
    if active.get("session"):
        candidate = root / active["session"]
        if candidate.is_dir():
            session_dir = candidate
    if session_dir is None:
        session_dir = find_latest_session(root, active.get("problem"))

    if session_dir is not None:
        finalize_session(session_dir, "stopped")
        return f"Stopped monitoring '{active.get('problem')}'.\n\n{render_timeline(session_dir)}"
    return f"Stopped monitoring '{active.get('problem')}' (no session directory found)."


def results(root=None, problem: str | None = None) -> str:
    root = Path(root) if root is not None else sessions_root()
    session_dir = find_latest_session(root, problem)
    if session_dir is None:
        if problem:
            return f"No monitoring session recorded for '{problem}'."
        return "No monitoring sessions recorded yet."
    return render_timeline(session_dir)


__all__ = [
    "ext_for_language", "results", "sessions_root", "spawn_monitor", "start", "stop",
]
