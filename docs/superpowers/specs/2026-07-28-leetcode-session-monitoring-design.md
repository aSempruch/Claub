# LeetCode Session Monitoring — Design

**Date:** 2026-07-28
**Status:** Approved pending review

## Overview

The `leetcode-stats` MCP gains the ability to record **how a problem was solved**, not
just what was submitted. A detached background process polls LeetCode's cloud-synced
editor buffer while the user works, writing a timestamped timeline of every distinct
save — what was written, what was deleted, where the long pauses fell — plus every
submission attempt and its verdict.

Three new tools:

- `start_monitoring(problem, language="python3")`
- `stop_monitoring()`
- `get_monitoring_results(problem=None)`

Output lands in the agent's workspace under `leetcode-sessions/`, in the same shape the
ICF emulator already writes to `sessions/`, so the coach agent's existing debrief
instincts transfer unchanged.

Everything lives inside `mcps/leetcode-stats/`. **No bot changes, no new host daemon,
no new port, no new secret.**

### Why this exists

The coach agent can currently see two things: the final cloud-saved code
(`get_cloud_code`) and the submission history (`get_submissions`). Both are endpoints.
Neither shows the process — the abandoned approach, the fifteen minutes stuck before
the insight, the helper function written and then deleted, the off-by-one fixed on the
third try.

That process is the coachable part. The `icf-emulator` project already proves the
value: its `events.jsonl` is described in its own CLAUDE.md as "the primary product —
it's what the coach agent analyzes." But that only works for problems solved *inside*
the emulator. Problems solved on leetcode.com itself — the majority — are invisible.

This closes that gap using the same data shape, so one analysis vocabulary covers both.

## What was measured

Design decisions below rest on measurements, not assumptions. All taken 2026-07-28.

**Sync cadence — the feature's viability gate.** LeetCode's editor pushes
`updateSyncedCode` roughly **every 5–8 seconds while typing continuously** (measured by
the user against the live site). This is the single fact that makes the feature worth
building: it is comparable to the ICF emulator's 3s client-side autosave debounce, so
polling yields a real timeline rather than coarse before/after snapshots.

**`syncedCode` shape.** Returns `{timestamp, code}` where `timestamp` is unix seconds.
Keyed by `(questionId, lang)` — probing `network-delay-time` returned a python3 entry
and `None` for java and cpp. Language is therefore a required part of the monitor's
identity, not an incidental parameter.

**Users keep editing after solving.** On `network-delay-time`, the accepted submission
landed at 20:19:28 and the synced code timestamp was 20:26:05 — nearly seven minutes of
post-accept editing. This is direct evidence against stopping on an accepted
submission, and it drove the decision to drop submission-based stopping entirely.

**Agent process lifetime.** `leetcode-stats` is a **stdio** MCP, spawned as a
subprocess of the agent's `claude` process. That process is killed by the idle reaper
at `min(21600, 2400 * random.lognormvariate(0, 0.75))` seconds
(`bot/src/claude_assistant/claude_process.py:102`) — median ~40 minutes, randomized per
process. A poller living in the MCP process would therefore die at an unpredictable
point mid-solve, precisely when the user is heads-down and not messaging the agent.
**This is the constraint that shapes the whole architecture.**

**Container init.** PID 1 is `/sbin/docker-init` (tini), which reaps orphans correctly,
so a detached child that outlives its parent is reparented cleanly and does not zombie.
`/proc` is available; `pid_max` is 4194304.

**Process discovery primitives — verified in the `claude-claub` image.** A tagged,
`setsid`'d child holding an exclusive `flock`:

| Scenario | Result |
| --- | --- |
| Monitor alive | `/proc` scan on exact `argv[0]` finds it with its session id; lock reports BUSY |
| Second `start_monitoring` while one runs | Refused, exit 3 |
| Monitor `SIGKILL`ed, zero cleanup, lock file left on disk | Scan finds nothing; **lock reports FREE** |
| `start_monitoring` after that crash | Acquires immediately, runs normally |

The third row is the important one: the kernel releases the lock on process death
regardless of how the process died, so **a stale lock is not a state that can exist**.

A flaw in the first version of that test is preserved as a design constraint: matching
the tag as a *substring anywhere in cmdline* produced false positives against the test
harness itself (whose shell body contained the tag string), causing it to target PID 1.
The scan must match **`argv[0]` exactly**. This is a required regression test.

## Architecture

### Module layout

All under `mcps/leetcode-stats/`:

