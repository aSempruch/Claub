# Claub Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the correctness bugs, deduplicate the three copied subsystems, remove the croniter dependency, fix Dockerfile layer caching, and sync CLAUDE.md with actual behavior — all found in the 2026-07-01 codebase review.

**Architecture:** No structural changes. Small targeted fixes to `discord_bot.py` and `claude_process.py`, one new shared helper module (`storage.py`), consolidation of duplicated helpers into existing modules, and doc/Dockerfile updates. Behavior changes are limited to: startup failures now raise instead of registering dead processes, the idle reaper re-checks activity before killing, and `/clear {agent}` works as documented.

**Tech Stack:** Python 3.12, discord.py, APScheduler 3.x, FastMCP, pytest + pytest-asyncio, uv, Docker.

## Global Constraints

- All test commands run from `/Users/you/Claude/bot`: `uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
- Never run integration tests (`test_integration.py`) — they need real Claude auth.
- Python 3.12 syntax allowed (`X | None`, `removeprefix`, etc.).
- Keep existing log message formats unless a task explicitly changes them.
- Work happens on branch `review-fixes` (created in Task 0). Commit after every task.
- **Out of scope (deliberate):** secret env scrubbing (`NEXTCLOUD_TOKEN`/`HASS_TOKEN` visible to agent Bash) and `X-Agent-Name` spoofability of the schedule MCP — accepted risk for a personal bot with semi-trusted agents; revisit if that changes. Also out of scope: redesigning the `_read_until_result` merge heuristic (it encodes real CLI behavior; Task 1 only documents it and removes its dead branch).

---

### Task 0: Land in-flight work and create the branch

The working tree has unrelated in-flight changes (per-agent `model` override, `xhigh` effort level, 60-min turn cap, pinned Claude CLI, LeetCode language-ID fix). They are complete and test-covered. Land them first so review-fix commits stay clean.

**Files:**
- No edits — commits only.

- [ ] **Step 1: Run the full suite to confirm the in-flight work is green**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: all tests PASS. If anything fails, STOP and report — do not commit.

- [ ] **Step 2: Commit in-flight work as two commits**

```bash
cd /Users/you/Claude
git add Dockerfile bot/src/claude_assistant/claude_process.py bot/src/claude_assistant/config.py bot/src/claude_assistant/discord_bot.py
git commit -m "feat(bot): per-agent model override, xhigh effort, 60min turn cap, pin Claude CLI"
git add mcps/leetcode-stats/server.py mcps/leetcode-stats/tests/test_cloud_code.py
git commit -m "fix(mcps): correct LeetCode language IDs to match languageList query"
```

- [ ] **Step 3: Create the working branch**

```bash
git checkout -b review-fixes
```

---

### Task 1: Dead code cleanup in claude_process.py

**Files:**
- Modify: `bot/src/claude_assistant/claude_process.py:19-22` (delete `_check_auth_error`)
- Modify: `bot/src/claude_assistant/claude_process.py:390-404` (result-merge heuristic)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: no API changes. `AuthenticationError` and `_check_error_event` remain untouched.

- [ ] **Step 1: Confirm `_check_auth_error` is dead**

Run: `grep -rn "_check_auth_error" /Users/you/Claude/bot`
Expected: exactly one hit — the definition at `claude_process.py:19`. If there are callers, STOP and report.

- [ ] **Step 2: Delete the function**

Remove these lines from `bot/src/claude_assistant/claude_process.py` (lines 19-22):

```python
def _check_auth_error(text: str) -> None:
    """Raise AuthenticationError if the text looks like an auth failure."""
    if "authentication_error" in text or "Invalid authentication credentials" in text:
        raise AuthenticationError(f"Claude authentication failed: {text[:200]}")
```

- [ ] **Step 3: Remove the unreachable else branch and document the merge heuristic**

In `_read_until_result`, replace the block starting at `if self._is_result_event(event):` (currently lines 390-404) with:

```python
            if self._is_result_event(event):
                result_text = self._extract_result(event)
                # Merge heuristic — the CLI's `result` field only holds the
                # LAST turn's text, so assistant text emitted before tool
                # calls must be stitched back in:
                #   1. nothing collected          -> result as-is
                #   2. result == last collected   -> collected (dedup the last)
                #   3. empty result               -> collected
                #   4. result already contains the first collected block
                #      (CLI concatenated the turns itself) -> result as-is
                #   5. otherwise                  -> collected + result
                if not collected_text:
                    final = result_text
                elif result_text and result_text.strip() == collected_text[-1]:
                    final = "\n\n".join(collected_text) if len(collected_text) > 1 else result_text
                elif not result_text.strip():
                    final = "\n\n".join(collected_text)
                elif collected_text[0] in result_text:
                    final = result_text
                else:
                    final = "\n\n".join(collected_text + [result_text])
                return _apply_reply_sentinel(final)
```

(The change: the old `elif result_text.strip(): ... else: ...` pair collapses to a single `else` — the old final `else` was unreachable because branch 3 already handled empty `result_text`.)

- [ ] **Step 4: Run the process tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_claude_process.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/you/Claude
git add bot/src/claude_assistant/claude_process.py
git commit -m "refactor(bot): drop dead _check_auth_error, document result-merge heuristic"
```

---

### Task 2: Hold a reference to the stderr drain task

Fire-and-forget `asyncio.create_task()` results can be garbage-collected mid-flight. Store the drain task on the instance and cancel it on stop.

**Files:**
- Modify: `bot/src/claude_assistant/claude_process.py` (`__init__`, `start`, `stop`)
- Test: `bot/tests/test_claude_process.py`

**Interfaces:**
- Produces: `AgentProcess._stderr_task: asyncio.Task | None` (private; used only by tests and `stop()`).

- [ ] **Step 1: Write the failing test**

Add to `bot/tests/test_claude_process.py` inside `class TestAgentProcess`:

