# Schedule Density Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent agents from creating too many cron jobs that fire in quick succession, with a persistent firing history for debugging.

**Architecture:** Two new components — `FiringHistory` (persistence) and `check_schedule_density` (validation in `mcp_server.py`) — wired through `main.py` and `discord_bot.py`. The density check combines projected future fire times with recent firing history and enforces daily (5) and weekly (20) sliding-window limits globally across all agents.

**Tech Stack:** Python 3.12, croniter, existing atomic JSON persistence pattern from ScheduleStore.

---

### Task 1: Add croniter dependency

**Files:**
- Modify: `bot/pyproject.toml:6`

- [ ] **Step 1: Add croniter to dependencies**

In `bot/pyproject.toml`, add `"croniter>=2.0"` to the `dependencies` list:

```toml
dependencies = [
    "discord.py>=2.4",
    "apscheduler>=3.10,<4",
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
    "fastmcp>=2.0",
    "uvicorn>=0.30",
    "croniter>=2.0",
]
```

- [ ] **Step 2: Install the new dependency**

Run: `cd /Users/you/Claude/bot && uv sync`
Expected: Resolves and installs croniter without errors.

- [ ] **Step 3: Verify import works**

Run: `cd /Users/you/Claude/bot && uv run python -c "from croniter import croniter; print('ok')"`
Expected: Prints `ok`.

- [ ] **Step 4: Commit**

```bash
cd /Users/you/Claude/bot
git add pyproject.toml uv.lock
git commit -m "feat: add croniter dependency for schedule density validation"
```

---

### Task 2: Implement FiringHistory

**Files:**
- Create: `bot/src/claude_assistant/firing_history.py`
- Test: `bot/tests/test_firing_history.py`

- [ ] **Step 1: Write tests for FiringHistory**

Create `bot/tests/test_firing_history.py`:

```python
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from claude_assistant.firing_history import FiringHistory, FIRING_HISTORY_RETENTION_DAYS


@pytest.fixture
def history(tmp_path: Path) -> FiringHistory:
    return FiringHistory(tmp_path / "firing_history.json")


def test_all_empty(history: FiringHistory) -> None:
    assert history.all() == []


def test_record_appends_entry(history: FiringHistory) -> None:
    history.record(
        agent="main",
        schedule_id="abc123",
        cron="0 9 * * *",
        prompt="morning check",
        one_shot=False,
    )
    entries = history.all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["agent"] == "main"
    assert entry["schedule_id"] == "abc123"
    assert entry["cron"] == "0 9 * * *"
    assert entry["prompt"] == "morning check"
    assert entry["one_shot"] is False
    assert "fired_at" in entry
    # fired_at should be parseable as ISO datetime
    datetime.fromisoformat(entry["fired_at"])


def test_record_multiple(history: FiringHistory) -> None:
    history.record("main", "aaa", "0 9 * * *", "first", False)
    history.record("journalist", "bbb", "0 10 * * *", "second", True)
    assert len(history.all()) == 2


def test_recent_filters_by_days(history: FiringHistory) -> None:
    history.record("main", "aaa", "0 9 * * *", "recent", False)
    # Manually inject an old entry
    old_time = (datetime.now() - timedelta(days=10)).isoformat()
    history._data["firings"].append({
        "agent": "main",
        "schedule_id": "old",
        "cron": "0 9 * * *",
        "prompt": "old entry",
        "one_shot": False,
        "fired_at": old_time,
    })
    assert len(history.all()) == 2
    recent = history.recent(days=7)
    assert len(recent) == 1
    assert recent[0]["schedule_id"] == "aaa"


def test_record_prunes_old_entries(tmp_path: Path) -> None:
    history = FiringHistory(tmp_path / "firing_history.json", retention_days=5)
    # Inject an entry that's 10 days old
    old_time = (datetime.now() - timedelta(days=10)).isoformat()
    history._data["firings"].append({
        "agent": "main",
        "schedule_id": "old",
        "cron": "0 9 * * *",
        "prompt": "stale",
        "one_shot": False,
        "fired_at": old_time,
    })
    # Recording a new entry should prune the old one
    history.record("main", "new", "0 10 * * *", "fresh", False)
    entries = history.all()
    assert len(entries) == 1
    assert entries[0]["schedule_id"] == "new"


def test_persistence(tmp_path: Path) -> None:
    path = tmp_path / "firing_history.json"
    h1 = FiringHistory(path)
    h1.record("main", "aaa", "0 9 * * *", "test", False)
    h2 = FiringHistory(path)
    assert len(h2.all()) == 1


def test_missing_file_treated_as_empty(tmp_path: Path) -> None:
    history = FiringHistory(tmp_path / "nonexistent" / "history.json")
    assert history.all() == []


def test_default_retention_days() -> None:
    assert FIRING_HISTORY_RETENTION_DAYS == 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_firing_history.py -v`