| File | Responsibility |
| --- | --- |
| `leetcode_api.py` | Auth headers, GraphQL queries, `_resolve_question_id`. Extracted from `server.py` so the monitor can use it **without importing FastMCP**. |
| `server.py` | Existing tools plus the three new ones. Never runs a poll loop. |
| `monitor.py` | The standalone poll loop. Never imported by `server.py` — only ever exec'd. |
| `session_store.py` | Lock acquisition, tag scan, session dir creation, retention pruning, interrupted-session reconciliation. The only module that touches the lock. |

The `server.py` ↔ `monitor.py` boundary is deliberately a process boundary, not a
function call. `server.py` spawns and signals; it never polls.

### Process lifetime and discovery

`start_monitoring` spawns the monitor via `os.setsid()` followed by
`os.execv(sys.executable, ["claub-leetcode-monitor", "monitor.py", "--session=...",
"--problem=...", ...])`. Setting `argv[0]` independently of the executable is what makes
the process self-describing.

Two mechanisms with distinct jobs:

**`flock` answers "is one running" — authoritatively.** The monitor holds
`LOCK_EX | LOCK_NB` on `/tmp/claub-leetcode-monitor.lock` for its entire life.
`start_monitoring` simply attempts the lock: acquired means nothing is running, blocked
means something is. Both guaranteed by the kernel.

This eliminates the heartbeat file, the PID liveness check, and all stale-file cleanup
logic that a PID-file design would require. There is no freshness threshold to tune and
no window in which the answer is wrong.

The lock lives in **container-local `/tmp`**, deliberately, not the bind-mounted
workspace. The monitor cannot survive a container restart, so the lock's natural
lifetime *is* the container's — and `/tmp` being wiped on rebuild is correct behavior
here, not a hazard. It also avoids relying on `flock` semantics across the macOS bind
mount.

**The tag answers "which one, and where is its data."** A `/proc` scan matching
`argv[0] == "claub-leetcode-monitor"` exactly recovers the live monitor and reads its
session id and problem slug straight off the process. So `start_monitoring` can refuse
with *"already monitoring `two-sum`, started 19:00"* sourced from the process itself
rather than from a file that could disagree with reality. It is also greppable in `ps`
and `docker top` when debugging.

`setsid` makes the monitor a process group leader (verified `pid=14 pgid=14 sid=14`),
so `stop_monitoring` uses `killpg` and takes down any children with it.

### Lock handoff across the spawn — no race window

A naive implementation has a gap here: if the server acquires the lock only to *test*
it, releases it, and then spawns the child to acquire it for real, two concurrent
`start_monitoring` calls can both pass the test and both spawn.

The lock is instead **held continuously across the fork**, exploiting the fact that
`flock` binds to the open file description rather than to the process:

1. Server `os.open`s the lock file and takes `LOCK_EX | LOCK_NB` — **before** forking.
2. `os.set_inheritable(fd, True)`.
3. `subprocess.Popen(["claub-leetcode-monitor", "monitor.py", f"--lock-fd={fd}", ...],
   executable=sys.executable, preexec_fn=os.setsid, pass_fds=(fd,))`.
4. Server closes its own copy of the fd. The child's inherited description keeps the
   lock alive.

The child **must not re-`flock`** the fd — it already holds the lock by inheritance.

Verified in the image: after the parent exits, a third process still observes the lock
as BUSY, and `killpg` on the child frees it. There is no instant at which the lock is
unheld.

The same call also confirms the `Popen(executable=..., args[0]=tag)` form sets OS-level
`argv[0]` — `ps` reports `claub-leetcode-monitor monitor.py --lock-fd=3 --session=...`.

One nuance this exposes: **`sys.argv[0]` inside the child is the script path, not the
tag**, because Python rewrites it. The tag exists only at the OS level in
`/proc/<pid>/cmdline`. The monitor must therefore take its identity from `--session=`
and `--problem=`, never from self-introspection.

**Lifetimes are fully decoupled.** The agent process can be reaped, restarted, `/clear`ed,
or crash; the monitor is unaffected. This matches how the feature is described
conceptually — a background job with the MCP as its control surface.

### The one reconciliation pass

The lock is container-local; session data is on the bind mount. If the container
restarts mid-session, the lock correctly vanishes but the session directory is left
without an `ended_at`.

So `start_monitoring` performs one reconciliation: any session directory whose manifest
lacks `ended_at`, with no live monitor holding the lock, is stamped
`stop_reason: "interrupted"` and `ended_at` set to its last event's timestamp. The
partial timeline stays readable instead of looking inexplicably truncated.

This is **data hygiene, not liveness** — it can never produce a false "already
running."

## Tool surface

### `start_monitoring(problem: str, language: str = "python3") -> str`

`problem` is a title slug, consistent with every existing tool in this server.

