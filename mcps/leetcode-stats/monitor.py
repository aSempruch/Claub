"""Background poller recording an in-progress LeetCode solve.

Runs as a detached process spawned by ``server.start_monitoring``. Never
imported by the server — only ever exec'd, so it must not import FastMCP.
"""

import argparse
import asyncio
import difflib
import time
from pathlib import Path

from leetcode_api import AuthError, LeetCodeClient
from session_store import append_event, finalize_session

# Poll cadence tiers, keyed on time since the last observed code change.
# 5s matches the measured 5-8s LeetCode cloud-sync floor, so active typing is
# captured at full resolution; the decay keeps request volume sane across a
# long session.
FAST_INTERVAL = 5
MEDIUM_INTERVAL = 15
SLOW_INTERVAL = 30

FAST_UNTIL = 60
MEDIUM_UNTIL = 300

# Stop conditions.
IDLE_TIMEOUT = 15 * 60
MAX_DURATION = 4 * 3600
AUTH_GRACE = 5 * 60
MAX_NETWORK_FAILURES = 10

SUBMISSION_INTERVAL = 30

# A pause longer than this preceding a change is itself signal, so it gets its
# own event. Matches the ICF emulator's threshold.
IDLE_EVENT_GAP = 60


_EXT_BY_LANG = {
    "python": ".py", "python3": ".py", "pythondata": ".py",
    "java": ".java", "cpp": ".cpp", "c": ".c", "csharp": ".cs",
    "javascript": ".js", "typescript": ".ts", "golang": ".go",
    "kotlin": ".kt", "swift": ".swift", "rust": ".rs", "scala": ".scala",
    "ruby": ".rb", "php": ".php", "dart": ".dart", "elixir": ".ex",
    "erlang": ".erl", "racket": ".rkt", "bash": ".sh",
    "mysql": ".sql", "mssql": ".sql", "oraclesql": ".sql", "postgresql": ".sql",
}


def ext_for_language(language: str) -> str:
    """Snapshot file extension, so a Java solve isn't written as .py."""
    return _EXT_BY_LANG.get(language, ".txt")


def normalize_ts(value) -> int | None:
    """Coerce a LeetCode timestamp to int.

    The live API is inconsistent: ``syncedCode.timestamp`` is an int while
    ``questionSubmissionList`` timestamps are strings. Comparing raw values
    would make a type change look like an edit and spam snapshots.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def poll_interval(seconds_since_change: float) -> int:
    """Seconds to wait before the next syncedCode poll."""
    if seconds_since_change < FAST_UNTIL:
        return FAST_INTERVAL
    if seconds_since_change < MEDIUM_UNTIL:
        return MEDIUM_INTERVAL
    return SLOW_INTERVAL


def diff_counts(old: str, new: str) -> tuple[int, int]:
    """Return (lines_added, lines_removed) between two code snapshots."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, old_lines, new_lines
    ).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