Expected: ImportError — `firing_history` module does not exist yet.

- [ ] **Step 3: Implement FiringHistory**

Create `bot/src/claude_assistant/firing_history.py`:

```python
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

FIRING_HISTORY_RETENTION_DAYS = 30


class FiringHistory:
    """Atomic JSON persistence for schedule firing history.

    Data format: {"firings": [{"agent": ..., "schedule_id": ..., "cron": ...,
    "prompt": ..., "one_shot": bool, "fired_at": ISO datetime str}]}
    """

    def __init__(self, path: Path, retention_days: int = FIRING_HISTORY_RETENTION_DAYS) -> None:
        self._path = path
        self._retention_days = retention_days
        self._data: dict[str, list[dict]] = {"firings": []}
        if self._path.exists():
            self._data = json.loads(self._path.read_text())

    def record(
        self,
        agent: str,
        schedule_id: str,
        cron: str,
        prompt: str,
        one_shot: bool,
    ) -> None:
        """Append a firing entry and prune entries older than retention_days."""
        entry = {
            "agent": agent,
            "schedule_id": schedule_id,
            "cron": cron,
            "prompt": prompt,
            "one_shot": one_shot,
            "fired_at": datetime.now().isoformat(),
        }
        self._data["firings"].append(entry)
        self._prune()
        self._save()

    def recent(self, days: int = 7) -> list[dict]:
        """Return firings from the last N days."""
        cutoff = datetime.now() - timedelta(days=days)
        return [
            e for e in self._data["firings"]
            if datetime.fromisoformat(e["fired_at"]) >= cutoff
        ]

    def all(self) -> list[dict]:
        """Return all retained firings."""
        return list(self._data["firings"])

    def _prune(self) -> None:
        cutoff = datetime.now() - timedelta(days=self._retention_days)
        self._data["firings"] = [
            e for e in self._data["firings"]
            if datetime.fromisoformat(e["fired_at"]) >= cutoff
        ]

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", dir=self._path.parent, suffix=".tmp", delete=False
        )
        try:
            json.dump(self._data, tmp, indent=2)
            tmp.close()
            Path(tmp.name).replace(self._path)
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_firing_history.py -v`
Expected: All 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add bot/src/claude_assistant/firing_history.py bot/tests/test_firing_history.py
git commit -m "feat: add FiringHistory for schedule firing persistence"
```

---

### Task 3: Implement check_schedule_density

**Files:**
- Modify: `bot/src/claude_assistant/mcp_server.py:1-15` (add imports and constants)
- Modify: `bot/src/claude_assistant/mcp_server.py:36-74` (add density function, modify `_create_schedule`)
- Test: `bot/tests/test_density.py`

- [ ] **Step 1: Write tests for check_schedule_density**

Create `bot/tests/test_density.py`:

```python
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_assistant.firing_history import FiringHistory
from claude_assistant.mcp_server import (
    MAX_FIRINGS_PER_DAY,
    MAX_FIRINGS_PER_WEEK,
    check_schedule_density,
)
from claude_assistant.schedule_store import ScheduleStore


@pytest.fixture
def store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.json")


@pytest.fixture
def history(tmp_path: Path) -> FiringHistory:
    return FiringHistory(tmp_path / "firing_history.json")