1. Acquire the lock. On failure, scan for the tag and return
   `"Already monitoring '<slug>' (started <time>). Call stop_monitoring() first."`
2. Reconcile any interrupted sessions.
3. Prune to the 30 most recent session directories.
4. Validate the slug via `_resolve_question_id` — fails fast on a typo, and caches the
   `questionId` so the monitor never re-resolves it.
5. Record the baseline: the most recent submission timestamp for this problem, and the
   current `syncedCode` timestamp if any.
6. Create the session directory and write `manifest.json`.
7. Spawn the monitor, passing the resolved ids as argv.

If `syncedCode` is currently `None`, that is **not an error** — starting before writing
any code is the normal case. The return message notes it so the coach can prompt the
user to confirm they are in the expected language.

### `stop_monitoring() -> str`

No arguments, by design — only one monitor can be active.

Finds the monitor by tag scan, `killpg(SIGTERM)`, waits up to 5s, `SIGKILL` if needed.
Finalizes the manifest itself (`stop_reason: "stopped"`) rather than trusting the child
to do it, so a wedged monitor cannot leave a session unstamped. Returns a one-line
summary: duration, change count, submission count, and the results path.

### `get_monitoring_results(problem: str | None = None) -> str`

Defaults to the most recent session; with `problem`, the most recent for that slug.

Returns a rendered compact timeline plus the directory path — not raw JSONL, and not a
dump of 250 snapshot files. The agent gets usable signal in one call and can read
individual snapshots when it wants detail.

Because the journal is written incrementally, **this works on a live session**. The
coach does not have to wait for monitoring to end to begin a debrief.

## Storage format

`/claub/workspaces/{agent}/leetcode-sessions/<YYYY-MM-DD-HHMM>-<slug>/`

Deliberately mirrors the ICF emulator's session directory so both sources read alike.

```
manifest.json      # atomic temp+rename writes
events.jsonl       # append-only, one write per line
snapshots/save-0001.py
monitor.log        # child stdout/stderr, for debugging
```

**`manifest.json`** — `session_id`, `problem`, `question_id`, `language`, `started_at`,
`ended_at`, `stop_reason`, `baseline_submission_ts`, poll configuration, and counts.

**`events.jsonl`** event types:

| Type | Fields |
| --- | --- |
| `monitor_start` | `session_id`, `problem`, `question_id`, `language`, `baseline_submission_ts` |
| `code_change` | `synced_ts`, `code_len`, `lines_added`, `lines_removed`, `snapshot_ref` |
| `idle` | `duration_sec` — emitted when a gap over 60s precedes a change, matching ICF |
| `submission` | `submission_id`, `status`, `lang`, `runtime`, `memory`, `submitted_at` |
| `error` | `kind`, `detail` |
| `monitor_end` | `stop_reason`, `duration_sec`, `change_count` |

`stop_reason` is one of: `stopped`, `idle`, `max_duration`, `auth_expired`, `network`,
`interrupted`.

**Retention: 30 sessions**, pruned oldest-first on `start_monitoring`. At roughly 250KB
per session that is ~8MB — the constraint being managed is directory listing noise, not
disk. Precedent: the ICF emulator has written into `sessions/` in this same workspace
since May without becoming a problem.

Snapshots are stored in full rather than as diffs. Reconstruction stays trivial, and
`get_monitoring_results` solves the readability problem regardless of storage format.

## Polling

**`syncedCode` on a tiered interval, computed purely from time since the last observed
change:**

| Time since last change | Interval |
| --- | --- |
| < 60s | 5s |
| < 5min | 15s |
| otherwise | 30s |

A pure function of one input, so it is directly unit-testable. 5s matches the measured
5–8s sync floor, catching essentially every distinct save during active typing, while
the decay keeps request volume sane across a 90-minute session — roughly 320
`syncedCode` calls per hour for a mixed session, versus 720 at a flat 5s. That matters
for not resembling a scraper.

**Deduplication is on `timestamp`.** Unchanged timestamp means no snapshot, no event,
no disk write. The poll interval is an upper bound on resolution, not a write rate.

**Submissions poll every 30s** on an independent timer, recording **all** statuses.
Failed submissions are the most coaching-relevant events on the timeline — a Wrong
Answer on test 43 followed by four minutes of edits is exactly the signal being
captured. Submissions do not affect control flow.

**Diffs** use stdlib `difflib` for `lines_added` / `lines_removed`, matching the ICF
emulator's crude line-diff semantics.

## Stop conditions

| Trigger | `stop_reason` |
| --- | --- |
| `stop_monitoring()` called | `stopped` |
| 15 min with no `syncedCode` change | `idle` |
| 4 h total duration | `max_duration` |
| Auth failure persisting 5 min | `auth_expired` |
| 10 consecutive network failures | `network` |

