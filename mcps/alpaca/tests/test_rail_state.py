import json
from pathlib import Path

from rails import RailConfig, RailState, load_state, save_state, update_high_water

CFG = RailConfig()


def test_missing_file_is_fresh_state(tmp_path):
    state, warning = load_state(tmp_path / "rail_state.json", "2026-08-03", CFG)
    assert state == RailState(date="2026-08-03", orders_today=0, high_water_mark=None)
    assert warning is None


def test_roundtrip(tmp_path):
    p = tmp_path / "rail_state.json"
    save_state(p, RailState(date="2026-08-03", orders_today=2, high_water_mark=105_000.0))
    state, warning = load_state(p, "2026-08-03", CFG)
    assert state.orders_today == 2
    assert state.high_water_mark == 105_000.0
    assert warning is None


def test_day_rollover_resets_counter_keeps_hwm(tmp_path):
    p = tmp_path / "rail_state.json"
    save_state(p, RailState(date="2026-08-03", orders_today=3, high_water_mark=105_000.0))
    state, _ = load_state(p, "2026-08-04", CFG)
    assert state == RailState(date="2026-08-04", orders_today=0, high_water_mark=105_000.0)


def test_corrupt_file_fails_closed_for_counter_open_for_hwm(tmp_path):
    p = tmp_path / "rail_state.json"
    p.write_text("{not json")
    state, warning = load_state(p, "2026-08-03", CFG)
    assert state.orders_today == CFG.max_orders_per_day  # budget exhausted
    assert state.high_water_mark is None  # will re-seed from current equity
    assert "corrupt" in warning


def test_update_high_water_rises_never_falls():
    s = RailState(date="2026-08-03", orders_today=0, high_water_mark=100_000.0)
    assert update_high_water(s, 110_000.0).high_water_mark == 110_000.0
    assert update_high_water(s, 90_000.0).high_water_mark == 100_000.0
    seeded = update_high_water(
        RailState(date="2026-08-03", orders_today=0, high_water_mark=None), 100_000.0
    )
    assert seeded.high_water_mark == 100_000.0


def test_negative_orders_today_fails_closed(tmp_path):
    p = tmp_path / "rail_state.json"
    p.write_text(json.dumps({
        "date": "2026-08-03",
        "orders_today": -5,
        "high_water_mark": 100_000.0,
    }))
    state, warning = load_state(p, "2026-08-03", CFG)
    assert state.orders_today == CFG.max_orders_per_day  # budget exhausted
    assert state.high_water_mark is None  # fail open for HWM
    assert "corrupt" in warning


def test_unreadable_file_fails_closed(tmp_path, monkeypatch):
    # Permissions, a directory in the file's place, a broken mount: the file
    # exists but we cannot see how much of today's budget is already spent.
    p = tmp_path / "rail_state.json"
    save_state(p, RailState(date="2026-08-03", orders_today=0, high_water_mark=100_000.0))

    def _unreadable(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _unreadable)
    state, warning = load_state(p, "2026-08-03", CFG)
    assert state.orders_today == CFG.max_orders_per_day  # budget exhausted
    assert state.high_water_mark is None                 # fail open for HWM
    assert "unreadable" in warning


def test_negative_high_water_mark_fails_closed(tmp_path):
    p = tmp_path / "rail_state.json"
    p.write_text(json.dumps({
        "date": "2026-08-03",
        "orders_today": 1,
        "high_water_mark": -1.0,
    }))
    state, warning = load_state(p, "2026-08-03", CFG)
    assert state.orders_today == CFG.max_orders_per_day  # budget exhausted
    assert state.high_water_mark is None  # fail open for HWM
    assert "corrupt" in warning