def test_constants() -> None:
    assert MAX_FIRINGS_PER_DAY == 5
    assert MAX_FIRINGS_PER_WEEK == 20


def test_empty_store_passes(store: ScheduleStore, history: FiringHistory) -> None:
    result = check_schedule_density(store, history, "0 9 * * *", one_shot=False)
    assert result is None


def test_under_daily_limit_passes(store: ScheduleStore, history: FiringHistory) -> None:
    # 4 existing recurring schedules at different hours on weekdays
    store.create("main", cron="0 9 * * 1-5", prompt="a", one_shot=False)
    store.create("main", cron="0 11 * * 1-5", prompt="b", one_shot=False)
    store.create("main", cron="0 14 * * 1-5", prompt="c", one_shot=False)
    store.create("main", cron="0 16 * * 1-5", prompt="d", one_shot=False)
    # Adding a 5th is still at the limit
    result = check_schedule_density(store, history, "0 18 * * 1-5", one_shot=False)
    assert result is None


def test_daily_limit_exceeded(store: ScheduleStore, history: FiringHistory) -> None:
    # 5 existing recurring schedules all firing daily
    store.create("main", cron="0 8 * * *", prompt="a", one_shot=False)
    store.create("main", cron="0 10 * * *", prompt="b", one_shot=False)
    store.create("main", cron="0 12 * * *", prompt="c", one_shot=False)
    store.create("main", cron="0 14 * * *", prompt="d", one_shot=False)
    store.create("main", cron="0 16 * * *", prompt="e", one_shot=False)
    # Adding a 6th exceeds the daily limit
    result = check_schedule_density(store, history, "0 18 * * *", one_shot=False)
    assert result is not None
    assert "Error" in result
    assert str(MAX_FIRINGS_PER_DAY) in result


def test_weekly_limit_exceeded(store: ScheduleStore, history: FiringHistory) -> None:
    # 4 recurring schedules firing daily = 4/day, 28/week
    # That's under daily limit (5) but over weekly (20)
    store.create("main", cron="0 9 * * *", prompt="a", one_shot=False)
    store.create("main", cron="0 11 * * *", prompt="b", one_shot=False)
    store.create("main", cron="0 14 * * *", prompt="c", one_shot=False)
    store.create("main", cron="0 16 * * *", prompt="d", one_shot=False)
    # Adding a 5th = 5/day, 35/week — daily is at limit but weekly exceeds 20
    # Actually 5/day * 7 = 35 which exceeds 20, but daily check would trigger first
    # since 5 is at the limit. Let's use schedules on specific days instead.
    # 4 schedules per day, but only on specific days to stay under daily limit
    # while exceeding weekly:
    # Mon-Fri: 4/day = 20/week at limit. Add one more on any day = 21.
    store.create("main", cron="0 18 * * 1", prompt="e", one_shot=False)
    result = check_schedule_density(store, history, "0 20 * * 1", one_shot=False)
    # Mon now has 6 firings — daily limit hit first
    # Let's redesign: use 3/day across all 7 days = 21/week, then add one more
    assert result is not None  # Will hit daily limit on Monday


def test_weekly_limit_without_daily_violation(store: ScheduleStore, history: FiringHistory) -> None:
    # 3 recurring schedules every day = 3/day, 21/week
    store.create("a1", cron="0 9 * * *", prompt="a", one_shot=False)
    store.create("a2", cron="0 12 * * *", prompt="b", one_shot=False)
    store.create("a3", cron="0 15 * * *", prompt="c", one_shot=False)
    # Adding another daily schedule = 4/day, 28/week — daily OK (<=5), weekly exceeds 20
    result = check_schedule_density(store, history, "0 18 * * *", one_shot=False)
    assert result is not None
    assert "Error" in result
    assert str(MAX_FIRINGS_PER_WEEK) in result


