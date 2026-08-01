"""Real paper-API smoke test. Run manually:
ALPACA_INTEGRATION_TEST=1 ALPACA_API_KEY=... ALPACA_SECRET_KEY=... \
  uv run --extra dev pytest tests/integration_alpaca.py -v
Places a 1-share limit buy far below market, verifies it appears, cancels it.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ALPACA_INTEGRATION_TEST") != "1",
    reason="set ALPACA_INTEGRATION_TEST=1 with paper keys to run",
)


@pytest.fixture
def broker():
    from alpaca_impl import AlpacaBroker
    return AlpacaBroker(os.environ["ALPACA_API_KEY"],
                        os.environ["ALPACA_SECRET_KEY"], paper=True)


def test_account_and_clock(broker):
    a = broker.get_account()
    assert a.equity > 0
    c = broker.get_clock()
    assert c.next_open


def test_bars_and_asset(broker):
    bars = broker.get_daily_bars("SPY", "2026-07-01", "2026-07-30")
    assert len(bars) > 15 and all(b.close > 0 for b in bars)
    asset = broker.get_asset("SPY")
    assert asset.asset_class == "us_equity" and asset.tradable


def test_place_verify_cancel(broker):
    from broker import OrderRequest
    quote = broker.get_quote("SPY")
    assert quote.last and quote.last > 100
    lowball = round(quote.last * 0.5, 2)
    order = broker.place_order(OrderRequest(
        symbol="SPY", side="buy", order_type="limit", qty=1, limit_price=lowball))
    try:
        assert order.id
        fetched = broker.get_order(order.id)
        assert fetched.symbol == "SPY"
    finally:
        broker.cancel_order(order.id)
