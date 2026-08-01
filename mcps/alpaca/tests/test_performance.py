import math

from broker import Bar
from performance import (
    annualized_vol, buy_and_hold_of_buys, daily_returns,
    max_drawdown, sharpe, total_return,
)


def bar(date, close):
    return Bar(date=date, open=close, high=close, low=close, close=close, volume=0)


def test_daily_returns():
    result = daily_returns([100.0, 110.0, 99.0])
    assert len(result) == 2
    assert math.isclose(result[0], 0.1, rel_tol=1e-9)
    assert math.isclose(result[1], -0.1, rel_tol=1e-9)


def test_total_return():
    assert math.isclose(total_return([100.0, 120.0]), 0.20)


def test_total_return_flat_and_degenerate():
    assert total_return([100.0]) == 0.0
    assert total_return([]) == 0.0


def test_annualized_vol():
    # alternating +1%/-1% daily: population std = 0.01, annualized = 0.01*sqrt(252)
    rets = [0.01, -0.01] * 50
    assert math.isclose(annualized_vol(rets), 0.01 * math.sqrt(252), rel_tol=1e-9)


def test_sharpe_zero_vol_is_none():
    assert sharpe([0.0, 0.0, 0.0]) is None


def test_sharpe_known_value():
    rets = [0.001] * 252 + [0.003] * 252  # mean 0.002, pop std 0.001
    expected = (0.002 * 252 - 0.04) / (0.001 * math.sqrt(252))
    assert math.isclose(sharpe(rets, rf_annual=0.04), expected, rel_tol=1e-9)


def test_max_drawdown():
    # peak 120 → trough 90 = 25%
    assert math.isclose(max_drawdown([100, 120, 95, 90, 110]), 0.25)


def test_max_drawdown_monotonic_rise_is_zero():
    assert max_drawdown([100, 110, 120]) == 0.0


def test_buy_and_hold_of_buys_notional_weighted():
    bars = {
        "AAA": [bar("2026-08-03", 100.0), bar("2026-08-04", 110.0), bar("2026-08-05", 120.0)],
        "BBB": [bar("2026-08-03", 50.0), bar("2026-08-04", 50.0), bar("2026-08-05", 45.0)],
    }
    buys = [
        {"symbol": "AAA", "side": "buy", "fill_date": "2026-08-03", "notional": 1000.0},
        {"symbol": "BBB", "side": "buy", "fill_date": "2026-08-04", "notional": 3000.0},
        {"symbol": "AAA", "side": "sell", "fill_date": "2026-08-04", "notional": 500.0},
    ]
    # AAA lot: 120/100 - 1 = +20% on 1000; BBB lot: 45/50 - 1 = -10% on 3000
    expected = (0.20 * 1000 + (-0.10) * 3000) / 4000
    assert math.isclose(buy_and_hold_of_buys(buys, bars), expected)


def test_buy_and_hold_fill_date_between_bars_uses_next_bar():
    bars = {"AAA": [bar("2026-08-03", 100.0), bar("2026-08-06", 110.0)]}
    buys = [{"symbol": "AAA", "side": "buy", "fill_date": "2026-08-04", "notional": 100.0}]
    assert math.isclose(buy_and_hold_of_buys(buys, bars), 0.0)  # entry bar IS the last bar


def test_buy_and_hold_no_buys_is_none():
    assert buy_and_hold_of_buys([], {}) is None