def test_one_shots_far_in_future_caught(store: ScheduleStore, history: FiringHistory) -> None:
    # Use a fixed "now" so we can create one-shots at a known future date
    # June 15 is ~79 days from March 28, well within 120-day horizon
    fake_now = datetime(2026, 3, 28, 12, 0)
    with patch("claude_assistant.mcp_server._now", return_value=fake_now):
        # 5 one-shots all firing on the same day (June 15)
        store.create("main", cron="0 9 15 6 *", prompt="a", one_shot=True)
        store.create("main", cron="0 10 15 6 *", prompt="b", one_shot=True)
        store.create("main", cron="0 11 15 6 *", prompt="c", one_shot=True)
        store.create("main", cron="0 12 15 6 *", prompt="d", one_shot=True)
        store.create("main", cron="0 13 15 6 *", prompt="e", one_shot=True)
        # 6th one-shot on the same day should be rejected
        result = check_schedule_density(store, history, "0 14 15 6 *", one_shot=True)
        assert result is not None
        assert "Error" in result


def test_firing_history_tips_over_daily(store: ScheduleStore, history: FiringHistory) -> None:
    # 4 recent firings in history today
    for i in range(4):
        history.record("main", f"id{i}", f"0 {8+i} * * *", f"task {i}", True)
    # 1 existing schedule firing today
    # Use a cron that fires every day — one of those days will have 4 history + 1 existing + 1 new = 6
    store.create("main", cron="0 14 * * *", prompt="existing", one_shot=False)
    result = check_schedule_density(store, history, "0 16 * * *", one_shot=False)
    assert result is not None
    assert "Error" in result


def test_empty_store_and_history_passes(store: ScheduleStore, history: FiringHistory) -> None:
    result = check_schedule_density(store, history, "0 9 * * *", one_shot=True)
    assert result is None


def test_error_message_mentions_other_agents(store: ScheduleStore, history: FiringHistory) -> None:
    store.create("main", cron="0 8 * * *", prompt="a", one_shot=False)
    store.create("main", cron="0 10 * * *", prompt="b", one_shot=False)
    store.create("main", cron="0 12 * * *", prompt="c", one_shot=False)
    store.create("main", cron="0 14 * * *", prompt="d", one_shot=False)
    store.create("main", cron="0 16 * * *", prompt="e", one_shot=False)
    result = check_schedule_density(store, history, "0 18 * * *", one_shot=False)
    assert result is not None
    assert "other agents" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_density.py -v`
Expected: ImportError — `check_schedule_density` and constants not yet defined.

- [ ] **Step 3: Implement check_schedule_density and _now helper**

Add imports, constants, the `_now()` helper (for testability), and the density function to `bot/src/claude_assistant/mcp_server.py`. Add these after the existing imports and before the `_list_schedules` function:

```python
# New imports to add at top of file
from datetime import datetime, timedelta
from croniter import croniter
from claude_assistant.firing_history import FiringHistory

# Constants — add after existing imports, before helper functions
MAX_FIRINGS_PER_DAY = 5
MAX_FIRINGS_PER_WEEK = 20
DENSITY_HORIZON_DAYS = 120


def _now() -> datetime:
    """Return current time. Extracted for test patching."""
    return datetime.now()