```python
    @pytest.mark.asyncio
    async def test_start_retains_stderr_drain_task(self, workspace: Path) -> None:
        """The stderr drain task must be referenced so asyncio can't GC it."""
        proc = AgentProcess(workspace=workspace)
        fake = MagicMock()
        fake.stderr = None  # _drain_stderr returns immediately
        fake.stdout = MagicMock()
        fake.stdin = MagicMock()
        fake.returncode = None
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            await proc.start(None)
        assert proc._stderr_task is not None
        proc._stderr_task.cancel()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_claude_process.py::TestAgentProcess::test_start_retains_stderr_drain_task -v`
Expected: FAIL with `AttributeError: 'AgentProcess' object has no attribute '_stderr_task'`

- [ ] **Step 3: Implement**

In `AgentProcess.__init__` (after `self._process: asyncio.subprocess.Process | None = None`):

```python
        self._stderr_task: asyncio.Task | None = None
```

In `start()`, change `asyncio.create_task(self._drain_stderr())` to:

```python
            self._stderr_task = asyncio.create_task(self._drain_stderr())
```

In `stop()`, after the terminate/kill block and before `self._ready.clear()`:

```python
            if self._stderr_task:
                self._stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._stderr_task
                self._stderr_task = None
```

Add `import contextlib` to the imports at the top of the file.

- [ ] **Step 4: Run the process tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_claude_process.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/you/Claude
git add bot/src/claude_assistant/claude_process.py bot/tests/test_claude_process.py
git commit -m "fix(bot): hold reference to stderr drain task so it can't be GC'd"
```

---

### Task 3: Make _start_agent raise on unrecoverable startup failure

Today, if `process.start()` raises and there's no saved session, the exception is swallowed and a never-started process is registered in `self._processes`, causing a confusing 30-second "not ready" stall on first use. Raise instead.

**Files:**
- Modify: `bot/src/claude_assistant/discord_bot.py:217-242` (`_start_agent`)
- Test: `bot/tests/test_discord_bot.py`

**Interfaces:**
- Produces: `_start_agent` now raises `RuntimeError(f"Failed to start agent {name}: ...")` when startup fails with no session to retry, or when the no-resume retry also fails. Callers (`_get_or_start_process` → `_send_with_restart` → `_handle_agent_message`) already handle `RuntimeError` and post "Agent stalled: …" to the channel.

- [ ] **Step 1: Write the failing tests**

Add to `bot/tests/test_discord_bot.py`:

```python
@pytest.mark.asyncio
async def test_start_agent_raises_when_start_fails_without_session(bot: AssistantBot) -> None:
    """No saved session + failed start must raise, not register a dead process."""
    bot.sessions.get = MagicMock(return_value=None)
    failing = MagicMock()
    failing.start = AsyncMock(side_effect=OSError("claude binary missing"))
    with patch("claude_assistant.discord_bot.build_agent_process", return_value=failing):
        with pytest.raises(RuntimeError, match="Failed to start agent"):
            await bot._start_agent("main")
    assert "main" not in bot._processes


@pytest.mark.asyncio
async def test_start_agent_retries_without_resume(bot: AssistantBot) -> None:
    """A failed --resume start clears the session and retries fresh."""
    bot.sessions.get = MagicMock(return_value="stale-uuid")
    bot.sessions.delete = MagicMock()
    proc = MagicMock()
    proc.start = AsyncMock(side_effect=[RuntimeError("resume failed"), None])
    with patch("claude_assistant.discord_bot.build_agent_process", return_value=proc):
        result = await bot._start_agent("main")
    assert result is proc
    assert bot._processes["main"] is proc
    bot.sessions.delete.assert_called_with("main")
    assert proc.start.await_count == 2
```

- [ ] **Step 2: Run tests to verify the first fails**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_discord_bot.py -k start_agent -v`
Expected: `test_start_agent_raises_when_start_fails_without_session` FAILS (`DID NOT RAISE`); `test_start_agent_retries_without_resume` PASSES (existing behavior).

- [ ] **Step 3: Implement**

In `_start_agent`, replace the try/except block:

```python
        session_id = self.sessions.get(name)
        try:
            await process.start(session_id)
        except Exception as e:
            log.exception("Failed to start agent %s", name)
            if not session_id:
                raise RuntimeError(f"Failed to start agent {name}: {e}") from e
            log.info("Retrying %s without --resume", name)
            self.sessions.delete(name)
            try:
                await process.start(None)
            except Exception as e2:
                raise RuntimeError(f"Failed to start agent {name}: {e2}") from e2
```

- [ ] **Step 4: Run the bot tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_discord_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/you/Claude
git add bot/src/claude_assistant/discord_bot.py bot/tests/test_discord_bot.py
git commit -m "fix(bot): raise on unrecoverable agent startup failure instead of registering dead process"
```

---

### Task 4: Idle reaper must re-check activity after the pre-kill delay

The reaper sleeps 0-60s before killing, then only re-checks `process.busy`. A message that arrives *and completes* during that sleep leaves `busy == False` with fresh `_last_activity` — and the agent gets reaped anyway. Extract the per-process logic into `_maybe_reap` (testable) and re-check idle time, not just busyness.

**Files:**
- Modify: `bot/src/claude_assistant/discord_bot.py:273-316` (`_reap_idle_processes`)
- Test: `bot/tests/test_discord_bot.py`

**Interfaces:**
- Produces: `AssistantBot._maybe_reap(name: str, process: AgentProcess) -> None` (async) and `AssistantBot._idle_secs(name: str) -> float`. The loop in `_reap_idle_processes` becomes a thin driver.

- [ ] **Step 1: Write the failing tests**

Add to `bot/tests/test_discord_bot.py` (module level). Note `import time` is needed at the top of the file:

```python
def _reapable_process() -> MagicMock:
    process = MagicMock()
    process.is_alive = True
    process.busy = False
    process.reap_threshold = 100
    process.stop = AsyncMock()
    process.can_stop = AsyncMock(return_value=True)
    return process


