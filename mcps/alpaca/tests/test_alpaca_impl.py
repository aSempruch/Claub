"""Mapper tests with SimpleNamespace stand-ins for alpaca-py models — the SDK
returns pydantic objects with these attribute names; we test the mapping, not
the SDK. Network paths are covered by tests/integration_alpaca.py (gated)."""
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import alpaca_impl
from alpaca_impl import (
    AlpacaBroker, _clamped_bars_end, _to_account, _to_order,
    _to_portfolio_history, _to_position,
)
from broker import Broker


def test_to_account_parses_strings():
    raw = SimpleNamespace(equity="100000.50", cash="40000", buying_power="80000")
    acct = _to_account(raw)
    assert acct.equity == 100000.50 and acct.cash == 40000.0 and acct.buying_power == 80000.0


def test_to_position():
    raw = SimpleNamespace(symbol="AAPL", qty="10", avg_entry_price="200.5",
                          market_value="2100.0", unrealized_pl="95.0")
    pos = _to_position(raw)
    assert pos.symbol == "AAPL" and pos.qty == 10.0 and pos.market_value == 2100.0


def test_to_order_handles_none_fields():
    raw = SimpleNamespace(
        id="abc-123", symbol="SPY", side=SimpleNamespace(value="buy"),
        order_type=SimpleNamespace(value="market"), status=SimpleNamespace(value="accepted"),
        qty=None, notional="1000", limit_price=None, filled_qty="0",
        filled_avg_price=None, submitted_at=None,
    )
    o = _to_order(raw)
    assert o.id == "abc-123" and o.side == "buy" and o.qty is None
    assert o.notional == 1000.0 and o.filled_avg_price is None


def test_to_portfolio_history_drops_none_equity_pairs():
    # Alpaca pads `equity` with None for points it hasn't priced yet; the
    # parallel `timestamp` list has no corresponding gap. The kept timestamp
    # must line up with the kept equity value, not just match in count.
    raw = SimpleNamespace(
        timestamp=[1700000000, 1700086400, 1700172800],
        equity=[100000.0, None, 100500.0],
    )
    hist = _to_portfolio_history(raw)
    assert len(hist.timestamps) == len(hist.equity) == 2
    assert hist.equity == [100000.0, 100500.0]
    dropped_date = datetime.fromtimestamp(1700086400).date().isoformat()
    kept_dates = [
        datetime.fromtimestamp(1700000000).date().isoformat(),
        datetime.fromtimestamp(1700172800).date().isoformat(),
    ]
    assert hist.timestamps == kept_dates
    assert dropped_date not in hist.timestamps


def test_clamped_bars_end_keeps_16_minute_buffer_near_midnight(monkeypatch):
    # A plain "clamp to yesterday's end-of-day" leaves only minutes of buffer
    # shortly after local midnight (yesterday 23:59:59.999999 vs. now 00:05).
    # The fix must additionally clamp to now - 16min so the free-tier's
    # >=15-min-old rule always holds regardless of time of day.
    fixed_now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    fixed_today = date(2026, 8, 1)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return fixed_today

    monkeypatch.setattr(alpaca_impl, "datetime", FrozenDatetime)
    monkeypatch.setattr(alpaca_impl, "date", FrozenDate)

    end_dt = _clamped_bars_end("2026-08-01")

    assert end_dt <= fixed_now - timedelta(minutes=16)


def test_clamped_bars_end_still_respects_date_clamp_when_not_near_midnight():
    # Far from midnight, the date-based "yesterday" clamp is the binding
    # constraint, not the 16-minute buffer — this pins that the fix didn't
    # regress the original behavior for the common case.
    requested_end = date.today().isoformat()
    end_dt = _clamped_bars_end(requested_end)
    assert end_dt.date() <= date.today() - timedelta(days=1)


def test_alpaca_broker_satisfies_protocol():
    assert issubclass(AlpacaBroker, object) and isinstance(
        AlpacaBroker.__init__, object
    )
    # structural check without constructing (constructor builds SDK clients):
    for name in ("get_account", "get_positions", "get_quote", "get_daily_bars",
                 "get_clock", "get_asset", "place_order", "cancel_order",
                 "list_open_orders", "get_order", "get_portfolio_history"):
        assert callable(getattr(AlpacaBroker, name))


_SDK_IMPORT_RE = re.compile(r"^\s*(from|import)\s+alpaca\b(?!_impl)")


def test_only_alpaca_impl_imports_the_sdk():
    # A per-line prefix check (startswith("from alpaca.") etc.) misses
    # aliased/multi-name imports like "import alpaca as sdk" or
    # "import alpaca, os". Match with a regex instead: "alpaca" as a whole
    # word, not followed by "_impl" (so "from alpaca_impl import X" — which
    # Task 7's server.py adds — is correctly excluded).
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for py in root.glob("*.py"):
        if py.name == "alpaca_impl.py":
            continue
        text = py.read_text()
        for line in text.splitlines():
            assert not _SDK_IMPORT_RE.match(line), f"{py.name}: {line!r}"