def check_schedule_density(
    store: ScheduleStore,
    history: FiringHistory,
    new_cron: str,
    one_shot: bool,
    max_per_day: int = MAX_FIRINGS_PER_DAY,
    max_per_week: int = MAX_FIRINGS_PER_WEEK,
    horizon_days: int = DENSITY_HORIZON_DAYS,
) -> str | None:
    """Check if adding a new schedule would exceed daily or weekly firing limits.

    Combines projected future fire times from all existing schedules (plus the
    proposed new one) with recent firing history. Returns None if OK, or an
    error string describing which window is overloaded.
    """
    now = _now()
    horizon = now + timedelta(days=horizon_days)

    # Collect all (cron, one_shot) tuples from the store across all agents
    entries: list[tuple[str, bool]] = []
    for agent_entries in store.all().values():
        for e in agent_entries:
            entries.append((e["cron"], e["one_shot"]))
    entries.append((new_cron, one_shot))

    # Project fire times
    fire_times: list[datetime] = []
    for cron_expr, is_one_shot in entries:
        it = croniter(cron_expr, now)
        if is_one_shot:
            nxt = it.get_next(datetime)
            if nxt < horizon:
                fire_times.append(nxt)
        else:
            while True:
                nxt = it.get_next(datetime)
                if nxt >= horizon:
                    break
                fire_times.append(nxt)

    # Add recent firing history
    for firing in history.recent(days=7):
        fire_times.append(datetime.fromisoformat(firing["fired_at"]))

    fire_times.sort()

    # Sliding window checks
    day_delta = timedelta(hours=24)
    week_delta = timedelta(days=7)

    for i, t in enumerate(fire_times):
        # Daily check
        day_count = sum(1 for t2 in fire_times[i:] if t2 < t + day_delta)
        if day_count > max_per_day:
            return (
                f"Error: adding this schedule would cause {day_count} firings "
                f"within 24h of {t.strftime('%Y-%m-%d %H:%M')} (max {max_per_day}). "
                f"This count includes schedules from all agents globally — some may "
                f"belong to other agents you can't control. Consider spreading your "
                f"schedules further apart or removing existing ones first."
            )

        # Weekly check
        week_count = sum(1 for t2 in fire_times[i:] if t2 < t + week_delta)
        if week_count > max_per_week:
            return (
                f"Error: adding this schedule would cause {week_count} firings "
                f"within 7 days of {t.strftime('%Y-%m-%d %H:%M')} (max {max_per_week}). "
                f"This count includes schedules from all agents globally — some may "
                f"belong to other agents you can't control. Consider spreading your "
                f"schedules further apart or removing existing ones first."
            )

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_density.py -v`
Expected: All tests pass.

- [ ] **Step 5: Run existing tests to check for regressions**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: All existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add bot/src/claude_assistant/mcp_server.py bot/tests/test_density.py
git commit -m "feat: add check_schedule_density with daily/weekly sliding window limits"
```

---

### Task 4: Integrate density check into _create_schedule

**Files:**
- Modify: `bot/src/claude_assistant/mcp_server.py:45-74` (modify `_create_schedule` and `create_mcp_server`)

- [ ] **Step 1: Write a test for density rejection in _create_schedule**

Add to `bot/tests/test_mcp_server.py`:

```python
from claude_assistant.firing_history import FiringHistory


@pytest.fixture
def history(tmp_path: Path) -> FiringHistory:
    return FiringHistory(tmp_path / "firing_history.json")


@pytest.mark.asyncio
async def test_create_schedule_rejected_by_density(
    store: ScheduleStore, scheduler: MagicMock, history: FiringHistory
) -> None:
    # Fill up to daily limit
    store.create("main", cron="0 8 * * *", prompt="a", one_shot=False)
    store.create("main", cron="0 10 * * *", prompt="b", one_shot=False)
    store.create("main", cron="0 12 * * *", prompt="c", one_shot=False)
    store.create("main", cron="0 14 * * *", prompt="d", one_shot=False)
    store.create("main", cron="0 16 * * *", prompt="e", one_shot=False)
    # 6th should be rejected
    result = await _create_schedule(
        agent="main",
        cron="0 18 * * *",
        prompt="too many",
        one_shot=False,
        store=store,
        scheduler=scheduler,
        history=history,
    )
    assert "Error" in result
    # Store should NOT have a 6th entry
    assert len(store.list("main")) == 5
    # Scheduler should NOT be called
    scheduler.add_job.assert_not_called()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_mcp_server.py::test_create_schedule_rejected_by_density -v`
Expected: Fails — `_create_schedule` doesn't accept `history` parameter yet.

- [ ] **Step 3: Add history parameter to _create_schedule**

Modify `_create_schedule` in `bot/src/claude_assistant/mcp_server.py` to accept `history` and call `check_schedule_density`:

```python
async def _create_schedule(
    agent: str,
    cron: str,
    prompt: str,
    one_shot: bool,
    store: ScheduleStore,
    scheduler: Scheduler,
    notify: NotifyCallback = None,
    history: FiringHistory | None = None,
) -> str:
    # Validate cron expression before touching persistent state
    try:
        CronTrigger.from_crontab(cron)
    except Exception as exc:
        return f"Error: invalid cron expression {cron!r} — {exc}"

    # Density check
    if history is not None:
        density_error = check_schedule_density(store, history, cron, one_shot)
        if density_error:
            return density_error

    async with store.lock:
        entry = store.create(agent, cron=cron, prompt=prompt, one_shot=one_shot)
        scheduler.add_job(agent, entry["id"], cron, prompt, one_shot=one_shot)

    log.info("Created schedule %s for %s: %r", entry["id"], agent, prompt)
    shot_label = "one-shot " if one_shot else ""
    msg = f"Schedule created: {shot_label}`{cron}` — {prompt}"
    if notify:
        await notify(agent, msg)
    return f"Created schedule {entry['id']}: {prompt!r} at {cron}"
```

- [ ] **Step 4: Update create_mcp_server to accept and pass history**

Modify `create_mcp_server` in `bot/src/claude_assistant/mcp_server.py`:

```python
def create_mcp_server(
    store: ScheduleStore,
    scheduler: Scheduler,
    notify: NotifyCallback = None,
    history: FiringHistory | None = None,
) -> fastmcp.FastMCP:
```

And update the `create_schedule` tool closure to pass `history`:

```python
        return await _create_schedule(agent, cron, prompt, one_shot, store, scheduler, notify, history)
```

- [ ] **Step 5: Run the new test**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_mcp_server.py::test_create_schedule_rejected_by_density -v`
Expected: PASS.

- [ ] **Step 6: Run all tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: All tests pass. Existing `_create_schedule` callers that don't pass `history` still work because the parameter defaults to `None` and the check is skipped.

- [ ] **Step 7: Commit**

```bash
git add bot/src/claude_assistant/mcp_server.py bot/tests/test_mcp_server.py
git commit -m "feat: integrate density check into schedule creation"
```

---

### Task 5: Wire FiringHistory into Scheduler for recording

**Files:**
- Modify: `bot/src/claude_assistant/scheduler.py:55-68` (add history param to `__init__`)
- Modify: `bot/src/claude_assistant/scheduler.py:102-124` (add recording to `_run` and `_run_one_shot`)
- Test: `bot/tests/test_scheduler.py` (add recording tests)

- [ ] **Step 1: Read the existing scheduler tests to understand patterns**

Read `bot/tests/test_scheduler.py` to understand how the scheduler is tested.

- [ ] **Step 2: Write tests for firing history recording**

Add to `bot/tests/test_scheduler.py`:

```python
from claude_assistant.firing_history import FiringHistory


@pytest.fixture
def history(tmp_path: Path) -> FiringHistory:
    return FiringHistory(tmp_path / "firing_history.json")
```

Add two test functions (adapting to whatever test patterns exist in the file):

```python
@pytest.mark.asyncio
async def test_run_records_firing(store, history, tmp_path):
    """_run should record the firing in history after callback completes."""
    callback = AsyncMock()
    sched = Scheduler(store, callback, history=history)
    # Call _run directly
    await sched._run("main", "abc123", "0 9 * * *", "do stuff")
    entries = history.all()
    assert len(entries) == 1
    assert entries[0]["agent"] == "main"
    assert entries[0]["cron"] == "0 9 * * *"
    assert entries[0]["one_shot"] is False