@pytest.mark.asyncio
async def test_maybe_reap_kills_idle_process(bot: AssistantBot) -> None:
    import time
    process = _reapable_process()
    bot._processes["main"] = process
    bot._last_activity["main"] = time.monotonic() - 200  # idle > threshold

    with patch("claude_assistant.discord_bot.random.uniform", return_value=0):
        await bot._maybe_reap("main", process)

    process.stop.assert_awaited_once()
    assert "main" not in bot._processes
    assert "main" in bot._reaped


@pytest.mark.asyncio
async def test_maybe_reap_skips_when_activity_arrives_during_delay(bot: AssistantBot) -> None:
    """A message that arrives AND completes during the pre-kill sleep must veto the reap."""
    import time
    process = _reapable_process()
    bot._processes["main"] = process
    bot._last_activity["main"] = time.monotonic() - 200

    real_sleep = asyncio.sleep

    async def sleep_with_activity(secs: float) -> None:
        bot._last_activity["main"] = time.monotonic()  # fresh activity mid-sleep
        await real_sleep(0)

    with patch("claude_assistant.discord_bot.random.uniform", return_value=1), \
         patch("claude_assistant.discord_bot.asyncio.sleep", side_effect=sleep_with_activity):
        await bot._maybe_reap("main", process)

    process.stop.assert_not_awaited()
    assert "main" in bot._processes
    assert "main" not in bot._reaped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_discord_bot.py -k maybe_reap -v`
Expected: both FAIL with `AttributeError: 'AssistantBot' object has no attribute '_maybe_reap'`

- [ ] **Step 3: Implement**

Replace the whole `_reap_idle_processes` method (`discord_bot.py:273-316`) with:

```python
    async def _reap_idle_processes(self) -> None:
        """Kill agent processes that have been idle longer than their reap threshold."""
        try:
            while True:
                await asyncio.sleep(60)
                for name, process in list(self._processes.items()):
                    await self._maybe_reap(name, process)
        except asyncio.CancelledError:
            log.info("Idle reaper cancelled")
            return

    def _idle_secs(self, name: str) -> float:
        last = self._last_activity.get(name, 0)
        return time.monotonic() - last if last else 0.0

    async def _maybe_reap(self, name: str, process: AgentProcess) -> None:
        """Reap *process* if it has been idle past its threshold; otherwise no-op."""
        if not process.is_alive:
            return
        idle_secs = self._idle_secs(name)
        if not (self._last_activity.get(name) and idle_secs > process.reap_threshold and not process.busy):
            self._veto_pin_started.pop(name, None)
            return
        # Random delay so kill times don't land on round minutes
        await asyncio.sleep(random.uniform(0, 60))
        # Re-check: a message may have arrived (and even completed) during the delay
        idle_secs = self._idle_secs(name)
        if process.busy or idle_secs <= process.reap_threshold:
            self._veto_pin_started.pop(name, None)
            return
        if not await process.can_stop():
            now = time.monotonic()
            pin_start = self._veto_pin_started.setdefault(name, now)
            pinned_for = now - pin_start
            if pinned_for <= self._max_pin_secs:
                log.info(
                    "Reap of %s vetoed by can_stop hook (pinned %.0fs/%.0fs)",
                    name, pinned_for, self._max_pin_secs,
                )
                return
            log.warning(
                "Reap of %s proceeding despite veto (pinned %.0fs > cap %.0fs)",
                name, pinned_for, self._max_pin_secs,
            )
        self._veto_pin_started.pop(name, None)
        log.info(
            "Reaping idle agent %s (idle %.0fs, threshold %.0fs)",
            name, idle_secs, process.reap_threshold,
        )
        self._reaped.add(name)
        await process.stop()
        self._processes.pop(name, None)
```

- [ ] **Step 4: Run the bot tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_discord_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/you/Claude
git add bot/src/claude_assistant/discord_bot.py bot/tests/test_discord_bot.py
git commit -m "fix(bot): idle reaper re-checks activity after pre-kill delay; extract _maybe_reap"
```

---

### Task 5: Implement `/clear {agent}` and merge the reset/stop handlers

CLAUDE.md documents `/clear {agent}` but the code only matches bare `/clear`. Implement the arg form and fold the near-identical `_handle_reset`/`_handle_stop` bodies into one helper.

**Files:**
- Modify: `bot/src/claude_assistant/discord_bot.py:332-348` (`_handle_message`) and `:434-462` (`_handle_reset`, `_handle_stop`)
- Test: `bot/tests/test_discord_bot.py`

**Interfaces:**
- Produces: `_handle_reset(message, target: str | None = None)` (default keeps existing test call sites working); `_stop_agent_process(agent_name: str) -> None` (async, shared teardown). `/stop` stays channel-only.

- [ ] **Step 1: Write the failing tests**

Add to `bot/tests/test_discord_bot.py`:

```python
@pytest.mark.asyncio
async def test_handle_reset_with_agent_arg(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 100  # main's channel — target overrides routing
    msg.channel.send = AsyncMock()
    proc = MagicMock()
    proc.stop = AsyncMock()
    bot._processes["journalist"] = proc
    await bot._handle_reset(msg, target="journalist")
    bot.sessions.delete.assert_called_with("journalist")
    proc.stop.assert_awaited_once()
    assert "journalist" not in bot._processes


@pytest.mark.asyncio
async def test_handle_reset_unknown_agent_arg(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 100
    msg.channel.send = AsyncMock()
    await bot._handle_reset(msg, target="nope")
    bot.sessions.delete.assert_not_called()
    msg.channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_message_routes_clear_with_arg(bot: AssistantBot) -> None:
    msg = MagicMock()
    msg.channel.id = 100
    msg.channel.send = AsyncMock()
    msg.content = "/clear journalist"
    with patch.object(bot, "_handle_reset", new=AsyncMock()) as reset:
        await bot._handle_message(msg)
    reset.assert_awaited_once_with(msg, target="journalist")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_discord_bot.py -k "agent_arg or clear_with_arg" -v`
