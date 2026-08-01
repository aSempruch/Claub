"""Scoreboard math — pure functions over equity curves and bars.

The evidence review behind this agent found benchmark selection is where the
LLM-trading literature fooled itself (wrong index, dividend-free benchmarks,
rf=0 Sharpe flattery). The scoreboard is therefore computed here, in code,
against SPY total return AND buy-and-hold-what-you-bought — never recalled or
estimated by the model.
"""
import math

from broker import Bar, PortfolioHistory

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


def format_report(
    history: PortfolioHistory,
    spy_bars: list[Bar],
    buys: list[dict],
    bars_by_symbol: dict[str, list[Bar]],
    rf_annual: float = 0.04,
) -> str:
    """Scoreboard report: agent performance vs SPY and buy-and-hold-of-buys."""
    if not history.timestamps or not history.equity:
        return "No equity history yet."
    agent_tr = total_return(history.equity)
    rets = daily_returns(history.equity)
    spy_closes = [b.close for b in spy_bars]
    spy_tr = total_return(spy_closes)
    spy_rets = daily_returns(spy_closes)
    bh_buys = buy_and_hold_of_buys(buys, bars_by_symbol)

    def fmt_sharpe(s):
        return "n/a (needs more data)" if s is None else f"{s:.2f}"

    delta_spy = agent_tr - spy_tr
    verdict = "ahead of SPY" if delta_spy >= 0 else "behind SPY"

    lines = [
        f"Period: {history.timestamps[0]} to {history.timestamps[-1]} "
        f"({len(history.timestamps)} points)",
        "",
        f"Agent:   total return {agent_tr:+.2%} | ann. vol {annualized_vol(rets):.2%} | "
        f"Sharpe (rf={rf_annual:.0%}) {fmt_sharpe(sharpe(rets, rf_annual))} | "
        f"max drawdown {max_drawdown(history.equity):.2%}",
        f"SPY:     total return {spy_tr:+.2%} | ann. vol {annualized_vol(spy_rets):.2%} | "
        f"Sharpe (rf={rf_annual:.0%}) {fmt_sharpe(sharpe(spy_rets, rf_annual))} | "
        f"max drawdown {max_drawdown(spy_closes):.2%}",
        "",
        f"Verdict: {delta_spy:+.2%} {verdict} over this period.",
    ]
    if bh_buys is None:
        lines.append("Buy-and-hold-of-buys benchmark: no buys recorded yet.")
    else:
        delta_bh = agent_tr - bh_buys
        lines.append(
            f"Buy-and-hold of everything you bought: {bh_buys:+.2%} "
            f"(your active management delta: {delta_bh:+.2%})"
        )
    lines.append(
        "Reminder: paper fills are optimistic (no slippage or market impact); "
        "treat results as logic validation, not proof of edge."
    )
    return "\n".join(lines)