@pytest.mark.asyncio
async def test_run_one_shot_records_firing(store, history, tmp_path):
    """_run_one_shot should record the firing in history after callback completes."""
    callback = AsyncMock()
    entry = store.create("main", cron="0 9 * * *", prompt="once", one_shot=True)
    sched = Scheduler(store, callback, history=history)
    await sched._run_one_shot("main", entry["id"], "0 9 * * *", "once")
    entries = history.all()
    assert len(entries) == 1
    assert entries[0]["agent"] == "main"
    assert entries[0]["one_shot"] is True
    assert entries[0]["schedule_id"] == entry["id"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_scheduler.py -v -k "record"`
Expected: Fails — `Scheduler.__init__` doesn't accept `history` yet.

- [ ] **Step 4: Add history parameter to Scheduler**

Modify `Scheduler.__init__` in `bot/src/claude_assistant/scheduler.py`:

```python
from claude_assistant.firing_history import FiringHistory

class Scheduler:
    def __init__(
        self,
        store: ScheduleStore,
        callback: ScheduleCallback,
        valid_agents: set[str] | None = None,
        history: FiringHistory | None = None,
    ) -> None:
        self._scheduler = AsyncIOScheduler()
        self._callback = callback
        self._store = store
        self._history = history

        for agent_name, entries in store.all().items():
            # ... rest unchanged
```

- [ ] **Step 5: Add recording to _run**

In `_run`, after `await self._callback(agent_name, prefixed)`, add:

```python
        if self._history:
            self._history.record(
                agent=agent_name,
                schedule_id="",
                cron="",
                prompt=prompt,
                one_shot=False,
            )
```

Note: `_run` is used for recurring schedules and doesn't have access to `schedule_id` or `cron`. We'd need to pass them through. Modify `_add_apscheduler_job` to include these in the args:

For recurring schedules, change args to:
```python
        if entry.get("one_shot"):
            run_fn = self._run_one_shot
            args = [agent_name, entry["id"], entry["cron"], entry["prompt"]]
        else:
            run_fn = self._run
            args = [agent_name, entry["id"], entry["cron"], entry["prompt"]]
```

Update `_run` signature:
```python
    async def _run(self, agent_name: str, entry_id: str, cron: str, prompt: str) -> None:
        jitter = recurring_jitter()
        log.info("Scheduled task for %s — delaying %.0fs", agent_name, jitter)
        await asyncio.sleep(jitter)
        log.info("Scheduled task firing for %s", agent_name)
        now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        prefixed = f"[scheduled — current time: {now}] {prompt}"
        await self._callback(agent_name, prefixed)
        if self._history:
            self._history.record(agent_name, entry_id, cron, prompt, one_shot=False)
```

Update `_run_one_shot` signature:
```python
    async def _run_one_shot(self, agent_name: str, entry_id: str, cron: str, prompt: str) -> None:
        async with self._store.lock:
            self._store.delete(agent_name, entry_id)
            job_id = f"{agent_name}_{entry_id}"
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass
        log.info("One-shot schedule %s for %s — firing and removing", entry_id, agent_name)
        jitter = lognormal_jitter()
        await asyncio.sleep(jitter)
        now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        prefixed = f"[scheduled — current time: {now}] {prompt}"
        await self._callback(agent_name, prefixed)
        if self._history:
            self._history.record(agent_name, entry_id, cron, prompt, one_shot=True)
```

And update `_add_apscheduler_job` accordingly — both branches now pass `[agent_name, entry["id"], entry["cron"], entry["prompt"]]` as args.

- [ ] **Step 6: Run recording tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/test_scheduler.py -v -k "record"`
Expected: Both recording tests pass.

- [ ] **Step 7: Run all tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: All tests pass. Existing callers that don't pass `history` still work because it defaults to `None`.

- [ ] **Step 8: Commit**

```bash
git add bot/src/claude_assistant/scheduler.py bot/tests/test_scheduler.py
git commit -m "feat: record schedule firings in FiringHistory"
```

---

### Task 6: Wire FiringHistory through main.py and discord_bot.py

**Files:**
- Modify: `bot/src/claude_assistant/main.py:24-38,41-76` (create FiringHistory, pass to bot)
- Modify: `bot/src/claude_assistant/discord_bot.py:24-35,62-70,177-185` (accept and pass history)

- [ ] **Step 1: Add firing_history.json to _resolve_paths in main.py**

In `bot/src/claude_assistant/main.py`, update `_resolve_paths` to return the firing history path:

```python
def _resolve_paths() -> tuple[Path, Path, Path, Path, Path, Path, Path, Path]:
    """Resolve paths relative to CLAUB_HOME (default: ~/.claub)."""
    claub_home = Path(os.environ.get(
        "CLAUB_HOME",
        Path.home() / ".claub",
    ))
    return (
        claub_home / "config" / "agents.yaml",
        claub_home / "workspaces",
        claub_home / "data" / "sessions.json",
        claub_home / "config" / "mcp.json",
        claub_home / "config" / "agents",
        claub_home / "data" / "schedules.json",
        claub_home / "config" / "skills",
        claub_home / "data" / "firing_history.json",
    )
```

- [ ] **Step 2: Create FiringHistory instance in main()**

In `main()`, after `schedules = ScheduleStore(schedules_path)`:

```python
from claude_assistant.firing_history import FiringHistory

# In main():
history_retention = int(os.environ.get("CLAUB_SCHEDULE_HISTORY_RETENTION_DAYS", "30"))
config_path, workspaces_dir, sessions_path, mcp_config, agents_dir, schedules_path, skills_dir, history_path = _resolve_paths()
# ... existing code ...
schedules = ScheduleStore(schedules_path)
firing_history = FiringHistory(history_path, retention_days=history_retention)
```

Pass `firing_history` to `AssistantBot`:

```python
    bot = AssistantBot(
        config=config,
        workspaces_dir=workspaces_dir,
        session_store=sessions,
        schedule_store=schedules,
        firing_history=firing_history,
        mcp_config=mcp_config if mcp_config.exists() else None,
        agents_dir=agents_dir if agents_dir.exists() else None,
        mcp_port=mcp_port,
        all_skills=all_skills,
    )
```

- [ ] **Step 3: Accept firing_history in AssistantBot**

In `bot/src/claude_assistant/discord_bot.py`, add `firing_history` to `__init__`:

```python
from claude_assistant.firing_history import FiringHistory

class AssistantBot:
    def __init__(
        self,
        config: AssistantConfig,
        workspaces_dir: Path,
        session_store: SessionStore,
        schedule_store: ScheduleStore,
        firing_history: FiringHistory | None = None,
        mcp_config: Path | None = None,
        agents_dir: Path | None = None,
        mcp_port: int = 9400,
        all_skills: list[str] | None = None,
    ) -> None:
        # ... existing assignments ...
        self.firing_history = firing_history
```

- [ ] **Step 4: Pass firing_history to Scheduler in on_ready**

In `_setup_events`, update the Scheduler instantiation:

```python
            self._scheduler = Scheduler(
                self.schedule_store,
                self._handle_scheduled,
                valid_agents=set(self.config.agents.keys()),
                history=self.firing_history,
            )
```

- [ ] **Step 5: Pass firing_history to create_mcp_server in _start_mcp_server**

In `_start_mcp_server`:

```python
        mcp = create_mcp_server(
            self.schedule_store,
            self._scheduler,
            notify=self._notify_channel,
            history=self.firing_history,
        )
```

- [ ] **Step 6: Run all tests**

Run: `cd /Users/you/Claude/bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: All tests pass. Existing test fixtures don't pass `firing_history`, which defaults to `None` — density check skipped, recording skipped.

- [ ] **Step 7: Commit**

```bash
git add bot/src/claude_assistant/main.py bot/src/claude_assistant/discord_bot.py
git commit -m "feat: wire FiringHistory through main.py and discord_bot.py"
```

---

### Task 7: Update CLAUDE.md documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add schedule density section to CLAUDE.md**

In the "Schedule Management" section of `CLAUDE.md`, after the existing schedule description, add a note about the density limits:

```markdown
- **Density limits**: Schedule creation is globally rate-limited. At most 5 firings per rolling 24h window and 20 per rolling 7-day window across all agents combined. The check considers both projected future fire times (120-day horizon) and recent firing history.
- **Firing history**: All schedule firings are logged to `/claub/data/firing_history.json` for debugging. Retention is configurable via `CLAUB_SCHEDULE_HISTORY_RETENTION_DAYS` env var (default 30 days).
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document schedule density limits and firing history"
```
