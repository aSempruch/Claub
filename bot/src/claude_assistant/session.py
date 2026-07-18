from __future__ import annotations

import json
import tempfile
from pathlib import Path


class SessionStore:
    """Per-agent session state: {"agent": {"session_id": ..., "model": ...}}."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict[str, str]] = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text())
            # Backwards compat (2026-07): pre-/model files stored bare
            # session-id strings. Safe to remove once all deployed
            # instances have rewritten sessions.json at least once.
            self._data = {
                agent: ({"session_id": value} if isinstance(value, str) else value)
                for agent, value in raw.items()
            }

    def get(self, agent: str) -> str | None:
        return self._data.get(agent, {}).get("session_id")

    def set(self, agent: str, session_id: str) -> None:
        self._data.setdefault(agent, {})["session_id"] = session_id
        self._save()

    def get_model(self, agent: str) -> str | None:
        return self._data.get(agent, {}).get("model")

    def set_model(self, agent: str, model: str) -> None:
        self._data.setdefault(agent, {})["model"] = model
        self._save()

    def clear_model(self, agent: str) -> None:
        record = self._data.get(agent)
        if record is None:
            return
        record.pop("model", None)
        if not record:
            self._data.pop(agent, None)
        self._save()

    def delete(self, agent: str) -> None:
        self._data.pop(agent, None)
        self._save()

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
