from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from claude_assistant.firing_history import FiringHistory
from claude_assistant.schedule_store import ScheduleStore

log = logging.getLogger(__name__)

# --- Human-like skip model for recurring schedules ---
# Target: ~30% overall skip rate, but distributed like real human habits.
#
# Three factors combine:
# 1. Day-of-week weight: weekends and Mondays are lazier
# 2. Streak momentum: consecutive fires lower skip chance, consecutive skips raise it
# 3. Beta-distributed randomness: heavier tails than uniform, mimics real variability
#
# The base skip probability is scaled by day-of-week, then shifted by streak,
# then a beta-distributed roll determines the outcome.

# Day-of-week multipliers on skip probability (Mon=0 .. Sun=6)
# Midweek is most disciplined; weekends and Monday are worst
_DAY_SKIP_WEIGHT = {
    0: 1.35,  # Monday — rough start
    1: 0.80,  # Tuesday — in the groove
    2: 0.75,  # Wednesday — peak discipline
    3: 0.85,  # Thursday — slight fade
    4: 1.10,  # Friday — winding down
    5: 1.40,  # Saturday — weekend
    6: 1.30,  # Sunday — weekend but some prep energy
}

_BASE_SKIP_PROB = 0.19  # tuned so weighted average across days ≈ 0.20
_STREAK_SHIFT = 0.03    # per consecutive outcome, shift probability this much
_MAX_STREAK_EFFECT = 0.12  # cap the streak shift
_BETA_ALPHA = 2.0       # beta distribution shape — moderate left skew
_BETA_BETA = 5.0        # gives a mean around 0.29, heavier left tail


def _should_skip_human(day_of_week: int, streak: int) -> bool:
    """Decide whether to skip a recurring schedule firing.

    Args:
        day_of_week: 0=Monday .. 6=Sunday
        streak: positive = consecutive fires, negative = consecutive skips
    """
    # Base probability adjusted for day of week
    p = _BASE_SKIP_PROB * _DAY_SKIP_WEIGHT.get(day_of_week, 1.0)

    # Streak momentum: firing streaks reduce skip chance, skip streaks increase it
    shift = min(abs(streak), int(_MAX_STREAK_EFFECT / _STREAK_SHIFT)) * _STREAK_SHIFT
    if streak > 0:
        p -= shift  # been consistent → less likely to skip
    elif streak < 0:
        p += shift  # been skipping → more likely to keep skipping

    p = max(0.05, min(0.60, p))  # clamp to reasonable range

    # Beta-distributed roll — heavier tails than uniform
    roll = random.betavariate(_BETA_ALPHA, _BETA_BETA)
    return roll < p

JITTER_MEDIAN_LOW = 480    # 8 minutes in seconds
JITTER_MEDIAN_HIGH = 720   # 12 minutes in seconds
JITTER_SIGMA = 0.4         # lognormal spread parameter
JITTER_MAX = 1800          # hard cap in seconds (30min)

# Recurring schedules get much wider jitter so firings feel natural
RECURRING_JITTER_MEDIAN_LOW = 900    # 15 minutes in seconds
RECURRING_JITTER_MEDIAN_HIGH = 1500  # 25 minutes in seconds
RECURRING_JITTER_SIGMA = 0.6        # wider lognormal spread
RECURRING_JITTER_MAX = 3300          # hard cap 55 minutes


def lognormal_jitter(sigma: float = JITTER_SIGMA, max_delay: float = JITTER_MAX) -> float:
    """Return a lognormal-distributed delay in seconds, clamped to *max_delay*.

    The median is randomized uniformly between 8 and 12 minutes on each call.
    Used for one-shot schedules.
    """
    import math
    median = random.uniform(JITTER_MEDIAN_LOW, JITTER_MEDIAN_HIGH)
    mu = math.log(median)
    return min(random.lognormvariate(mu, sigma), max_delay)


def recurring_jitter() -> float:
    """Return a wide lognormal-distributed delay for recurring schedules.

    Median randomly chosen between 15-25 minutes with high variance.
    Most firings land 10-40 min after the cron time, occasionally up to ~55 min.
    """
    import math
    median = random.uniform(RECURRING_JITTER_MEDIAN_LOW, RECURRING_JITTER_MEDIAN_HIGH)
    mu = math.log(median)
    return min(random.lognormvariate(mu, RECURRING_JITTER_SIGMA), RECURRING_JITTER_MAX)


ScheduleCallback = Callable[[str, str], Awaitable[None]]


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
        # Per-schedule streak tracker for human-like skip model.
        # Positive = consecutive fires, negative = consecutive skips.
        # Keyed by (agent_name, entry_id). In-memory only — resets on restart.
        self._streaks: dict[tuple[str, str], int] = defaultdict(int)

        for agent_name, entries in store.all().items():
            if valid_agents is not None and agent_name not in valid_agents:
                log.warning("Skipping orphaned schedules for agent %s", agent_name)
                continue
            for entry in entries:
                self._add_apscheduler_job(agent_name, entry)

    def _add_apscheduler_job(self, agent_name: str, entry: dict) -> None:
        job_id = f"{agent_name}_{entry['id']}"
        if entry.get("one_shot"):
            run_fn = self._run_one_shot
            args = [agent_name, entry["id"], entry["cron"], entry["prompt"]]
        else:
            run_fn = self._run
            args = [agent_name, entry["id"], entry["cron"], entry["prompt"]]
        self._scheduler.add_job(
            run_fn,
            trigger=CronTrigger.from_crontab(entry["cron"]),
            args=args,
            id=job_id,
            name=f"{agent_name}: {entry['prompt'][:50]}",
        )

    def add_job(
        self, agent_name: str, entry_id: str, cron: str, prompt: str, *, one_shot: bool
    ) -> None:
        entry = {"id": entry_id, "cron": cron, "prompt": prompt, "one_shot": one_shot}
        self._add_apscheduler_job(agent_name, entry)

    def remove_job(self, agent_name: str, entry_id: str) -> None:
        job_id = f"{agent_name}_{entry_id}"
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            log.debug("Job %s not found in scheduler", job_id)

    async def _run(self, agent_name: str, entry_id: str, cron: str, prompt: str) -> None:
        key = (agent_name, entry_id)
        day = datetime.now().weekday()  # 0=Mon .. 6=Sun
        streak = self._streaks[key]

        if _should_skip_human(day, streak):
            self._streaks[key] = min(streak, 0) - 1  # extend skip streak
            log.info("Skipping scheduled task for %s (day=%d, streak=%d → skip)",
                     agent_name, day, streak)
            if self._history:
                self._history.record(agent_name, entry_id, cron, prompt,
                                     one_shot=False, skipped=True)
            return

        self._streaks[key] = max(streak, 0) + 1  # extend fire streak
        jitter = recurring_jitter()
        log.info("Scheduled task for %s — delaying %.0fs", agent_name, jitter)
        await asyncio.sleep(jitter)
        log.info("Scheduled task firing for %s", agent_name)
        now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        prefixed = f"[scheduled — current time: {now}] {prompt}"
        await self._callback(agent_name, prefixed)
        if self._history:
            self._history.record(agent_name, entry_id, cron, prompt, one_shot=False)

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

    def get_jobs(self) -> list:
        return self._scheduler.get_jobs()

    def start(self) -> None:
        self._scheduler.start()
        log.info("Scheduler started with %d jobs", len(self.get_jobs()))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
