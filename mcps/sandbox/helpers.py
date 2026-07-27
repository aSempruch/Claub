"""Pure helpers for the sandbox MCP server — no I/O, unit-testable."""
from __future__ import annotations

import re

# Must start AND end with an alphanumeric so option flags (`-e`, `--pre`) and a
# bare `.` are rejected. Kept in lockstep with the bridge's copy, but that copy
# is the enforcing control — see validate_packages below.
PACKAGE_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(==[A-Za-z0-9][A-Za-z0-9._+!-]*)?$"
)


def validate_packages(packages: list[str]) -> list[str]:
    """Return *packages* unchanged if every name is a bare (optionally pinned)
    distribution name.

    CONVENIENCE ONLY — it saves the agent a round trip and gives a clearer
    error. The enforcing copy lives in the bridge (scripts/exec-bridge/
    helpers.py), because the bridge is the only component a compromised bot
    container cannot tamper with. Never treat this copy as the control, and
    never build an install command here — the MCP forwards names, the bridge
    builds the argv.
    """
    if not packages:
        raise ValueError("no packages given")
    for name in packages:
        if not PACKAGE_RE.match(name):
            raise ValueError(f"invalid package name: {name!r}")
    return packages


def truncate_tail(text: str, limit: int = 4000) -> str:
    """Tail-biased truncation with an explicit marker when content is dropped."""
    if len(text) <= limit:
        return text
    marker = f"[... {len(text) - limit} chars truncated ...]\n"
    return marker + text[-limit:]
