from broker import Bar, PortfolioHistory
from performance import format_report


def bar(date, close):
    return Bar(date=date, open=close, high=close, low=close, close=close, volume=0)


HISTORY = PortfolioHistory(
    timestamps=["2026-08-03", "2026-08-04", "2026-08-05"],
    equity=[100_000.0, 101_000.0, 103_000.0],
)
SPY = [bar("2026-08-03", 500.0), bar("2026-08-04", 505.0), bar("2026-08-05", 501.0)]


def test_report_contains_agent_and_benchmark_numbers():
    out = format_report(HISTORY, SPY, buys=[], bars_by_symbol={})
    assert "+3.00%" in out            # agent total return
    assert "+0.20%" in out            # SPY total return
    assert "ahead of SPY" in out
    assert "no buys recorded" in out  # benchmark 2 fallback text


def test_report_behind_spy_is_stated_plainly():
    hist = PortfolioHistory(timestamps=HISTORY.timestamps,
                            equity=[100_000.0, 100_000.0, 100_100.0])
    out = format_report(hist, SPY, buys=[], bars_by_symbol={})
    assert "behind SPY" in out


def test_report_includes_bh_of_buys_when_present():
    buys = [{"symbol": "AAA", "side": "buy", "fill_date": "2026-08-03", "notional": 1000.0}]
    bars = {"AAA": [bar("2026-08-03", 100.0), bar("2026-08-05", 110.0)]}
    out = format_report(HISTORY, SPY, buys=buys, bars_by_symbol=bars)
    assert "+10.00%" in out           # B&H of buys
    assert "paper fills are optimistic" in out


def test_report_handles_empty_history():
    out = format_report(PortfolioHistory([], []), SPY, buys=[], bars_by_symbol={})
    assert "No equity history yet." in out


# --- benchmark integrity ---

def test_no_verdict_when_spy_has_too_few_bars():
    out = format_report(HISTORY, [bar("2026-08-03", 500.0)], buys=[], bars_by_symbol={})
    assert "Verdict:" not in out
    assert "SPY benchmark unavailable" in out
    assert "paper fills are optimistic" in out


def test_agent_stats_survive_a_missing_benchmark():
    # One SPY bar on the *last* day: the agent window still has three points,
    # so its stats stay — only the comparison is withheld.
    out = format_report(HISTORY, [bar("2026-08-05", 500.0)], buys=[], bars_by_symbol={})
    assert "Verdict:" not in out
    assert "SPY benchmark unavailable" in out
    assert "Agent:" in out and "+3.00%" in out


def test_agent_window_is_truncated_to_the_last_spy_bar():
    # Equity runs a day past the benchmark (the Friday-evening case: today's
    # equity point exists, today's bar does not). Scoring that extra day
    # against a SPY window that never saw it is the fabrication this prevents.
    hist = PortfolioHistory(
        timestamps=["2026-08-03", "2026-08-04", "2026-08-05"],
        equity=[100_000.0, 110_000.0, 50_000.0],
    )
    spy = [bar("2026-08-03", 500.0), bar("2026-08-04", 505.0)]
    out = format_report(hist, spy, buys=[], bars_by_symbol={})
    assert "+10.00%" in out        # through 2026-08-04 only
    assert "-50.00%" not in out    # the unaligned answer
    assert "2026-08-03 to 2026-08-04" in out
    assert "Verdict:" in out


def test_report_flags_buy_lots_with_no_bar_data():
    buys = [
        {"symbol": "AAA", "side": "buy", "fill_date": "2026-08-03", "notional": 1000.0},
        {"symbol": "ZZZ", "side": "buy", "fill_date": "2026-08-03", "notional": 500.0},
    ]
    bars = {"AAA": [bar("2026-08-03", 100.0), bar("2026-08-05", 110.0)]}
    out = format_report(HISTORY, SPY, buys=buys, bars_by_symbol=bars)
    assert "1 buy lot" in out
    assert "no bar data" in out
    assert "+10.00%" in out  # still computed from the lot that does have bars