Expected: FAIL — `_handle_reset` doesn't accept `target`, and `/clear journalist` falls through to agent routing.

- [ ] **Step 3: Implement**

In `_handle_message`, replace the `/clear` check:

```python
        if content == "/clear" or content.startswith("/clear "):
            await self._handle_reset(message, target=content.removeprefix("/clear").strip() or None)
            return
```

Replace `_handle_reset` and `_handle_stop` (keeping section comment `# --- Commands ---`):

```python
    async def _handle_reset(self, message: discord.Message, target: str | None = None) -> None:
        if target is not None and target not in self.config.agents:
            await message.channel.send(f"Unknown agent: `{target}`")
            return
        agent_name = target or self.router.route(str(message.channel.id))
        if agent_name is None:
            return

        await self._stop_agent_process(agent_name)
        self.sessions.delete(agent_name)
        await message.channel.send(f"Agent `{agent_name}` reset.")

    async def _handle_stop(self, message: discord.Message) -> None:
        agent_name = self.router.route(str(message.channel.id))
        if agent_name is None:
            return

        log.info("/stop received for %s (chan=%s)", agent_name, message.channel.id)
        await self._stop_agent_process(agent_name)
        await message.channel.send(f"Agent `{agent_name}` stopped.")

    async def _stop_agent_process(self, agent_name: str) -> None:
        """Stop an agent's process and mark it intentionally stopped (not supervisor-restartable)."""
        self._reaped.add(agent_name)
        process = self._processes.pop(agent_name, None)
        if process:
            await process.stop()
        self._last_activity.pop(agent_name, None)
```

- [ ] **Step 4: Run the bot tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_discord_bot.py -v`
Expected: PASS (including the three pre-existing `_handle_reset` tests, which call it without `target`).

- [ ] **Step 5: Commit**

```bash
cd /Users/you/Claude
git add bot/src/claude_assistant/discord_bot.py bot/tests/test_discord_bot.py
git commit -m "feat(bot): implement documented /clear {agent}; merge reset/stop teardown"
```

---

### Task 6: Extract the shared turn-running tail into _run_agent_turn

`_handle_agent_message` and `_handle_scheduled` duplicate the auth-error handling, RuntimeError handling, and session-ID persistence. Extract one helper.

**Files:**
- Modify: `bot/src/claude_assistant/discord_bot.py:350-430` (`_handle_agent_message`, `_handle_scheduled`, new `_run_agent_turn`)

**Interfaces:**
- Produces: `_run_agent_turn(agent_name: str, content: str, channel: discord.abc.Messageable, failure_label: str) -> str | None` (async). Returns the result text, or `None` after posting an error to the channel. Persists the session ID on success.

- [ ] **Step 1: Implement the helper and rewrite both callers**

Add after `_send_with_restart`:

```python
    async def _run_agent_turn(
        self,
        agent_name: str,
        content: str,
        channel: discord.abc.Messageable,
        failure_label: str,
    ) -> str | None:
        """Send *content* to the agent; on failure notify *channel* and return None.

        Persists the session ID after a successful turn.
        """
        try:
            result = await self._send_with_restart(agent_name, content)
        except AuthenticationError:
            log.error("Claude authentication failed for %s", agent_name)
            await channel.send(
                "Claude authentication expired. Re-authenticate with `claude` on the host and restart."
            )
            return None
        except RuntimeError as e:
            log.exception("%s for %s", failure_label, agent_name)
            await channel.send(f"{failure_label}: {e}")
            return None

        process = self._processes.get(agent_name)
        if process and process.session_id:
            self.sessions.set(agent_name, process.session_id)
        return result
```

Replace `_handle_agent_message`:

```python
    async def _handle_agent_message(
        self, message: discord.Message, agent_name: str, content: str
    ) -> None:
        log.info(
            "Handling message for %s (chan=%s, len=%d)",
            agent_name, message.channel.id, len(content),
        )
        async with _safe_typing(message.channel):
            footer = await download_attachments(message, agent_name)
            if footer:
                content = (content + footer) if content else footer.lstrip()
            result = await self._run_agent_turn(
                agent_name, content, message.channel, failure_label="Agent stalled"
            )
        if result is None:
            return
        await self._send_chunked(message.channel, agent_name, result)
```

Replace the body of `_handle_scheduled` after the channel lookup:

```python
        result = await self._run_agent_turn(
            agent_name, prompt, channel, failure_label="Scheduled task failed"
        )
        if result is None:
            return

        if "[NO_POST]" in result:
            log.info("Agent %s opted out of posting", agent_name)
            return

        await self._send_chunked(channel, agent_name, result)
```

(User-visible copy is preserved: "Agent stalled: …" and "Scheduled task failed: …".)

- [ ] **Step 2: Run the bot tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_discord_bot.py tests/test_attachments.py -v`
Expected: PASS (`test_handle_agent_message_appends_attachment_footer` patches `_send_with_restart`, which `_run_agent_turn` still calls).

- [ ] **Step 3: Commit**

```bash
cd /Users/you/Claude
git add bot/src/claude_assistant/discord_bot.py
git commit -m "refactor(bot): extract _run_agent_turn shared by message and scheduled paths"
```

---

### Task 7: Consolidate atomic JSON writes into storage.py

`session.py`, `schedule_store.py`, and `firing_history.py` carry byte-identical `_save()` tempfile logic.

**Files:**
- Create: `bot/src/claude_assistant/storage.py`
- Modify: `bot/src/claude_assistant/session.py`, `bot/src/claude_assistant/schedule_store.py`, `bot/src/claude_assistant/firing_history.py`
- Test: `bot/tests/test_storage.py`

**Interfaces:**
- Produces: `atomic_write_json(path: Path, data: Any) -> None` in `claude_assistant.storage`.

- [ ] **Step 1: Write the failing test**

Create `bot/tests/test_storage.py`:

