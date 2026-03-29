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
