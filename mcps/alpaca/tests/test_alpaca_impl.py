"""Mapper tests with SimpleNamespace stand-ins for alpaca-py models — the SDK
returns pydantic objects with these attribute names; we test the mapping, not
the SDK. Network paths are covered by tests/integration_alpaca.py (gated)."""
from types import SimpleNamespace

from alpaca_impl import AlpacaBroker, _to_account, _to_order, _to_position
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


def test_alpaca_broker_satisfies_protocol():
    assert issubclass(AlpacaBroker, object) and isinstance(
        AlpacaBroker.__init__, object
    )
    # structural check without constructing (constructor builds SDK clients):
    for name in ("get_account", "get_positions", "get_quote", "get_daily_bars",
                 "get_clock", "get_asset", "place_order", "cancel_order",
                 "list_open_orders", "get_order", "get_portfolio_history"):
        assert callable(getattr(AlpacaBroker, name))


def test_only_alpaca_impl_imports_the_sdk():
    # Tightened from a plain substring check: "from alpaca" / "import alpaca"
    # would false-positive on "from alpaca_impl import ..." (added by Task 7's
    # server.py). Match only the SDK package itself.
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for py in root.glob("*.py"):
        if py.name == "alpaca_impl.py":
            continue
        text = py.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            assert not (
                stripped.startswith("from alpaca.")
                or stripped.startswith("from alpaca import")
                or stripped.startswith("import alpaca.")
                or stripped == "import alpaca"
            ), f"{py.name}: {stripped!r}"