```python
import json
from pathlib import Path

import pytest

from claude_assistant.storage import atomic_write_json


def test_atomic_write_json_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "data.json"
    atomic_write_json(p, {"a": 1, "b": [2, 3]})
    assert json.loads(p.read_text()) == {"a": 1, "b": [2, 3]}
    # No stray tempfiles left behind
    assert not list(p.parent.glob("*.tmp"))


def test_atomic_write_json_overwrites(tmp_path: Path) -> None:
    p = tmp_path / "data.json"
    atomic_write_json(p, {"v": 1})
    atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text()) == {"v": 2}


def test_atomic_write_json_unserializable_leaves_no_tmp(tmp_path: Path) -> None:
    p = tmp_path / "data.json"
    with pytest.raises(TypeError):
        atomic_write_json(p, {"bad": object()})
    assert not p.exists()
    assert not list(tmp_path.glob("*.tmp"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claude_assistant.storage'`

- [ ] **Step 3: Implement**

Create `bot/src/claude_assistant/storage.py`:

```python
"""Shared persistence helpers."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any) -> None:
    """Write *data* as indented JSON to *path* atomically (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False
    )
    try:
        json.dump(data, tmp, indent=2)
        tmp.close()
        Path(tmp.name).replace(path)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_storage.py -v`
Expected: PASS

- [ ] **Step 5: Switch the three stores over**

In each of `session.py`, `schedule_store.py`, `firing_history.py`:
- Add `from claude_assistant.storage import atomic_write_json` to imports.
- Replace the entire `_save` method body with:

```python
    def _save(self) -> None:
        atomic_write_json(self._path, self._data)
```

- Remove the now-unused `import tempfile` from each file. In `session.py`, `json` is still used by the constructor's `json.loads` — keep it; same for the other two.

- [ ] **Step 6: Run the store tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_session.py tests/test_schedule_store.py tests/test_firing_history.py tests/test_storage.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
cd /Users/you/Claude
git add bot/src/claude_assistant/storage.py bot/src/claude_assistant/session.py bot/src/claude_assistant/schedule_store.py bot/src/claude_assistant/firing_history.py bot/tests/test_storage.py
git commit -m "refactor(bot): consolidate atomic JSON writes into storage.atomic_write_json"
```

---

### Task 8: Deduplicate agent-setup helpers between the bot and the debug CLI

`debug_agent.py` reimplements `_mcp_configs_for`, `_disallowed_skills_for`, and `_load_agent_definition`. Promote the bot's methods to module-level functions in `discord_bot.py` (the precedent: `build_agent_process` and `_ensure_authoring_symlink` already live there and are imported by `debug_agent`).

**Files:**
- Modify: `bot/src/claude_assistant/discord_bot.py` (methods at `:196-215` and `:545-554` become module functions)
- Modify: `bot/src/claude_assistant/debug_agent.py:36-71` (delete local copies, import instead)

**Interfaces:**
- Produces, at module level in `claude_assistant.discord_bot`:
  - `mcp_configs_for(agent_name: str, shared: Path | None, agents_dir: Path | None) -> list[Path]`
  - `disallowed_skills_for(name: str, config: AssistantConfig, all_skills: list[str]) -> list[str]`
  - `load_agent_definition(name: str, agents_dir: Path | None) -> dict[str, str] | None`

- [ ] **Step 1: Confirm no other callers of the method/function names**

Run: `grep -rn "_mcp_configs_for\|_disallowed_skills_for\|_load_agent_definition" /Users/you/Claude/bot`
Expected: hits only in `discord_bot.py` and `debug_agent.py`. If tests reference them, update those call sites in Step 4.

- [ ] **Step 2: Add module-level functions to discord_bot.py**

Insert after `_merged_hooks` (before `_ensure_authoring_symlink`):

```python
def mcp_configs_for(agent_name: str, shared: Path | None, agents_dir: Path | None) -> list[Path]:
    """Build MCP config list: shared + per-agent if it exists."""
    configs: list[Path] = []
    if shared and shared.exists():
        configs.append(shared)
    if agents_dir:
        per_agent = agents_dir / f"{agent_name}.mcp.json"
        if per_agent.exists():
            configs.append(per_agent)
    return configs


def disallowed_skills_for(name: str, config: AssistantConfig, all_skills: list[str]) -> list[str]:
    """Return skills this agent is NOT allowed to use."""
    agent_config = config.agents.get(name)
    allowed = set(config.allowed_skills)
    if agent_config:
        allowed |= set(agent_config.allowed_skills)
    return [s for s in all_skills if s not in allowed]


def load_agent_definition(name: str, agents_dir: Path | None) -> dict[str, str] | None:
    """Load the agent .md file from the agents dir, if it exists."""
    if not agents_dir:
        return None
    agent_file = agents_dir / f"{name}.md"
    if not agent_file.exists():
        return None
    try:
        return parse_agent_file(agent_file)
    except Exception:
        log.exception("Failed to parse agent file %s", agent_file)
        return None
```

- [ ] **Step 3: Delete the bot methods and update _start_agent**

Delete the `AssistantBot._disallowed_skills_for`, `AssistantBot._load_agent_definition`, and `AssistantBot._mcp_configs_for` methods. In `_start_agent`, update the `build_agent_process` call:

```python
        process = build_agent_process(
            name=name,
            workspace=workspace,
            config=self.config,
            mcp_configs=mcp_configs_for(name, self.mcp_config, self.agents_dir),
            agent_definition=load_agent_definition(name, self.agents_dir),
            disallowed_skills=disallowed_skills_for(name, self.config, self.all_skills),
        )
