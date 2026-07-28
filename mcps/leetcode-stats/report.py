"""Rendering a recorded solve into something a coach can read in one call.

``get_monitoring_results`` returns this rather than raw JSONL or a directory of
snapshot files: the point is that the agent gets usable signal immediately and
only opens individual snapshots when it wants detail.
"""

import datetime as dt
import json
from pathlib import Path

from session_store import read_manifest, session_dirs


def find_latest_session(root, problem: str | None = None) -> Path | None:
    """Most recent session, optionally restricted to one problem.

    Matching is on the manifest's ``problem`` field rather than the directory
    name, so ``two-sum`` never matches a ``two-sum-ii`` session.
    """
    for session_dir in reversed(session_dirs(root)):
        if problem is None:
            return session_dir
        try:
            if read_manifest(session_dir).get("problem") == problem:
                return session_dir
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _read_events(session_dir) -> list[dict]:
    path = Path(session_dir) / "events.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts)
    except ValueError:
        return None


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _fmt_offset(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"+{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"+{seconds // 60:02d}:{seconds % 60:02d}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def render_timeline(session_dir) -> str:
    """Render one session as a compact, chronological timeline."""
    session_dir = Path(session_dir)
    manifest = read_manifest(session_dir)
    events = _read_events(session_dir)

    started = _parse(manifest.get("started_at"))
    ended = _parse(manifest.get("ended_at"))
    last_ts = _parse(events[-1]["ts"]) if events else None
    finish = ended or last_ts or started
    duration = (finish - started).total_seconds() if started and finish else 0

    changes = [e for e in events if e.get("type") == "code_change"]
    submissions = [e for e in events if e.get("type") == "submission"]

    if manifest.get("ended_at"):
        state = f"stopped: {manifest.get('stop_reason')}"
    else:
        state = "still running"

    lines = [
        f"LeetCode session: {manifest.get('problem')} ({manifest.get('language')})",
        f"  started {manifest.get('started_at')} · {_fmt_duration(duration)} · {state}",
        f"  {_plural(len(changes), 'change')}, {_plural(len(submissions), 'submission')}",
        f"  {session_dir}",
        "",
    ]

    for event in events:
        ts = _parse(event.get("ts"))
        offset = _fmt_offset((ts - started).total_seconds()) if ts and started else "     "
        kind = event.get("type")

        if kind == "code_change":
            ref = str(event.get("snapshot_ref", "")).split("/")[-1]
            lines.append(
                f"  {offset}  +{event.get('lines_added', 0)}/-{event.get('lines_removed', 0)}"
                f"  {event.get('code_len', 0)} chars  {ref}"
            )
        elif kind == "submission":
            lines.append(
                f"  {offset}  SUBMIT  {event.get('status')}"
                + (f"  {event.get('runtime')}" if event.get("runtime") else "")
            )
        elif kind == "idle":
            lines.append(
                f"  {offset}  -- paused {_fmt_duration(event.get('duration_sec', 0))} --"
            )
        elif kind == "error":
            lines.append(f"  {offset}  ERROR ({event.get('kind')}) {event.get('detail')}")
        elif kind == "monitor_end":
            lines.append(f"  {offset}  end: {event.get('stop_reason')}")

    return "\n".join(lines)
