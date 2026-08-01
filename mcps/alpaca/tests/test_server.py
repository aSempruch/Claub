import json

import pytest

from broker import Account, Order, Quote
from tests.fake_broker import FakeBroker

import server


def _open_buy(symbol, *, qty=None, notional=None, limit_price=None):
    return Order(id=f"open-{symbol}", symbol=symbol, side="buy",
                 order_type="limit" if limit_price else "market", status="new",
                 qty=qty, notional=notional, limit_price=limit_price,
                 filled_qty=0.0, filled_avg_price=None,
                 submitted_at="2026-08-03T14:00:00+00:00")


@pytest.fixture
def srv(tmp_path, monkeypatch):
    fake = FakeBroker()
    fake.quotes["SPY"] = Quote("SPY", 499.9, 500.1, 500.0, "2026-08-03T14:00:00+00:00")
    monkeypatch.setattr(server, "_broker", fake)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(server, "_today", lambda: "2026-08-03")
    return fake


def test_startup_check_requires_paper():
    with pytest.raises(RuntimeError, match="paper"):
        server.startup_check({"ALPACA_PAPER": "false"})
    server.startup_check({"ALPACA_PAPER": "true"})  # no raise
    server.startup_check({"ALPACA_PAPER": "false",
                          "ALPACA_LIVE_CONFIRMED": "I_UNDERSTAND"})  # no raise


def test_place_order_happy_path_records_trade(srv, tmp_path):
    out = server.place_order(symbol="SPY", side="buy", order_type="market", notional=5000.0)
    assert "accepted" in out and "ord-1" in out
    assert "2 of 3" not in out  # remaining budget is 2 after 1 order
    assert "remaining today: 2" in out
    assert len(srv.placed) == 1
    rows = [json.loads(l) for l in (tmp_path / "trades.jsonl").read_text().splitlines()]
    assert rows[0]["symbol"] == "SPY" and rows[0]["side"] == "buy"
    assert rows[0]["fill_date"] == "2026-08-03"


def test_place_order_rejected_by_rail_places_nothing(srv, tmp_path):
    out = server.place_order(symbol="SPY", side="buy", order_type="market", notional=50_000.0)
    assert out.startswith("REJECTED")
    assert "max_position_pct" in out
    assert srv.placed == []
    assert not (tmp_path / "trades.jsonl").exists()


def test_order_budget_counts_across_calls(srv):
    for _ in range(3):
        server.place_order(symbol="SPY", side="buy", order_type="market", notional=1000.0)
    out = server.place_order(symbol="SPY", side="buy", order_type="market", notional=1000.0)
    assert "REJECTED" in out and "max_orders_per_day" in out
    assert len(srv.placed) == 3


def test_place_order_arg_validation(srv):
    out = server.place_order(symbol="SPY", side="buy", order_type="limit", notional=1000.0)
    assert "REJECTED" in out and "limit_price" in out
    out = server.place_order(symbol="SPY", side="buy", order_type="market")
    assert "REJECTED" in out and "qty or notional" in out


def test_get_rails_reports_config_and_budget(srv):
    server.place_order(symbol="SPY", side="buy", order_type="market", notional=1000.0)
    out = server.get_rails()
    assert "max_position_pct: 10" in out
    assert "orders used today: 1 of 3" in out


def test_market_clock(srv):
    out = server.get_market_clock()
    assert "OPEN" in out


def test_performance_report_tool(srv):
    from broker import Bar, PortfolioHistory
    srv.history = PortfolioHistory(["2026-08-03", "2026-08-04"], [100_000.0, 101_000.0])
    srv.bars["SPY"] = [Bar("2026-08-03", 500, 500, 500, 500.0, 0),
                       Bar("2026-08-04", 505, 505, 505, 505.0, 0)]
    out = server.get_performance_report(period="1M")
    assert "Agent:" in out and "SPY:" in out and "Verdict:" in out


# --- Fix-review findings ---

def test_place_order_rejects_qty_and_notional_together(srv):
    out = server.place_order(symbol="SPY", side="buy", order_type="market",
                              qty=100, notional=500.0)
    assert out.startswith("REJECTED") and "not both" in out
    assert srv.placed == []


def test_place_order_market_buy_falls_back_to_ask_when_last_missing(srv):
    srv.quotes["AAPL"] = Quote("AAPL", 149.0, 151.0, None, "2026-08-03T14:00:00+00:00")
    out = server.place_order(symbol="AAPL", side="buy", order_type="market", qty=10)
    assert "accepted" in out
    assert len(srv.placed) == 1


def test_place_order_market_buy_ask_fallback_still_trips_position_cap(srv):
    # last is None; ask is large enough that sizing off it (rather than
    # silently treating the missing last as a reference price of 0) should
    # trip the position cap. A ref-price of 0 would wrongly let this through.
    srv.quotes["AAPL"] = Quote("AAPL", 599.0, 601.0, None, "2026-08-03T14:00:00+00:00")
    out = server.place_order(symbol="AAPL", side="buy", order_type="market", qty=20)
    assert out.startswith("REJECTED") and "max_position_pct" in out
    assert srv.placed == []


def test_place_order_market_buy_rejected_without_reference_price(srv):
    srv.quotes["AAPL"] = Quote("AAPL", None, None, None, "2026-08-03T14:00:00+00:00")
    out = server.place_order(symbol="AAPL", side="buy", order_type="market", qty=10)
    assert out.startswith("REJECTED") and "reference price" in out
    assert srv.placed == []