```

- [ ] **Step 4: Update debug_agent.py**

Delete the local `_mcp_configs_for`, `_disallowed_skills_for`, `_load_agent_definition` functions (lines 36-71). Update the import:

```python
from claude_assistant.discord_bot import (
    _ensure_authoring_symlink,
    build_agent_process,
    disallowed_skills_for,
    load_agent_definition,
    mcp_configs_for,
)
```

In `_build_process`, replace the setup tail (the module function now does the `exists()` checks itself):

```python
    return build_agent_process(
        name=agent_name,
        workspace=workspace,
        config=config,
        mcp_configs=mcp_configs_for(agent_name, mcp_config, agents_dir),
        agent_definition=load_agent_definition(agent_name, agents_dir),
        disallowed_skills=disallowed_skills_for(agent_name, config, all_skills),
        debug=True,
    )
```

and delete the now-unused `shared_mcp` / `agents_dir_resolved` locals.

- [ ] **Step 5: Run the full suite and smoke-import the debug CLI**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py && uv run --extra dev python -c "import claude_assistant.debug_agent"`
Expected: PASS, no import errors.

- [ ] **Step 6: Commit**

```bash
cd /Users/you/Claude
git add bot/src/claude_assistant/discord_bot.py bot/src/claude_assistant/debug_agent.py
git commit -m "refactor(bot): share agent-setup helpers between bot and debug CLI"
```

---

### Task 9: Replace croniter with CronTrigger in the density check

The code validates crons with APScheduler's `CronTrigger` but projects density with `croniter` — two parsers with diverging edge-case semantics (notably day-of-month/day-of-week combination), and an extra direct dependency. Project with `CronTrigger`, same as `check_nighttime_hours` already does.

**Files:**
- Modify: `bot/src/claude_assistant/mcp_server.py:94-107` (`_project` inside `check_schedule_density`), remove `from croniter import croniter` import
- Modify: `bot/pyproject.toml` (remove `croniter>=2.0`)

**Interfaces:**
- No signature changes. `check_schedule_density` keeps returning `str | None`. All datetimes inside stay naive-local (matching `_now()` and `FiringHistory.fired_at`); `CronTrigger` results are converted from aware to naive local.

**Timezone care:** `CronTrigger.get_next_fire_time` needs an aware `now` and returns aware datetimes in the trigger's (local) timezone. History timestamps and `_now()` are naive local. Convert at the boundary: `aware_now = now.astimezone()` going in, `.astimezone().replace(tzinfo=None)` coming out. Iterate by advancing the probe time (`t = nxt + timedelta(seconds=1)`) — do NOT pass the previous fire as `previous_fire_time` with a fixed `now` (APScheduler clamps `start_date = min(now, previous + 1µs)`, which loops forever).

- [ ] **Step 1: Run the density tests as the baseline**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_density.py -v`
Expected: PASS (17+ tests). These are the regression net for this task.

- [ ] **Step 2: Implement**

In `check_schedule_density`, after `now = _now()` add:

```python
    aware_now = now.astimezone()  # CronTrigger needs an aware anchor
```

Replace the `_project` closure with:

```python
    def _project(cron_expr: str, is_one_shot: bool) -> list[datetime]:
        trigger = CronTrigger.from_crontab(cron_expr)
        times: list[datetime] = []
        t = aware_now
        while True:
            nxt = trigger.get_next_fire_time(None, t)
            if nxt is None:
                break
            naive = nxt.astimezone().replace(tzinfo=None)
            if naive >= horizon:
                break
            times.append(naive)
            if is_one_shot:
                break
            t = nxt + timedelta(seconds=1)
        return times
```

Delete `from croniter import croniter` from the imports.

- [ ] **Step 3: Run the density and MCP tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_density.py tests/test_mcp_server.py -v`
Expected: PASS. If a test fails on a cron-semantics difference between croniter and CronTrigger, STOP and report the specific expression — do not adjust test expectations silently.

- [ ] **Step 4: Drop the dependency**

Remove the line `"croniter>=2.0",` from `bot/pyproject.toml`, then:

Run: `cd /Users/you/Claude/bot && uv lock && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: lockfile updates, full suite PASSES, and `grep -rn croniter src/` returns nothing.

- [ ] **Step 5: Commit**

```bash
cd /Users/you/Claude
git add bot/src/claude_assistant/mcp_server.py bot/pyproject.toml bot/uv.lock
git commit -m "refactor(bot): project schedule density with CronTrigger, drop croniter dep"
```

---

### Task 10: Small consolidations (jitter, config parsing, path tuple)

Three independent micro-cleanups, one commit.

**Files:**
- Modify: `bot/src/claude_assistant/scheduler.py:83-104` (jitter functions), `:134-148` (`_add_apscheduler_job`)
- Modify: `bot/src/claude_assistant/config.py:53-111` (`load_config`)
- Modify: `bot/src/claude_assistant/main.py:25-53`, `bot/src/claude_assistant/debug_agent.py:74-107`

**Interfaces:**
- `lognormal_jitter()` and `recurring_jitter()` keep their names and zero-required-arg call form (tests patch them by name). `lognormal_jitter` loses its never-used `sigma`/`max_delay` params.
- `_resolve_paths()` now returns `ClaubPaths` (a NamedTuple) instead of a bare 8-tuple. Field names: `config, workspaces, sessions, mcp_config, agents_dir, schedules, skills, firing_history`.

- [ ] **Step 1: scheduler.py — merge the jitter functions**

Add `import math` to the top-level imports. Replace both functions (removing the two inner `import math` statements):

```python
def _clamped_lognormal(median_low: float, median_high: float, sigma: float, max_delay: float) -> float:
    """Lognormal-distributed delay with a uniformly randomized median, clamped to *max_delay*."""
    median = random.uniform(median_low, median_high)
    return min(random.lognormvariate(math.log(median), sigma), max_delay)


def lognormal_jitter() -> float:
    """Delay for one-shot schedules: median 8-12 min, capped at 30 min."""
    return _clamped_lognormal(JITTER_MEDIAN_LOW, JITTER_MEDIAN_HIGH, JITTER_SIGMA, JITTER_MAX)


