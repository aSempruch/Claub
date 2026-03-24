from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path


class ScheduleStore:
    """Atomic JSON persistence for agent schedules.

    Data format: {"agent_name": [{"id": "abc123", "cron": "...", "prompt": "...", "one_shot": bool}]}
    All mutations should be guarded by the async lock via `async with store.lock:`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, list[dict]] = {}
        self.lock = asyncio.Lock()
        if self._path.exists():
            self._data = json.loads(self._path.read_text())

    def list(self, agent: str) -> list[dict]:
        return list(self._data.get(agent, []))

    def all(self) -> dict[str, list[dict]]:
        return {k: list(v) for k, v in self._data.items()}

    def create(
        self, agent: str, *, cron: str, prompt: str, one_shot: bool
    ) -> dict:
        entry_id = self._generate_id()
        entry = {"id": entry_id, "cron": cron, "prompt": prompt, "one_shot": one_shot}
        self._data.setdefault(agent, []).append(entry)
        self._save()
        return entry

    def delete(self, agent: str, entry_id: str) -> bool:
        entries = self._data.get(agent, [])
        for i, entry in enumerate(entries):
            if entry["id"] == entry_id:
                entries.pop(i)
                if not entries:
                    del self._data[agent]
                self._save()
                return True
        return False

    def _generate_id(self) -> str:
        all_ids = {e["id"] for entries in self._data.values() for e in entries}
        while True:
            entry_id = os.urandom(3).hex()
            if entry_id not in all_ids:
                return entry_id

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