def test_place_order_submit_failure_rolls_back_counter(srv, monkeypatch, tmp_path):
    def _raise(req):
        raise RuntimeError("boom")

    monkeypatch.setattr(srv, "place_order", _raise)
    out = server.place_order(symbol="SPY", side="buy", order_type="market", notional=1000.0)
    assert out.startswith("ERROR") and "boom" in out
    rails_out = server.get_rails()
    assert "orders used today: 0 of 3" in rails_out
    assert not (tmp_path / "trades.jsonl").exists()


def test_sell_within_position_does_not_need_a_quote(srv):
    from broker import Position
    # Deliberately no quote registered for AAPL — if the sell path touched the
    # quote, FakeBroker.get_quote's dict lookup would KeyError.
    srv.positions.append(Position("AAPL", 10, 150.0, 1600.0, 100.0))
    out = server.place_order(symbol="AAPL", side="sell", order_type="market", qty=5)
    assert "accepted" in out
    assert len(srv.placed) == 1


def test_oversell_rejected_long_only(srv):
    from broker import Position
    srv.positions.append(Position("AAPL", 5, 150.0, 750.0, 0.0))
    out = server.place_order(symbol="AAPL", side="sell", order_type="market", qty=10)
    assert out.startswith("REJECTED") and "long-only" in out
    assert srv.placed == []


def test_place_order_rejects_non_positive_sizes(srv):
    out = server.place_order(symbol="SPY", side="buy", order_type="market", qty=-1)
    assert out.startswith("REJECTED") and "qty" in out
    out = server.place_order(symbol="SPY", side="buy", order_type="market", notional=0)
    assert out.startswith("REJECTED") and "notional" in out
    out = server.place_order(symbol="SPY", side="buy", order_type="limit",
                              qty=1, limit_price=-5)
    assert out.startswith("REJECTED") and "limit_price" in out
    assert srv.placed == []


def test_disabled_sentinel_file_blocks_all_orders(srv, tmp_path):
    (tmp_path / "DISABLED").touch()
    out = server.place_order(symbol="SPY", side="buy", order_type="market", notional=1000.0)
    assert out.startswith("REJECTED") and "sentinel" in out
    assert srv.placed == []
    assert not (tmp_path / "trades.jsonl").exists()
    assert "DISABLED sentinel file present" in server.get_rails()


def test_open_buy_orders_count_against_position_cap(srv):
    # 12 sh × $500 = $6,000 committed but unfilled (6% of equity); a further
    # 5% buy would land the symbol past the 10% cap once both fill.
    srv.open_orders.append(_open_buy("SPY", qty=12.0, limit_price=500.0))
    out = server.place_order(symbol="SPY", side="buy", order_type="market", notional=5000.0)
    assert out.startswith("REJECTED") and "max_position_pct" in out
    assert "open buy orders" in out
    assert srv.placed == []


def test_open_order_in_another_symbol_does_not_count(srv):
    srv.open_orders.append(_open_buy("AAPL", qty=12.0, limit_price=500.0))
    out = server.place_order(symbol="SPY", side="buy", order_type="market", notional=5000.0)
    assert "accepted" in out
    assert len(srv.placed) == 1


def test_get_account_refreshes_high_water_mark(srv, tmp_path):
    import rails
    p = tmp_path / "rail_state.json"
    rails.save_state(p, rails.RailState(date="2026-08-03", orders_today=0,
                                        high_water_mark=100_000.0))
    srv.account = Account(equity=120_000.0, cash=60_000.0, buying_power=60_000.0)
    server.get_account()
    state, _ = rails.load_state(p, "2026-08-03", rails.RailConfig())
    assert state.high_water_mark == 120_000.0


def test_sell_records_null_notional(srv, tmp_path):
    from broker import Position
    srv.positions.append(Position("AAPL", 10, 150.0, 1600.0, 100.0))
    server.place_order(symbol="AAPL", side="sell", order_type="market", qty=5)
    row = json.loads((tmp_path / "trades.jsonl").read_text().splitlines()[0])
    assert row["notional"] is None  # not the 0.0 the sell path uses internally
    assert row["qty"] == 5


def test_limit_buy_by_qty_within_cap_is_accepted(srv):
    # 19 sh × $500 = $9,500, just inside the $10,000 cap
    out = server.place_order(symbol="SPY", side="buy", order_type="limit",
                             qty=19, limit_price=500.0)
    assert "accepted" in out
    assert len(srv.placed) == 1
    assert srv.placed[0].qty == 19 and srv.placed[0].limit_price == 500.0


def test_limit_buy_by_qty_just_over_cap_is_rejected(srv):
    # 21 sh × $500 = $10,500 — proves the estimate really is qty × limit_price
    out = server.place_order(symbol="SPY", side="buy", order_type="limit",
                             qty=21, limit_price=500.0)
    assert out.startswith("REJECTED") and "max_position_pct" in out
    assert srv.placed == []


def test_read_trades_skips_torn_lines(srv, tmp_path):
    good = json.dumps({"symbol": "SPY", "side": "buy", "fill_date": "2026-08-03"})
    torn = '{"symbol": "AAPL", "side": "bu'
    (tmp_path / "trades.jsonl").write_text(good + "\n" + torn)
    rows = server._read_trades()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "SPY"