def recurring_jitter() -> float:
    """Delay for recurring schedules: median 15-25 min, capped at 55 min.

    Most firings land 10-40 min after the cron time, occasionally up to ~55 min.
    """
    return _clamped_lognormal(
        RECURRING_JITTER_MEDIAN_LOW, RECURRING_JITTER_MEDIAN_HIGH,
        RECURRING_JITTER_SIGMA, RECURRING_JITTER_MAX,
    )
```

Also simplify `_add_apscheduler_job` — the if/else assigns identical `args`:

```python
    def _add_apscheduler_job(self, agent_name: str, entry: dict) -> None:
        job_id = f"{agent_name}_{entry['id']}"
        run_fn = self._run_one_shot if entry.get("one_shot") else self._run
        self._scheduler.add_job(
            run_fn,
            trigger=CronTrigger.from_crontab(entry["cron"]),
            args=[agent_name, entry["id"], entry["cron"], entry["prompt"]],
            id=job_id,
            name=f"{agent_name}: {entry['prompt'][:50]}",
        )
```

- [ ] **Step 2: config.py — normalize agent_raw once, move _validate_effort to module level**

Move `_validate_effort` out of `load_config` to module level, next to `_validate_compact_pct`:

```python
def _validate_effort(value: str | None, context: str) -> str | None:
    if value is not None and value not in VALID_EFFORT_LEVELS:
        raise ValueError(
            f"{context}: effort must be one of {VALID_EFFORT_LEVELS}, got {value!r}"
        )
    return value
```

In the agents loop, bind once and drop all ten `(agent_raw or {})` wrappers:

```python
    agents: dict[str, AgentConfig] = {}
    for name, agent_raw in (raw.get("agents") or {}).items():
        agent_raw = agent_raw or {}
        channel_id = agent_raw.get("channel_id")
        if not channel_id:
            raise ValueError(f"agents.{name}.channel_id is required")
        agents[name] = AgentConfig(
            channel_id=channel_id,
            display_name=agent_raw.get("display_name"),
            avatar_url=agent_raw.get("avatar_url"),
            allowed_tools_additional=agent_raw.get("allowed_tools_additional") or [],
            allowed_skills=agent_raw.get("allowed_skills") or [],
            model=agent_raw.get("model"),
            effort=_validate_effort(agent_raw.get("effort"), f"agents.{name}"),
            compact_pct=_validate_compact_pct(agent_raw.get("compact_pct"), f"agents.{name}"),
            on_start=agent_raw.get("on_start") or [],
            on_stop=agent_raw.get("on_stop") or [],
            can_stop=agent_raw.get("can_stop") or [],
        )
```

- [ ] **Step 3: main.py — named paths instead of an 8-tuple**

Add `from typing import NamedTuple` to imports. Replace `_resolve_paths`:

```python
class ClaubPaths(NamedTuple):
    config: Path
    workspaces: Path
    sessions: Path
    mcp_config: Path
    agents_dir: Path
    schedules: Path
    skills: Path
    firing_history: Path


def _resolve_paths() -> ClaubPaths:
    """Resolve paths relative to CLAUB_HOME (default: ~/.claub)."""
    claub_home = Path(os.environ.get("CLAUB_HOME", Path.home() / ".claub"))
    return ClaubPaths(
        config=claub_home / "config" / "agents.yaml",
        workspaces=claub_home / "workspaces",
        sessions=claub_home / "data" / "sessions.json",
        mcp_config=claub_home / "config" / "mcp.json",
        agents_dir=claub_home / "config" / "agents",
        schedules=claub_home / "data" / "schedules.json",
        skills=claub_home / "config" / "skills",
        firing_history=claub_home / "data" / "firing_history.json",
    )
```

Update `main()` to use fields (replacing the 8-name unpack):

```python
    paths = _resolve_paths()

    if not paths.config.exists():
        log.error("Config not found: %s", paths.config)
        sys.exit(1)

    mcp_port = int(os.environ.get("CLAUB_MCP_PORT", "9400"))

    config = load_config(paths.config)
    sessions = SessionStore(paths.sessions)
    schedules = ScheduleStore(paths.schedules)
    history_retention = int(os.environ.get("CLAUB_SCHEDULE_HISTORY_RETENTION_DAYS", "30"))
    firing_history = FiringHistory(paths.firing_history, retention_days=history_retention)
    all_skills = discover_skills(paths.skills)

    bot = AssistantBot(
        config=config,
        workspaces_dir=paths.workspaces,
        session_store=sessions,
        schedule_store=schedules,
        firing_history=firing_history,
        mcp_config=paths.mcp_config if paths.mcp_config.exists() else None,
        agents_dir=paths.agents_dir if paths.agents_dir.exists() else None,
        mcp_port=mcp_port,
        all_skills=all_skills,
    )
```

Update `debug_agent._build_process` to match (replacing its 8-name unpack; this builds on Task 8's version):

```python
    paths = _resolve_paths()

    if not paths.config.exists():
        print(f"error: config not found: {paths.config}", file=sys.stderr)
        sys.exit(1)

    config = load_config(paths.config)
    if agent_name not in config.agents:
        known = ", ".join(sorted(config.agents.keys()))
        print(f"error: unknown agent {agent_name!r}. known: {known}", file=sys.stderr)
        sys.exit(2)

    all_skills = discover_skills(paths.skills)
    workspace = paths.workspaces / agent_name
    workspace.mkdir(parents=True, exist_ok=True)
    _ensure_authoring_symlink(workspace, "skills")
    _ensure_authoring_symlink(workspace, "agents")

    return build_agent_process(
        name=agent_name,
        workspace=workspace,
        config=config,
        mcp_configs=mcp_configs_for(agent_name, paths.mcp_config, paths.agents_dir),
        agent_definition=load_agent_definition(agent_name, paths.agents_dir),
        disallowed_skills=disallowed_skills_for(agent_name, config, all_skills),
        debug=True,
    )
