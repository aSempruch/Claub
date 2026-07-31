# Mid-Turn `/model` and `/compact` — Design

**Date:** 2026-07-30
**Status:** Approved

## Problem

`/model` and `/compact` arrive on the same Discord channel as ordinary messages, and
`on_message` dispatches each one as its own asyncio task. So either can land while the
agent is halfway through a turn. Today the two commands handle that very differently,
and one of them is destructive.

### `/model` destroys the in-flight turn

`_handle_model` ends with an unconditional teardown:

```python
self._reaped.add(agent_name)
process = self._processes.get(agent_name)
if process:
    await process.stop()
    del self._processes[agent_name]
```

If a turn is in flight, `stop()` terminates the CLI under it. The blocked
`_read_until_result` reads EOF and raises `RuntimeError("Claude process ended
unexpectedly")`. `_send_with_restart` would normally restart and retry — but the agent
is now in `_reaped`, so it re-raises instead:

```python
except RuntimeError:
    if agent_name in self._reaped:
        raise
```

The user gets `Agent stalled: Claude process ended unexpectedly` and the turn's reply is
gone. Any tool work the turn had already done is orphaned.

The teardown exists because the model is a process-start flag (`--model`), so applying a
change requires a restart. That is true, but it does not require killing a live turn.

### `/compact` already queues, but blindly

`_handle_compact` goes through the normal send path, so it blocks on
`AgentProcess._lock` (`claude_process.py:325`). `asyncio.Lock` is FIFO, so the literal
`/compact` is delivered *after* the current turn rather than injected into it. The
serialization is correct. Three things around it are not:

1. **No feedback.** It posts ``Compacting `x`…`` immediately, then goes silent for
   however long the turn runs. Indistinguishable from a hang.
2. **It can wake up on a dead process.** A queued sender holds a reference to the
   `AgentProcess` it captured before blocking. If the in-flight turn errors and
   `_send_with_restart` calls `_restart_process`, that object's subprocess is
   terminated and `self._processes[name]` is rebound to a new one. The waiter then
   writes to a closed stdin and gets `BrokenPipeError` — which is not a `RuntimeError`,
   so neither `_send_with_restart`'s retry nor `_handle_compact`'s handler catches it.
3. **`/model` kills it outright**, per the section above.

## Design

Keep `AgentProcess._lock` as the single serialization point — it already does the
queueing correctly. Fix the two paths that replace the process out from under it, and
surface the queued state.

### 1. `/model` never stops a process; the restart is lazy

The teardown block is removed from `_handle_model` entirely. The command records the
override and acknowledges; nothing is killed.

The restart moves to `_get_or_start_process`, which compares the live process's `.model`
against the currently configured model and, on a mismatch, drains the in-flight turn
before swapping:

```python
process = self._processes.get(name)
if process and process.is_alive:
    if process.model == self._effective_model(name):
        return process
    await process.wait_until_idle()
    return await self._restart_process(name)
return await self._start_agent(name)
```

`wait_until_idle()` is a new `AgentProcess` method — `async with self._lock: pass`. It
inherits the lock's FIFO ordering, so it drains the current turn *and* anything already
queued behind it, with no polling and no `busy` race window.

Because the restart is driven by comparing state rather than by a command-time side
effect, it also covers cases `/model` never sees: an override cleared by
`_start_agent`'s failure fallback, or a process that outlived a config reload.

**Model resolution is deduplicated.** `build_agent_process` and `_effective_model`
currently compute the same "override → agent config → global" chain in two places. The
new comparison makes drift between them a silent failure mode — the process would run one
model while the bot believed another, and never restart. Both now call a single
module-level `resolve_model(config, name, override)`.

**Acknowledgement wording** splits on `process.busy`:

- idle → ``Model set to `opus` (was `sonnet`). Takes effect on your next message.``
- busy → ``Model set to `opus` (was `sonnet`). Takes effect after the current turn finishes.``

### 2. Queued senders survive the process dying

`send_message` re-checks liveness immediately after acquiring the stream lock:

```python
async with self._lock:
    if not self.is_alive:
        raise RuntimeError(
            f"Agent {self.agent_name} process exited while this message was queued"
        )
```

This converts the `BrokenPipeError` case into the `RuntimeError` that
`_send_with_restart` already knows how to restart-and-retry. It is general: any sender
queued behind a turn that dies now recovers, not just `/compact`.

### 3. `/compact` reports that it is queued

`_handle_compact` checks `process.busy` before sending and picks its opening message
accordingly:

- busy → ``` `x` is mid-turn — compacting when it finishes. ```
- idle → ``` Compacting `x`… ```

The send path is unchanged.

## Out of scope

- **`/clear` and `/stop` still kill mid-turn.** For `/stop` that is the whole point;
  `/clear` reads as the same intent.
- **Concurrent `_get_or_start_process` for one agent.** Two messages arriving together
  can both restart, leaving a briefly orphaned process. This race predates the change
  (two callers can already both hit `_start_agent` on a dead process) and is now
  self-healing: the loser's send hits the new liveness check and retries through
  `_send_with_restart`. A per-agent start lock would close it properly; deferred as a
  separate fix.

## Tests

| Test | Asserts |
|---|---|
| `test_handle_model_while_busy_leaves_process_alive` | no `stop()`, process still in `_processes`, agent not in `_reaped` |
| `test_handle_model_while_busy_says_after_current_turn` | busy wording |
| `test_handle_model_while_idle_says_next_message` | idle wording |
| `test_get_or_start_process_reuses_process_when_model_matches` | no restart on match |
| `test_get_or_start_process_restarts_after_turn_drains_on_model_change` | `wait_until_idle()` awaited *before* `_restart_process` |
| `test_wait_until_idle_blocks_until_turn_finishes` | real lock, real ordering |
| `test_send_message_raises_runtime_error_when_process_died_while_queued` | `RuntimeError`, not `BrokenPipeError` |
| `test_handle_compact_reports_queued_when_busy` | busy wording, still sends `/compact` raw |
