"""Liveness, discovery, and session-directory bookkeeping for the monitor.

Liveness rests entirely on ``flock``: the monitor holds an exclusive lock for
its whole life, and the kernel releases it on process death however that death
happens. A stale lock is therefore not a representable state — there is no
heartbeat to age out and no PID file to invalidate.

Identity is separate, and comes from the process itself: the monitor's
``argv[0]`` is set to ``MONITOR_TAG``, so a ``/proc`` scan recovers which
problem is being monitored without consulting any file that could disagree
with reality.
"""

import datetime as dt
import fcntl
import json
import os
import shutil
from pathlib import Path

MONITOR_TAG = "claub-leetcode-monitor"

# Container-local by design, not on the bind-mounted workspace: the monitor
# cannot outlive the container, so the lock's natural lifetime is the
# container's, and /tmp being wiped on rebuild is correct rather than a hazard.
DEFAULT_LOCK_PATH = Path("/tmp/claub-leetcode-monitor.lock")

DEFAULT_KEEP = 30


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---- liveness ----

def acquire_lock(path=DEFAULT_LOCK_PATH) -> int | None:
    """Take the monitor lock, returning an open fd, or None if already held.

    The caller owns the returned fd: hold it for as long as the monitor should
    be considered alive, and pass it to the child so the lock never lapses
    across the spawn.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


# ---- identity ----

def find_monitor(proc_root="/proc") -> dict | None:
    """Locate the live monitor by scanning for its argv[0] tag.

    Matches argv[0] **exactly**. Substring matching over the whole cmdline
    produces false positives against any process that merely mentions the tag
    — including a shell whose script body contains it.
    """
    proc_root = Path(proc_root)
    try:
        entries = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return None

    for entry in sorted(entries, key=lambda p: int(p.name)):
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        if not argv or argv[0] != MONITOR_TAG:
            continue

        info = {"pid": int(entry.name), "argv": argv, "session": None, "problem": None}
        for arg in argv[1:]:
            for key in ("session", "problem"):
                if arg.startswith(f"--{key}="):
                    info[key] = arg.split("=", 1)[1]
        return info
    return None


# ---- session directories ----

def session_dir_name(problem: str, when: dt.datetime) -> str:
    return f"{when:%Y-%m-%d-%H%M}-{problem}"


def read_manifest(session_dir) -> dict:
    return json.loads((Path(session_dir) / "manifest.json").read_text())


def write_manifest(session_dir, manifest: dict) -> None:
    path = Path(session_dir) / "manifest.json"
    tmp = path.with_name("manifest.json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    os.replace(tmp, path)


def create_session(
    root,
    *,
    problem: str,
    question_id: int,
    language: str,
    baseline_submission_ts: int | None,
    when: dt.datetime,
) -> Path:
    session_dir = Path(root) / session_dir_name(problem, when)
    (session_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    write_manifest(session_dir, {
        "session_id": session_dir.name,
        "problem": problem,
        "question_id": question_id,
        "language": language,
        "started_at": when.isoformat(timespec="seconds"),
        "ended_at": None,
        "stop_reason": None,
        "baseline_submission_ts": baseline_submission_ts,
    })
    return session_dir


def append_event(session_dir, event: dict) -> None:
    """Append one JSON object as a single line. Crash-safe by construction."""
    payload = dict(event)
    ts = payload.pop("ts", None) or _now_iso()
    line = json.dumps({"ts": ts, **payload})
    with open(Path(session_dir) / "events.jsonl", "a") as fh:
        fh.write(line + "\n")


def finalize_session(session_dir, stop_reason: str, ended_at: str | None = None) -> dict:
    """Stamp the terminal state. First writer wins.

    ``stop_monitoring`` finalizes defensively in case a wedged child never
    got there, so this must not clobber a reason the child already recorded.
    """
    manifest = read_manifest(session_dir)
    if manifest.get("stop_reason"):
        return manifest
    manifest["stop_reason"] = stop_reason
    manifest["ended_at"] = ended_at or _now_iso()
    write_manifest(session_dir, manifest)
    return manifest


def session_dirs(root) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    # Names lead with a sortable timestamp, so lexical order is chronological.
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)


def last_event_ts(session_dir) -> str | None:
    path = Path(session_dir) / "events.jsonl"
    try:
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            return json.loads(line).get("ts")
        except json.JSONDecodeError:
            continue
    return None


# ---- retention ----

def prune_sessions(root, keep: int = DEFAULT_KEEP) -> list[str]:
    """Drop all but the ``keep`` most recent sessions. Returns removed names.

    Runs before the new session directory is created, so it can only ever
    remove finished or interrupted sessions.
    """
    dirs = session_dirs(root)
    doomed = dirs if keep <= 0 else dirs[:-keep]
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
    return [p.name for p in doomed]


# ---- reconciliation ----

def reconcile_interrupted(root) -> list[str]:
    """Stamp sessions left unfinished by a container restart or hard kill.

    Data hygiene only — this cannot produce a false "already running", since
    liveness is the lock's job alone.
    """
    reconciled = []
    for session_dir in session_dirs(root):
        try:
            manifest = read_manifest(session_dir)
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("ended_at"):
            continue
        manifest["ended_at"] = last_event_ts(session_dir) or manifest.get("started_at")
        manifest["stop_reason"] = "interrupted"
        write_manifest(session_dir, manifest)
        reconciled.append(session_dir.name)
    return reconciled
