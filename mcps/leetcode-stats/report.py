"""Rendering a recorded solve into something a coach can read in one call.

``get_monitoring_results`` returns this rather than raw JSONL or a directory of
snapshot files: the point is that the agent gets usable signal immediately and
only opens individual snapshots when it wants detail.

The unit of the render is an **edit run**, not a save. LeetCode's cloud autosave
fires every 5-10s while typing, so a save boundary reflects the sampler's clock
rather than the solver's intent: a 19-minute solve produces ~70 saves whose
median diff is one partially-typed line. Segmenting on elapsed time, on
submissions, or on diff magnitude were all tried against real recordings and all
failed — pauses barely exist in the data, approach changes routinely happen with
no submission near them, and the single largest line-diff in the sample turned
out to be a method being moved down a file.

What survives is the sign of the change. Runs of growth coalesce into one
``WROTE``; runs of shrink into one ``DELETED``. That needs no tuned constant,
and it puts the emphasis where the information is: everything added and kept is
recoverable from the final snapshot, so the only content that is lost forever if
the render drops it is the code that was thrown away.
"""

import datetime as dt
import difflib
import json
from pathlib import Path

from session_store import read_manifest, session_dirs

# A single run should never be able to flood the caller's context. Real solves
# top out around 35 diff lines in the opening run, so this only fires on a
# large paste — where the truncation notice is itself the useful signal.
MAX_DIFF_LINES_PER_RUN = 80


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


def _snapshot(session_dir, ref: str) -> str | None:
    """Snapshot contents, or None if the file is gone.

    Retention prunes whole sessions, but a half-deleted directory must still
    render — a dangling ref degrades the diff rather than raising.
    """
    if not ref:
        return None
    try:
        return (Path(session_dir) / ref).read_text()
    except OSError:
        return None


def drop_duplicate_saves(session_dir, events: list[dict]) -> list[dict]:
    """Remove code_change events whose snapshot repeats its predecessor.

    The monitor now gates on content, but sessions recorded before that fix
    contain saves where LeetCode bumped ``syncedCode.timestamp`` without the
    code changing. They are indistinguishable from real edits in the event
    stream and must not open a run.

    Equal ``code_len`` is deliberately not treated as duplicate — a same-length
    substitution is a real edit. When a snapshot is unreadable the event is
    kept, since it cannot be shown to be redundant.
    """
    kept: list[dict] = []
    prev_code: str | None = None
    for event in events:
        if event.get("type") != "code_change":
            kept.append(event)
            continue
        code = _snapshot(session_dir, event.get("snapshot_ref", ""))
        if code is not None and code == prev_code:
            continue
        if code is not None:
            prev_code = code
        kept.append(event)
    return kept


def segment_runs(events: list[dict]) -> list[tuple]:
    """Group the event stream into ``("run", [changes], sign)`` and ``("event", e)``.

    A run breaks when the sign of the size change flips, and any non-code event
    flushes it: work done before a verdict and work done in response to it are
    different work, and merging them across the boundary would hide the
    causality that makes the timeline worth reading.
    """
    out: list[tuple] = []
    run: list[dict] = []
    sign = 0
    prev_len = 0

    def flush():
        nonlocal run, sign
        if run:
            out.append(("run", run, sign))
            run, sign = [], 0

    for event in events:
        if event.get("type") != "code_change":
            flush()
            out.append(("event", event, 0))
            continue
        length = event.get("code_len", 0)
        delta = length - prev_len
        # A zero-delta save is a same-length substitution: real work, and it
        # belongs to whichever run it interrupts rather than starting one.
        direction = 1 if delta > 0 else (-1 if delta < 0 else 0)
        if run and direction and direction != sign:
            flush()
        if not run:
            sign = direction
        run.append(event)
        prev_len = length

    flush()
    return out


def _render_run(session_dir, changes: list[dict], sign: int, base: str,
                offset_of) -> tuple[list[str], str]:
    """One run's header plus its diff. Returns (lines, new base).

    Both the delta and the diff are relative to the last state actually
    rendered, which is not always the immediately preceding save: if a run's
    final snapshot has been pruned the base is carried forward, so the next run
    diffs against the last thing the reader was shown rather than against a
    state they never saw.
    """
    new = _snapshot(session_dir, changes[-1].get("snapshot_ref", ""))
    delta = changes[-1].get("code_len", 0) - len(base)
    span = offset_of(changes[0])
    if len(changes) > 1:
        span = f"{span}-{offset_of(changes[-1]).lstrip('+')}"

    label = "DELETED" if sign < 0 else "WROTE"
    lines = [
        f"  {span}  {label} {delta:+d} chars  [{_plural(len(changes), 'save')}]"
    ]

    if new is None:
        lines.append("      (snapshot unavailable)")
        return lines, base

    diff = [
        ln for ln in difflib.unified_diff(
            (base or "").splitlines(), new.splitlines(), lineterm="", n=0
        )
        if not ln.startswith(("---", "+++"))
    ]
    if len(diff) > MAX_DIFF_LINES_PER_RUN:
        hidden = len(diff) - MAX_DIFF_LINES_PER_RUN
        diff = diff[:MAX_DIFF_LINES_PER_RUN]
        diff.append(f"... {hidden} more diff lines (see {changes[-1].get('snapshot_ref')})")
    lines.extend("      " + ln for ln in diff)
    return lines, new


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

    events = drop_duplicate_saves(session_dir, events)
    segments = segment_runs(events)

    changes = [e for e in events if e.get("type") == "code_change"]
    submissions = [e for e in events if e.get("type") == "submission"]
    runs = [s for s in segments if s[0] == "run"]

    reason = manifest.get("stop_reason")
    if not manifest.get("ended_at"):
        state = "still running"
    elif reason == "stopped":
        state = "stopped"
    else:
        state = f"stopped: {reason}"

    def offset_of(event) -> str:
        ts = _parse(event.get("ts"))
        return _fmt_offset((ts - started).total_seconds()) if ts and started else "     "

    lines = [
        f"LeetCode session: {manifest.get('problem')} ({manifest.get('language')})",
        f"  started {manifest.get('started_at')} · {_fmt_duration(duration)} · {state}",
        f"  {_plural(len(changes), 'save')} in {_plural(len(runs), 'edit run')}, "
        f"{_plural(len(submissions), 'submission')}",
        f"  {session_dir}",
        "",
    ]

    base = ""
    for kind, payload, sign in segments:
        if kind == "run":
            rendered, base = _render_run(session_dir, payload, sign, base, offset_of)
            lines.extend(rendered)
            continue

        event = payload
        offset = offset_of(event)
        kind = event.get("type")

        if kind == "submission":
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
