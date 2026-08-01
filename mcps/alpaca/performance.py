"""Scoreboard math — pure functions over equity curves and bars.

The evidence review behind this agent found benchmark selection is where the
LLM-trading literature fooled itself (wrong index, dividend-free benchmarks,
rf=0 Sharpe flattery). The scoreboard is therefore computed here, in code,
against SPY total return AND buy-and-hold-what-you-bought — never recalled or
estimated by the model.
"""
import math

from broker import Bar

TRADING_DAYS = 252


def daily_returns(equity: list[float]) -> list[float]:
    return [b / a - 1 for a, b in zip(equity, equity[1:]) if a]


def total_return(equity: list[float]) -> float:
    if len(equity) < 2 or not equity[0]:
        return 0.0
    return equity[-1] / equity[0] - 1


def annualized_vol(returns: list[float]) -> float:
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def sharpe(returns: list[float], rf_annual: float = 0.04) -> float | None:
    vol = annualized_vol(returns)
    if not returns or vol == 0:
        return None
    mean_annual = sum(returns) / len(returns) * TRADING_DAYS
    return (mean_annual - rf_annual) / vol


def max_drawdown(equity: list[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, 1 - v / peak)
    return worst


def _entry_close(bars: list[Bar], fill_date: str) -> float | None:
    """Close on the first bar on/after fill_date (fills can land on half-days
    missing from the bar set)."""
    for b in bars:
        if b.date >= fill_date:
            return b.close
    return None


def buy_and_hold_of_buys(
    buys: list[dict], bars_by_symbol: dict[str, list[Bar]]
) -> float | None:
    """Benchmark 2: every buy, held from fill to the last bar. Notional-weighted.
    Sells are deliberately ignored — the question this answers is 'did your
    exits and timing add anything over just keeping what you bought?'"""
    weighted, total = 0.0, 0.0
    for row in buys:
        if row.get("side") != "buy":
            continue
        bars = bars_by_symbol.get(row["symbol"], [])
        entry = _entry_close(bars, row["fill_date"])
        if not bars or entry is None or not entry:
            continue
        lot_return = bars[-1].close / entry - 1
        weighted += lot_return * row["notional"]
        total += row["notional"]
    return weighted / total if total else None
