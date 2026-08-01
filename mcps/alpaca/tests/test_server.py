import json

import pytest

from broker import Quote
from tests.fake_broker import FakeBroker

import server


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