async def run_poll_loop(
    *,
    client,
    session_dir,
    problem: str,
    question_id: int,
    lang_id: int,
    clock,
    sleep,
    baseline_submission_ts: int | None = None,
    baseline_synced_ts: int | None = None,
    baseline_code: str = "",
    snapshot_ext: str = ".py",
    idle_timeout: float = IDLE_TIMEOUT,
    max_duration: float = MAX_DURATION,
    submission_interval: float = SUBMISSION_INTERVAL,
    auth_grace: float = AUTH_GRACE,
    max_network_failures: int = MAX_NETWORK_FAILURES,
) -> str:
    """Poll until a stop condition fires. Returns the stop reason.

    ``clock`` and ``sleep`` are injected so the loop can be driven in virtual
    time by tests; in production they are ``time.monotonic`` and
    ``asyncio.sleep``.
    """
    session_dir = Path(session_dir)
    started = clock()
    last_change = started
    last_synced_ts = normalize_ts(baseline_synced_ts)
    prev_code = baseline_code
    seq = 0
    seen_submissions: set[str] = set()
    next_submission_poll = started
    auth_failing_since: float | None = None
    network_failures = 0
    reason = None

    while True:
        now = clock()
        if now - started >= max_duration:
            reason = "max_duration"
            break
        if now - last_change >= idle_timeout:
            reason = "idle"
            break

        try:
            synced = await client.synced_code(question_id, lang_id, problem)
        except AuthError as exc:
            # Recoverable: the user may refresh cookies mid-session, and the
            # token is re-read per request, so keep polling for a while.
            if auth_failing_since is None:
                auth_failing_since = now
                append_event(session_dir, {
                    "type": "error", "kind": "auth", "detail": str(exc),
                })
            elif now - auth_failing_since >= auth_grace:
                reason = "auth_expired"
                break
        except Exception as exc:
            network_failures += 1
            if network_failures >= max_network_failures:
                append_event(session_dir, {
                    "type": "error", "kind": "network", "detail": str(exc),
                })
                reason = "network"
                break
        else:
            network_failures = 0
            auth_failing_since = None
            synced_ts = normalize_ts(synced.get("timestamp")) if synced else None
            if synced_ts is not None and synced_ts != last_synced_ts:
                code = synced.get("code") or ""
                gap = now - last_change
                if gap > IDLE_EVENT_GAP:
                    append_event(session_dir, {
                        "type": "idle", "duration_sec": round(gap),
                    })
                seq += 1
                ref = f"snapshots/save-{seq:04d}{snapshot_ext}"
                (session_dir / ref).write_text(code)
                added, removed = diff_counts(prev_code, code)
                append_event(session_dir, {
                    "type": "code_change",
                    "synced_ts": synced_ts,
                    "code_len": len(code),
                    "lines_added": added,
                    "lines_removed": removed,
                    "snapshot_ref": ref,
                })
                prev_code = code
                last_synced_ts = synced_ts
                last_change = now

        if now >= next_submission_poll:
            next_submission_poll = now + submission_interval
            # Submissions are a secondary signal: never let them end a session.
            try:
                submissions = await client.submissions(problem)
            except Exception:
                submissions = []
            for sub in sorted(submissions, key=lambda s: int(s.get("timestamp") or 0)):
                ts = int(sub.get("timestamp") or 0)
                if baseline_submission_ts is not None and ts <= baseline_submission_ts:
                    continue
                sub_id = str(sub.get("id"))
                if sub_id in seen_submissions:
                    continue
                seen_submissions.add(sub_id)
                append_event(session_dir, {
                    "type": "submission",
                    "submission_id": sub_id,
                    "status": sub.get("statusDisplay"),
                    "lang": sub.get("lang"),
                    "runtime": sub.get("runtime"),
                    "memory": sub.get("memory"),
                    "submitted_at": ts,
                })

        await sleep(poll_interval(now - last_change))

    append_event(session_dir, {
        "type": "monitor_end",
        "stop_reason": reason,
        "duration_sec": round(clock() - started),
        "change_count": seq,
    })
    finalize_session(session_dir, reason)
    return reason


def parse_args(argv=None) -> argparse.Namespace:
    """Parse the monitor's argv.

    The tag lives in OS-level argv[0], which Python rewrites in ``sys.argv``,
    so the monitor takes its identity from these flags rather than by
    introspecting its own process name.
    """
    parser = argparse.ArgumentParser(prog="claub-leetcode-monitor")
    parser.add_argument("--session", required=True)
    parser.add_argument("--problem", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--question-id", type=int, required=True)
    parser.add_argument("--lang-id", type=int, required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--baseline-synced-ts", type=int, default=None)
    parser.add_argument("--baseline-submission-ts", type=int, default=None)
    return parser.parse_args(argv)


async def _main(argv=None) -> str:
    args = parse_args(argv)
    session_dir = Path(args.session_dir)

    append_event(session_dir, {
        "type": "monitor_start",
        "session_id": args.session,
        "problem": args.problem,
        "question_id": args.question_id,
        "language": args.language,
        "baseline_submission_ts": args.baseline_submission_ts,
    })

    baseline_code = ""
    async with LeetCodeClient() as client:
        if args.baseline_synced_ts is not None:
            # Diff the first observed change against what was already there.
            try:
                synced = await client.synced_code(
                    args.question_id, args.lang_id, args.problem
                )
                if synced:
                    baseline_code = synced.get("code") or ""
            except Exception:
                pass

        return await run_poll_loop(
            client=client,
            session_dir=session_dir,
            problem=args.problem,
            question_id=args.question_id,
            lang_id=args.lang_id,
            clock=time.monotonic,
            sleep=asyncio.sleep,
            baseline_submission_ts=args.baseline_submission_ts,
            baseline_synced_ts=args.baseline_synced_ts,
            baseline_code=baseline_code,
            snapshot_ext=ext_for_language(args.language),
        )


def main(argv=None) -> None:
    reason = asyncio.run(_main(argv))
    print(f"monitor exiting: {reason}", flush=True)


if __name__ == "__main__":
    main()