No submission-based stopping. An accepted submission is recorded and nothing more —
justified by the measured seven minutes of post-accept editing above, where stopping on
accept would have discarded the cleanup phase.

The 4h cap is a runaway backstop only; the 15-minute idle timeout does essentially all
the real work.

## Error handling

**Auth failure (401/403) is recoverable.** The monitor **re-reads the token file on
every poll** rather than caching it at startup. On auth failure it writes an `error`
event and continues polling at the 30s floor; if the user refreshes cookies via
`update_session` mid-session, the very next poll picks them up and monitoring resumes
with only a gap in the timeline. Only after 5 continuous minutes of auth failure does
it stop with `auth_expired`. A 90-minute session outliving its token is plausible, and
silently losing the rest of it would be the worst failure mode available.

**Transient network errors** retry with backoff; 10 consecutive failures stops with
`network`.

**All writes are crash-safe**: `events.jsonl` is append-only with one write per line,
`manifest.json` uses atomic temp+rename. A hard kill loses at most the current poll.

## Testing

The poll loop takes an **injected client**, so every case below runs without network.

- Tiered interval function across all three bands and their boundaries.
- Timestamp dedup: unchanged timestamp produces no event and no snapshot.
- Diff line counting: additions, deletions, mixed, whole-file replacement.
- `idle` event emission at the 60s gap threshold.
- Lock contention: second `start_monitoring` refused while one holds the lock.
- Lock release after `SIGKILL` — the third row of the verification table.
- Lock handoff: the lock is observably held by a third process at every point across
  spawn, including after the parent has exited.
- **Tag scan matches `argv[0]` exactly** — regression test for the substring false
  positive found during design.
- Retention pruning at the 30-session boundary. Pruning runs before the new session
  directory is created, so the invariant under test is that it only ever removes
  finished or interrupted sessions.
- Interrupted-session reconciliation stamps `ended_at` from the last event.
- Auth recovery: 401 then a refreshed token resumes without stopping.
- `get_monitoring_results` renders a live (unfinished) session.

Follows the existing `tests/test_cloud_code.py` pattern.

## Rejected alternatives

**Poller inside the MCP process, kept alive by a `can_stop` hook.** Simplest code, and
the mechanism exists — the playwright bridge already vetoes reaping this way via a
`can_stop` hook in the instance config, with a 48h pin cap. Rejected because it pins a `claude` process
alive for the entire solve, reintroducing exactly the stale-OAuth-token race the idle
reaper was built to prevent. A browser session is minutes; a solve is 30–90.

**A third host-side bridge daemon.** Mirrors `playwright-bridge` (9500) and
`exec-bridge` (9501), and would survive container rebuilds. Rejected as disproportionate
— a new launchd service, port, and secret to buy resilience against a failure mode
(mid-session rebuild) that is rare and already degrades gracefully.

**Pushing a notification to the agent when monitoring stops.** Originally specified,
then dropped in favor of pull-based `get_monitoring_results`. Push would have required a
new bot-side HTTP endpoint wrapping `_deliver_agent_message`, since `agent_messaging.py`
only exposes fire-and-forget delivery to *other agents*, and the monitor is not an
agent. Pull keeps the entire feature inside one MCP directory.

**One-shot schedules as the notification channel.** Rejected: schedule creation is
globally rate-limited to 8 firings/day and 40/week across *all* agents
(`bot/src/claude_assistant/mcp_server.py:40-41`), and gated by nighttime-hours checks. Spending that shared
budget on monitor notifications would starve real schedules.

**PID file plus heartbeat for liveness.** The initial proposal. Superseded by `flock`,
which is strictly better: the kernel guarantees release on death, so the stale-state
handling, the heartbeat writer, and the freshness threshold all disappear.

## Deployment

`mcps/leetcode-stats/` is baked into the image at `/app/mcps/`, so this ships with
`docker compose up -d --build`. No configuration change is required: in the instance
config (`~/docker/claub/config/agents.yaml`, not in this repo), `leetcode-coach`
already has `mcp__leetcode-stats__*` in `allowed_tools_additional`, and the wildcard
covers the new tools automatically.

The `career` agent carries the same wildcard, so it will see the monitoring tools too.
Harmless — it writes to its own workspace and contends for the same single lock — but
worth noting rather than discovering later. If that is unwanted, the fix is to replace
the wildcard with an explicit tool list for `career`.

Note that `example/config/agents.yaml` in this repo defines neither agent; the
sanitized example does not need updating for this feature.