```

- [ ] **Step 4: Run the full suite**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: PASS (test_scheduler.py patches `lognormal_jitter`/`recurring_jitter` by name — both names survive; test_config.py exercises `load_config` unchanged).

- [ ] **Step 5: Commit**

```bash
cd /Users/you/Claude
git add bot/src/claude_assistant/scheduler.py bot/src/claude_assistant/config.py bot/src/claude_assistant/main.py bot/src/claude_assistant/debug_agent.py
git commit -m "refactor(bot): merge jitter helpers, tidy config parsing, name the resolved paths"
```

---

### Task 11: Fix Dockerfile dependency-layer caching

`COPY bot/src/ ./src/` currently runs *before* `uv sync`, so every source edit invalidates the dependency install layer — defeating the comment's stated intent. Split into deps-only sync, then source copy, then project install.

**Files:**
- Modify: `Dockerfile:18-22`

- [ ] **Step 1: Reorder the layers**

Replace:

```dockerfile
# Install Python deps (deps layer cached separately from source)
WORKDIR /app/bot
COPY bot/pyproject.toml bot/uv.lock ./
COPY bot/src/ ./src/
RUN uv sync --no-dev --frozen
```

with:

```dockerfile
# Install Python deps first (cached until pyproject/uv.lock change),
# then copy source and install the project itself.
WORKDIR /app/bot
COPY bot/pyproject.toml bot/uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project
COPY bot/src/ ./src/
RUN uv sync --no-dev --frozen
```

- [ ] **Step 2: Verify the image builds and caching works**

Run: `cd /Users/you/Claude && docker compose build`
Expected: build succeeds. Then touch a source file and rebuild to confirm the deps layer is cached:

Run: `touch bot/src/claude_assistant/main.py && docker compose build 2>&1 | grep -E "CACHED|uv sync"`
Expected: the first `uv sync --no-install-project` layer shows CACHED; only the final `uv sync` re-runs.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "build: cache Python deps layer independently of bot source"
```

---

### Task 12: Sync CLAUDE.md with actual behavior

CLAUDE.md is loaded into every dev session; drift here compounds. Four fixes — wrong density numbers, wrong idle-reaper description, missing `/stop`, and undocumented scheduling/sentinel behavior.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Fix the density-limit numbers**

In the "Schedule Management" section, replace the density bullet:

> - **Density limits**: Schedule creation is globally rate-limited. At most 5 firings per rolling 24h window and 30 per rolling 7-day window across all agents combined. The check considers both projected future fire times (120-day horizon) and recent firing history.

with:

> - **Density limits**: Schedule creation is globally rate-limited. At most 8 firings per rolling 24h window and 40 per rolling 7-day window across all agents combined (`MAX_FIRINGS_PER_DAY` / `MAX_FIRINGS_PER_WEEK` in `mcp_server.py`). The check considers both projected future fire times (120-day horizon) and recent firing history.

- [ ] **Step 2: Document the humanizing scheduling behavior**

Add these bullets to the same "Schedule Management" bullet list, after the density bullet:

> - **Nighttime block**: Schedules that would fire between 2 AM and 5 AM (inclusive) are rejected at creation; exactly 1 AM and 6 AM are allowed.
> - **Firing jitter**: Every firing is intentionally delayed past its cron time — one-shots by ~8–30 min, recurring schedules by ~15–55 min (lognormal) — so agent activity doesn't land on round clock times.
> - **Human-like skips**: Recurring schedules deliberately skip ~20% of firings, weighted by day-of-week and streak momentum (`scheduler.py`). A missed recurring firing is a feature, not a bug — don't "fix" it. Skips are recorded in firing history with `"skipped": true`.

- [ ] **Step 3: Fix the Commands section**

Replace:

> - `/clear` — stops agent process for current channel, clears session (next message starts fresh)
> - `/clear {agent}` — same, but targets a specific agent by name

with:

> - `/clear` — stops agent process for current channel, clears session (next message starts fresh)
> - `/clear {agent}` — same, but targets a specific agent by name (works from any bot channel)
> - `/stop` — stops the current channel's agent process without clearing its session (next message resumes)

- [ ] **Step 4: Document the response sentinels in Message Flow**

Add after step 7 ("Response chunked at newline boundaries…") in the "Message Flow" section:

> The response text supports three sentinels: `[REPLY]` — only text after the last occurrence is posted (earlier text is treated as scratch); `[NO_POST]` — a scheduled task's response is suppressed entirely (scheduled runs only); `[FILE:/path]` — the file at that container path is attached to the Discord message and the marker removed.

- [ ] **Step 5: Fix the idle-reaper design note**

In "Key Design Decisions", replace:

> - **Idle reaper**: Background task kills agent processes after 10 minutes of inactivity.

(keeping the rest of that bullet's text about OAuth races and `_reaped`) with:

> - **Idle reaper**: Background task kills agent processes after a randomized lognormal idle threshold (median ~40 min, capped at 6 h, per-process; see `AgentProcess.reap_threshold`). A `can_stop` hook can veto the reap, up to a pin cap (`CLAUB_MAX_PIN_SECS`, default 48 h).

- [ ] **Step 6: Commit**

```bash
cd /Users/you/Claude
git add CLAUDE.md
git commit -m "docs: sync CLAUDE.md with actual density limits, reaper, commands, and sentinels"
```

---

### Task 13: Final verification

- [ ] **Step 1: Full test suite**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: all PASS.

- [ ] **Step 2: Container build + boot smoke test**

Run: `cd /Users/you/Claude && docker compose build && docker compose up -d && sleep 15 && docker compose logs --tail 30`
Expected: log shows "Discord connected as …", "Scheduler started with N jobs", "Starting MCP server on 127.0.0.1:9400", and no tracebacks.

- [ ] **Step 3: Live smoke test of the changed surface**

Send `/clear journalist` (or any non-main agent) in the main Discord channel — expect "Agent `journalist` reset." Then send a normal message in any agent channel and confirm a reply arrives.

- [ ] **Step 4: Merge decision**

Work is on `review-fixes`. Use the superpowers:finishing-a-development-branch skill to decide merge vs. PR vs. further review.
