#!/usr/bin/env python3
"""Set Chromium's native page zoom in each agent profile's Default/Preferences.

Reads the bridge config to find each agent's user_data_dir, then merges the
zoom keys into that profile's Preferences (preserving everything else).

Run after first creating a profile, or after adding a new agent. Idempotent.

Why native zoom (Preferences) instead of CSS zoom (init.js)?
  CSS `zoom` on documentElement breaks hCaptcha rendering — cross-origin
  iframes don't inherit it, so the captcha content lays out smaller than its
  parent-side iframe element, click-misaligned. Native zoom (Cmd+- equivalent)
  works in the compositor and propagates to iframes correctly.

Chromium zoom level math: zoom_level = log(factor) / log(1.2)
  80%: log(0.8)/log(1.2) ≈ -1.2238

Do NOT run while Chromium has the profile open — Chromium will overwrite
your changes on shutdown.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ZOOM_LEVEL = -1.2238  # 80%
DEFAULT_BRIDGE_CONFIG = Path.home() / "Library/Application Support/claub-playwright-bridge/config.json"


def apply(profile_dir: Path) -> None:
    prefs_path = profile_dir / "Default" / "Preferences"
    if prefs_path.exists():
        data = json.loads(prefs_path.read_text())
    else:
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        data = {}

    data.setdefault("profile", {})["default_zoom_level"] = ZOOM_LEVEL
    data.setdefault("partition", {})["default_zoom_level"] = {"x": ZOOM_LEVEL}

    prefs_path.write_text(json.dumps(data, separators=(",", ":")))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_BRIDGE_CONFIG,
        help=f"bridge config JSON (default: {DEFAULT_BRIDGE_CONFIG})",
    )
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text())
    for name, agent in sorted(cfg.get("agents", {}).items()):
        profile_dir = Path(agent["user_data_dir"])
        apply(profile_dir)
        print(f"OK: {name} ({profile_dir})")


if __name__ == "__main__":
    main()
