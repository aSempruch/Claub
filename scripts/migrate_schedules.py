#!/usr/bin/env python3
"""Migrate schedules from agents.yaml to schedules.json.

Usage: python scripts/migrate_schedules.py [CLAUB_HOME]
Default CLAUB_HOME: ~/.claub
"""

import json
import os
import sys
from pathlib import Path

import yaml


def main() -> None:
    claub_home = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".claub"
    agents_yaml = claub_home / "config" / "agents.yaml"
    schedules_json = claub_home / "data" / "schedules.json"

    if not agents_yaml.exists():
        print(f"agents.yaml not found at {agents_yaml}")
        sys.exit(1)

    with open(agents_yaml) as f:
        raw = yaml.safe_load(f)

    schedules: dict[str, list[dict]] = {}
    for name, agent in (raw.get("agents") or {}).items():
        for entry in agent.get("schedule") or []:
            crons = entry.get("cron", [])
            prompt = entry.get("prompt", "")
            for cron in crons:
                schedule_id = os.urandom(3).hex()
                schedules.setdefault(name, []).append({
                    "id": schedule_id,
                    "cron": cron,
                    "prompt": prompt,
                    "one_shot": False,
                })

    schedules_json.parent.mkdir(parents=True, exist_ok=True)
    with open(schedules_json, "w") as f:
        json.dump(schedules, f, indent=2)

    print(f"Migrated {sum(len(v) for v in schedules.values())} schedule(s) to {schedules_json}")
    for agent, entries in schedules.items():
        for e in entries:
            print(f"  {agent}: [{e['id']}] {e['cron']} — {e['prompt'][:60]}")

    print(f"\nNow remove 'schedule:' keys from {agents_yaml}")


if __name__ == "__main__":
    main()
