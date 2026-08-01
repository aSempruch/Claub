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


def _lot_entry(row: dict, bars_by_symbol: dict[str, list[Bar]]) -> float | None:
    """Usable entry price for a buy lot, or None if this lot cannot be
    benchmarked (no bars for the symbol, or no bar on/after the fill)."""
    bars = bars_by_symbol.get(row["symbol"], [])
    if not bars:
        return None
    entry = _entry_close(bars, row["fill_date"])
    return entry or None


def excluded_buy_lots(
    buys: list[dict], bars_by_symbol: dict[str, list[Bar]]
) -> int:
    """Buy lots the benchmark silently drops for want of bar data. Surfaced in
    the report so a benchmark computed over half the portfolio is never mistaken
    for one computed over all of it."""
    return sum(1 for row in buys if row.get("side") == "buy"
               and _lot_entry(row, bars_by_symbol) is None)


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
        entry = _lot_entry(row, bars_by_symbol)
        if entry is None:
            continue
        lot_return = bars_by_symbol[row["symbol"]][-1].close / entry - 1
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
    """Scoreboard report: agent performance vs SPY and buy-and-hold-of-buys.

    The agent window is truncated to the last SPY bar so both sides of the
    comparison cover identical calendar days. Bars for the request's end date
    are not always available when the equity curve already has that day (a
    report run on Friday evening sees Friday's equity but Thursday's last bar),
    and a one-day head start on either side is exactly the kind of quiet
    benchmark mismatch this module exists to prevent. When the windows cannot
    be aligned the verdict is withheld rather than computed against a stub.
    """
    if not history.timestamps or not history.equity:
        return "No equity history yet."

    spy_closes = [b.close for b in spy_bars]
    if spy_bars:
        cutoff = spy_bars[-1].date
        aligned = [(t, e) for t, e in zip(history.timestamps, history.equity)
                   if t <= cutoff]
    else:
        aligned = list(zip(history.timestamps, history.equity))
    timestamps = [t for t, _ in aligned]
    equity = [e for _, e in aligned]

    agent_tr = total_return(equity)
    rets = daily_returns(equity)
    spy_tr = total_return(spy_closes)
    spy_rets = daily_returns(spy_closes)
    bh_buys = buy_and_hold_of_buys(buys, bars_by_symbol)
    excluded = excluded_buy_lots(buys, bars_by_symbol)

    def fmt_sharpe(s):
        return "n/a (needs more data)" if s is None else f"{s:.2f}"

    def agent_line():
        return (
            f"Agent:   total return {agent_tr:+.2%} | ann. vol {annualized_vol(rets):.2%} | "
            f"Sharpe (rf={rf_annual:.0%}) {fmt_sharpe(sharpe(rets, rf_annual))} | "
            f"max drawdown {max_drawdown(equity):.2%}"
        )

    if timestamps:
        period = (f"Period: {timestamps[0]} to {timestamps[-1]} "
                  f"({len(timestamps)} point{'s' if len(timestamps) != 1 else ''})")
    else:
        period = (f"Period: {history.timestamps[0]} to {history.timestamps[-1]} "
                  f"(no equity points inside the benchmark window)")

    if len(spy_closes) < 2 or len(equity) < 2:
        lines = [period, ""]
        if len(equity) >= 2:
            lines.append(agent_line())
        lines += [
            "SPY benchmark unavailable for this window (not enough overlapping "
            "SPY bars) — no verdict this run. Re-run once the window covers at "
            "least two shared trading days.",
        ]
    else:
        delta_spy = agent_tr - spy_tr
        verdict = "ahead of SPY" if delta_spy >= 0 else "behind SPY"
        lines = [
            period,
            "",
            agent_line(),
            f"SPY:     total return {spy_tr:+.2%} | ann. vol {annualized_vol(spy_rets):.2%} | "
            f"Sharpe (rf={rf_annual:.0%}) {fmt_sharpe(sharpe(spy_rets, rf_annual))} | "
            f"max drawdown {max_drawdown(spy_closes):.2%}",
            "",
            f"Verdict: {delta_spy:+.2%} {verdict} over this period.",
        ]

    excluded_phrase = (f"{excluded} buy lot{'s' if excluded != 1 else ''} excluded "
                       f"from benchmark: no bar data")
    note = f" ({excluded_phrase})" if excluded else ""
    if bh_buys is None:
        lines.append(f"Buy-and-hold-of-buys benchmark: unavailable ({excluded_phrase})."
                     if excluded else
                     "Buy-and-hold-of-buys benchmark: no buys recorded yet.")
    else:
        delta_bh = agent_tr - bh_buys
        lines.append(
            f"Buy-and-hold of everything you bought: {bh_buys:+.2%} "
            f"(your active management delta: {delta_bh:+.2%}){note}"
        )
    lines.append(
        "Reminder: paper fills are optimistic (no slippage or market impact); "
        "treat results as logic validation, not proof of edge."
    )
    return "\n".join(lines)
