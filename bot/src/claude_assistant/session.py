from __future__ import annotations

import json
import tempfile
from pathlib import Path


class SessionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, str] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text())

    def get(self, agent: str) -> str | None:
        return self._data.get(agent)

    def set(self, agent: str, session_id: str) -> None:
        self._data[agent] = session_id
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
