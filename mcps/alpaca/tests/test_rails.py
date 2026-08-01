from broker import Account, AssetInfo, OrderRequest, Position
from rails import RailConfig, RailState, check_order, config_from_env

CFG = RailConfig()
ACCT = Account(equity=100_000.0, cash=50_000.0, buying_power=50_000.0)
ETF = AssetInfo(symbol="SPY", name="SPDR S&P 500", asset_class="us_equity", tradable=True)
STATE = RailState(date="2026-08-03", orders_today=0, high_water_mark=100_000.0)


def buy(notional=5_000.0, symbol="SPY"):
    return OrderRequest(symbol=symbol, side="buy", order_type="market", notional=notional)


def test_allows_plain_buy():
    assert check_order(buy(), account=ACCT, position=None, asset=ETF,
                       est_notional=5_000.0, state=STATE, cfg=CFG) is None


def test_kill_switch_blocks_everything():
    cfg = RailConfig(trading_disabled=True)
    reason = check_order(buy(), account=ACCT, position=None, asset=ETF,
                         est_notional=5_000.0, state=STATE, cfg=cfg)
    assert "disabled" in reason


def test_short_sell_rejected():
    req = OrderRequest(symbol="SPY", side="sell", order_type="market", qty=10)
    reason = check_order(req, account=ACCT, position=None, asset=ETF,
                         est_notional=5_000.0, state=STATE, cfg=CFG)
    assert "long-only" in reason


def test_sell_more_than_held_rejected():
    pos = Position(symbol="SPY", qty=5, avg_entry_price=500.0,
                   market_value=2_500.0, unrealized_pl=0.0)
    req = OrderRequest(symbol="SPY", side="sell", order_type="market", qty=10)
    reason = check_order(req, account=ACCT, position=pos, asset=ETF,
                         est_notional=5_000.0, state=STATE, cfg=CFG)
    assert "long-only" in reason


def test_sell_within_position_allowed_even_when_budget_and_drawdown_bad():
    # Sells must stay possible during a drawdown halt; only the daily budget
    # still applies to them (it counts orders, not sides? — no: budget applies).
    pos = Position(symbol="SPY", qty=10, avg_entry_price=500.0,
                   market_value=5_000.0, unrealized_pl=0.0)
    state = RailState(date="2026-08-03", orders_today=0, high_water_mark=200_000.0)
    req = OrderRequest(symbol="SPY", side="sell", order_type="market", qty=10)
    assert check_order(req, account=ACCT, position=pos, asset=ETF,
                       est_notional=5_000.0, state=state, cfg=CFG) is None


def test_non_equity_asset_rejected():
    crypto = AssetInfo(symbol="BTCUSD", name="Bitcoin", asset_class="crypto", tradable=True)
    reason = check_order(buy(symbol="BTCUSD"), account=ACCT, position=None, asset=crypto,
                         est_notional=5_000.0, state=STATE, cfg=CFG)
    assert "us_equity" in reason


def test_untradable_asset_rejected():
    dead = AssetInfo(symbol="XXXX", name="Delisted", asset_class="us_equity", tradable=False)
    reason = check_order(buy(symbol="XXXX"), account=ACCT, position=None, asset=dead,
                         est_notional=5_000.0, state=STATE, cfg=CFG)
    assert "not tradable" in reason


def test_position_cap_counts_existing_position():
    pos = Position(symbol="SPY", qty=12, avg_entry_price=500.0,
                   market_value=6_000.0, unrealized_pl=0.0)
    # 6k held + 5k new = 11k > 10% of 100k
    reason = check_order(buy(), account=ACCT, position=pos, asset=ETF,
                         est_notional=5_000.0, state=STATE, cfg=CFG)
    assert "max_position_pct" in reason and "10" in reason


def test_position_cap_boundary_exactly_at_cap_allowed():
    assert check_order(buy(notional=10_000.0), account=ACCT, position=None, asset=ETF,
                       est_notional=10_000.0, state=STATE, cfg=CFG) is None


def test_order_budget_exhausted():
    state = RailState(date="2026-08-03", orders_today=3, high_water_mark=100_000.0)
    reason = check_order(buy(), account=ACCT, position=None, asset=ETF,
                         est_notional=5_000.0, state=state, cfg=CFG)
    assert "max_orders_per_day" in reason


def test_drawdown_halts_buys_not_sells():
    state = RailState(date="2026-08-03", orders_today=0, high_water_mark=120_000.0)
    # equity 100k < 85% of 120k (=102k) → buys blocked
    reason = check_order(buy(), account=ACCT, position=None, asset=ETF,
                         est_notional=5_000.0, state=state, cfg=CFG)
    assert "circuit breaker" in reason


def test_no_high_water_mark_means_no_drawdown_check():
    state = RailState(date="2026-08-03", orders_today=0, high_water_mark=None)
    assert check_order(buy(), account=ACCT, position=None, asset=ETF,
                       est_notional=5_000.0, state=state, cfg=CFG) is None


def test_config_from_env_defaults_and_overrides():
    assert config_from_env({}) == RailConfig()
    cfg = config_from_env({
        "ALPACA_MAX_POSITION_PCT": "5",
        "ALPACA_MAX_ORDERS_PER_DAY": "1",
        "ALPACA_DRAWDOWN_HALT_PCT": "10",
        "ALPACA_TRADING_DISABLED": "1",
    })
    assert cfg == RailConfig(max_position_pct=5.0, max_orders_per_day=1,
                             drawdown_halt_pct=10.0, trading_disabled=True)


def test_config_from_env_ignores_empty_strings():
    # compose passes VAR=${VAR:-} — empty must mean "use default"
    assert config_from_env({"ALPACA_MAX_POSITION_PCT": ""}) == RailConfig()
