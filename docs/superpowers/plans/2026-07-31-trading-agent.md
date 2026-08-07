# Trading Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `trader` Claub agent that paper-trades US equities through a new `mcps/alpaca/` MCP server with hard safety rails and an honest computed scoreboard.

**Architecture:** One new FastMCP stdio server (`mcps/alpaca/`) baked into the image, split into a broker-agnostic protocol (`broker.py`), the only Alpaca-importing file (`alpaca_impl.py`), pure rail checks (`rails.py`), pure benchmark math (`performance.py`), and tool wiring (`server.py`). Everything else is configuration: agents.yaml, settings.json, per-agent mcp.json, identity/workspace markdown, a Playwright bridge profile, and compose env plumbing.

**Tech Stack:** Python ≥3.10, FastMCP from `mcp[cli]` (house pattern — see `mcps/hass/server.py`), `alpaca-py` (official SDK), pytest. No pandas/numpy — the math is a few list comprehensions.

**Spec:** `docs/superpowers/specs/2026-07-31-trading-agent-design.md` (committed). Read it before starting any task.

## Global Constraints

- Python `requires-python = ">=3.10"`; deps exactly `mcp[cli]`, `alpaca-py`; dev extra `pytest`. No other runtime deps.
- `alpaca_impl.py` is the ONLY file that may import `alpaca` (the alpaca-py package). Enforced by a unit test.
- No PDT handling anywhere — FINRA retired the rule; Alpaca removed the API fields 2026-07-06. Fields like `pattern_day_trader`, `daytrade_count`, `dtbp_check` must not appear.
- Long-only, US stocks/ETFs only, hard rails per spec: max_position_pct=10.0, max_orders_per_day=3, drawdown_halt_pct=15.0, kill switch `ALPACA_TRADING_DISABLED=1`, paper guard (`ALPACA_PAPER=true` required unless `ALPACA_LIVE_CONFIRMED=I_UNDERSTAND`).
- Server state dir: `ALPACA_STATE_DIR`, default `/claub/data/alpaca/` — holds `rail_state.json` and `trades.jsonl`.
- Rails/performance are pure modules: no I/O, no env reads inside check/math functions (state file I/O lives in small load/save helpers).
- Tool outputs are formatted human-readable strings shaped for the agent (house style), never raw SDK dumps.
- Tests run from `mcps/alpaca/`: `uv run --extra dev pytest tests/ -v`. Add `[tool.pytest.ini_options] pythonpath = ["."]` like leetcode-stats.
- `docs/` is gitignored — spec/plan files need `git add -f`.
- End every commit message with:
  `Claude-Session: https://claude.ai/code/session_017xjKd43HNfH5b53f8Uqd3U`
- Instance config lives at `~/docker/claub/` on the host (bind-mounted to `/claub/`), NOT in the repo. Repo `example/` gets sanitized mirrors.

---

### Task 1: Package scaffold + broker protocol

**Files:**
- Create: `mcps/alpaca/pyproject.toml`
- Create: `mcps/alpaca/broker.py`
- Create: `mcps/alpaca/tests/test_broker.py`

**Interfaces:**
- Produces (used by every later task): the dataclasses `Account`, `Position`, `Quote`, `Bar`, `Clock`, `AssetInfo`, `OrderRequest`, `Order`, `PortfolioHistory` and the `Broker` protocol, exactly as below.

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "alpaca-mcp"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "mcp[cli]",
    "alpaca-py",
]

[project.optional-dependencies]
dev = ["pytest"]

[tool.pytest.ini_options]
pythonpath = ["."]
```

- [ ] **Step 2: Write the failing test**

`tests/test_broker.py`:

```python
"""The Broker protocol is the vendor-swap seam: a fake must satisfy it."""
from broker import (
    Account, AssetInfo, Bar, Broker, Clock, Order, OrderRequest,
    Position, PortfolioHistory, Quote,
)


def test_order_request_defaults():
    req = OrderRequest(symbol="AAPL", side="buy", order_type="market", notional=1000.0)
    assert req.qty is None
    assert req.limit_price is None
    assert req.stop_loss is None
    assert req.take_profit is None


def test_fake_broker_satisfies_protocol():
    class Fake:
        def get_account(self) -> Account: ...
        def get_positions(self) -> list[Position]: ...
        def get_quote(self, symbol: str) -> Quote: ...
        def get_daily_bars(self, symbol: str, start: str, end: str) -> list[Bar]: ...
        def get_clock(self) -> Clock: ...
        def get_asset(self, symbol: str) -> AssetInfo: ...
        def place_order(self, req: OrderRequest) -> Order: ...
        def cancel_order(self, order_id: str) -> None: ...
        def list_open_orders(self) -> list[Order]: ...
        def get_order(self, order_id: str) -> Order: ...
        def get_portfolio_history(self, period: str) -> PortfolioHistory: ...

    b: Broker = Fake()  # would fail type-check; at runtime, isinstance via runtime_checkable
    assert isinstance(Fake(), Broker)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/ -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'broker'`

- [ ] **Step 4: Write broker.py**

```python
"""Broker-agnostic data shapes and interface.

This is the vendor-swap seam: alpaca_impl.py is the only implementation today;
an IBKR (or other) swap means one new impl file, nothing else changes.
All dataclasses use plain floats and ISO-8601 strings — no SDK types leak out.
"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float


@dataclass
class Quote:
    symbol: str
    bid: float | None
    ask: float | None
    last: float | None
    as_of: str


@dataclass
class Bar:
    date: str  # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Clock:
    is_open: bool
    next_open: str
    next_close: str


@dataclass
class AssetInfo:
    symbol: str
    name: str
    asset_class: str  # broker-normalized; "us_equity" is the only tradable class
    tradable: bool


@dataclass
class OrderRequest:
    symbol: str
    side: str  # "buy" | "sell"
    order_type: str  # "market" | "limit"
    qty: float | None = None
    notional: float | None = None
    limit_price: float | None = None
    stop_loss: float | None = None  # bracket leg stop price
    take_profit: float | None = None  # bracket leg limit price


@dataclass
class Order:
    id: str
    symbol: str
    side: str
    order_type: str
    status: str
    qty: float | None
    notional: float | None
    limit_price: float | None
    filled_qty: float
    filled_avg_price: float | None
    submitted_at: str


@dataclass
class PortfolioHistory:
    timestamps: list[str]  # YYYY-MM-DD
    equity: list[float]


@runtime_checkable
class Broker(Protocol):
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
    def get_quote(self, symbol: str) -> Quote: ...
    def get_daily_bars(self, symbol: str, start: str, end: str) -> list[Bar]: ...
    def get_clock(self) -> Clock: ...
    def get_asset(self, symbol: str) -> AssetInfo: ...
    def place_order(self, req: OrderRequest) -> Order: ...
    def cancel_order(self, order_id: str) -> None: ...
    def list_open_orders(self) -> list[Order]: ...
    def get_order(self, order_id: str) -> Order: ...
    def get_portfolio_history(self, period: str) -> PortfolioHistory: ...
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/ -v`
Expected: 2 PASS

- [ ] **Step 6: Commit**

```bash
git add mcps/alpaca/pyproject.toml mcps/alpaca/broker.py mcps/alpaca/tests/test_broker.py mcps/alpaca/uv.lock
git commit -m "feat(alpaca-mcp): package scaffold and broker protocol"
```

---

### Task 2: Rail checks (pure)

**Files:**
- Create: `mcps/alpaca/rails.py`
- Create: `mcps/alpaca/tests/test_rails.py`

**Interfaces:**
- Consumes: `Account`, `Position`, `AssetInfo`, `OrderRequest` from `broker.py` (Task 1 signatures).
- Produces: `RailConfig` (fields `max_position_pct: float = 10.0`, `max_orders_per_day: int = 3`, `drawdown_halt_pct: float = 15.0`, `trading_disabled: bool = False`), `config_from_env(env: dict) -> RailConfig`, `RailState` (fields `date: str`, `orders_today: int`, `high_water_mark: float | None`), and `check_order(req, *, account, position, asset, est_notional, state, cfg) -> str | None` (None = allowed; string = rejection reason).

- [ ] **Step 1: Write the failing tests**

`tests/test_rails.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/test_rails.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rails'`

- [ ] **Step 3: Write rails.py**

```python
"""Hard safety rails — pure functions, no I/O, no env access.

Every check returns None (allowed) or a human-readable rejection string that
names the rail and the numbers involved, so the agent can adapt instead of
retrying blindly. These are the hard ceiling; the agent's workspace config may
self-impose tighter limits but can never loosen these.
"""
from dataclasses import dataclass

from broker import Account, AssetInfo, OrderRequest, Position


@dataclass
class RailConfig:
    max_position_pct: float = 10.0
    max_orders_per_day: int = 3
    drawdown_halt_pct: float = 15.0
    trading_disabled: bool = False


@dataclass
class RailState:
    date: str  # YYYY-MM-DD (ET) the orders_today counter belongs to
    orders_today: int
    high_water_mark: float | None


def config_from_env(env: dict) -> RailConfig:
    def _f(key: str, default: float) -> float:
        v = env.get(key, "")
        return float(v) if v else default

    def _i(key: str, default: int) -> int:
        v = env.get(key, "")
        return int(v) if v else default

    return RailConfig(
        max_position_pct=_f("ALPACA_MAX_POSITION_PCT", 10.0),
        max_orders_per_day=_i("ALPACA_MAX_ORDERS_PER_DAY", 3),
        drawdown_halt_pct=_f("ALPACA_DRAWDOWN_HALT_PCT", 15.0),
        trading_disabled=env.get("ALPACA_TRADING_DISABLED", "") in ("1", "true"),
    )


def check_order(
    req: OrderRequest,
    *,
    account: Account,
    position: Position | None,
    asset: AssetInfo,
    est_notional: float,
    state: RailState,
    cfg: RailConfig,
) -> str | None:
    if cfg.trading_disabled:
        return "rejected by rail kill_switch: trading is disabled (ALPACA_TRADING_DISABLED)"

    if state.orders_today >= cfg.max_orders_per_day:
        return (
            f"rejected by rail max_orders_per_day: {state.orders_today} orders already "
            f"placed today; cap is {cfg.max_orders_per_day}. Budget resets at midnight ET."
        )

    if req.side == "sell":
        held = position.qty if position else 0.0
        if req.qty is None or req.qty > held:
            return (
                f"rejected by rail long_only: cannot sell {req.qty or 'notional'} of "
                f"{req.symbol}; position is {held} shares. Sells must be by qty and "
                f"within the held quantity — no shorting."
            )
        return None  # sells within position are always allowed (incl. during drawdown halt)

    # --- buy-side rails ---
    if asset.asset_class != "us_equity":
        return (
            f"rejected by rail asset_universe: {asset.symbol} is {asset.asset_class}; "
            f"only us_equity (stocks/ETFs) is allowed"
        )
    if not asset.tradable:
        return f"rejected by rail asset_universe: {asset.symbol} is not tradable on Alpaca"

    if state.high_water_mark is not None:
        floor = state.high_water_mark * (1 - cfg.drawdown_halt_pct / 100)
        if account.equity < floor:
            return (
                f"rejected by rail circuit breaker: equity ${account.equity:,.0f} is more "
                f"than {cfg.drawdown_halt_pct:.0f}% below the high-water mark "
                f"${state.high_water_mark:,.0f}. Buys are halted; sells still work. "
                f"Talk to the user if you believe the halt should be lifted."
            )

    held_value = position.market_value if position else 0.0
    cap = account.equity * cfg.max_position_pct / 100
    if held_value + est_notional > cap + 1e-6:
        return (
            f"rejected by rail max_position_pct: ${held_value:,.0f} held + "
            f"${est_notional:,.0f} new = ${held_value + est_notional:,.0f} would exceed "
            f"{cfg.max_position_pct:.0f}% of equity (${cap:,.0f}) for {req.symbol}"
        )

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/test_rails.py -v`
Expected: 14 PASS

- [ ] **Step 5: Commit**

```bash
git add mcps/alpaca/rails.py mcps/alpaca/tests/test_rails.py
git commit -m "feat(alpaca-mcp): pure order rail checks"
```

---

### Task 3: Rail state persistence

**Files:**
- Modify: `mcps/alpaca/rails.py` (append)
- Create: `mcps/alpaca/tests/test_rail_state.py`

**Interfaces:**
- Produces: `load_state(path: Path, today: str, cfg: RailConfig) -> tuple[RailState, str | None]` (state + optional warning), `save_state(path: Path, state: RailState) -> None`, `update_high_water(state: RailState, equity: float) -> RailState`.

- [ ] **Step 1: Write the failing tests**

`tests/test_rail_state.py`:

```python
import json

from rails import RailConfig, RailState, load_state, save_state, update_high_water

CFG = RailConfig()


def test_missing_file_is_fresh_state(tmp_path):
    state, warning = load_state(tmp_path / "rail_state.json", "2026-08-03", CFG)
    assert state == RailState(date="2026-08-03", orders_today=0, high_water_mark=None)
    assert warning is None


def test_roundtrip(tmp_path):
    p = tmp_path / "rail_state.json"
    save_state(p, RailState(date="2026-08-03", orders_today=2, high_water_mark=105_000.0))
    state, warning = load_state(p, "2026-08-03", CFG)
    assert state.orders_today == 2
    assert state.high_water_mark == 105_000.0
    assert warning is None


def test_day_rollover_resets_counter_keeps_hwm(tmp_path):
    p = tmp_path / "rail_state.json"
    save_state(p, RailState(date="2026-08-03", orders_today=3, high_water_mark=105_000.0))
    state, _ = load_state(p, "2026-08-04", CFG)
    assert state == RailState(date="2026-08-04", orders_today=0, high_water_mark=105_000.0)


def test_corrupt_file_fails_closed_for_counter_open_for_hwm(tmp_path):
    p = tmp_path / "rail_state.json"
    p.write_text("{not json")
    state, warning = load_state(p, "2026-08-03", CFG)
    assert state.orders_today == CFG.max_orders_per_day  # budget exhausted
    assert state.high_water_mark is None  # will re-seed from current equity
    assert "corrupt" in warning


def test_update_high_water_rises_never_falls():
    s = RailState(date="2026-08-03", orders_today=0, high_water_mark=100_000.0)
    assert update_high_water(s, 110_000.0).high_water_mark == 110_000.0
    assert update_high_water(s, 90_000.0).high_water_mark == 100_000.0
    seeded = update_high_water(
        RailState(date="2026-08-03", orders_today=0, high_water_mark=None), 100_000.0
    )
    assert seeded.high_water_mark == 100_000.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/test_rail_state.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_state'`

- [ ] **Step 3: Append to rails.py**

```python
# --- state persistence (the only I/O in this module) ---
import json
from dataclasses import replace
from pathlib import Path


def load_state(path: Path, today: str, cfg: RailConfig) -> tuple[RailState, str | None]:
    """Missing file = normal first run. Corrupt file = fail closed for the order
    counter (budget exhausted today), fail open for the high-water mark (re-seed
    from current equity) — failing closed there would block buys forever."""
    if not path.exists():
        return RailState(date=today, orders_today=0, high_water_mark=None), None
    try:
        raw = json.loads(path.read_text())
        state = RailState(
            date=str(raw["date"]),
            orders_today=int(raw["orders_today"]),
            high_water_mark=(None if raw.get("high_water_mark") is None
                             else float(raw["high_water_mark"])),
        )
    except (ValueError, KeyError, TypeError):
        return (
            RailState(date=today, orders_today=cfg.max_orders_per_day, high_water_mark=None),
            f"rail state file {path} is corrupt; order budget treated as exhausted for "
            f"today and high-water mark re-seeded",
        )
    if state.date != today:
        state = replace(state, date=today, orders_today=0)
    return state, None


def save_state(path: Path, state: RailState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "date": state.date,
        "orders_today": state.orders_today,
        "high_water_mark": state.high_water_mark,
    }))
    tmp.replace(path)


def update_high_water(state: RailState, equity: float) -> RailState:
    if state.high_water_mark is None or equity > state.high_water_mark:
        return replace(state, high_water_mark=equity)
    return state
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/ -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add mcps/alpaca/rails.py mcps/alpaca/tests/test_rail_state.py
git commit -m "feat(alpaca-mcp): rail state persistence with fail-closed order counter"
```

---

### Task 4: Performance math (pure)

**Files:**
- Create: `mcps/alpaca/performance.py`
- Create: `mcps/alpaca/tests/test_performance.py`

**Interfaces:**
- Consumes: `Bar` from `broker.py`.
- Produces: `daily_returns(equity: list[float]) -> list[float]`, `total_return(equity: list[float]) -> float`, `annualized_vol(returns: list[float]) -> float`, `sharpe(returns: list[float], rf_annual: float = 0.04) -> float | None`, `max_drawdown(equity: list[float]) -> float`, `buy_and_hold_of_buys(buys: list[dict], bars_by_symbol: dict[str, list[Bar]]) -> float | None` (buys are trades.jsonl rows: `{"symbol", "side", "fill_date", "notional"}`; only `side=="buy"` rows count), `format_report(...) -> str` (Task 7 wires it; exact signature below).

- [ ] **Step 1: Write the failing tests**

`tests/test_performance.py`:

```python
import math

from broker import Bar
from performance import (
    annualized_vol, buy_and_hold_of_buys, daily_returns,
    max_drawdown, sharpe, total_return,
)


def bar(date, close):
    return Bar(date=date, open=close, high=close, low=close, close=close, volume=0)


def test_daily_returns():
    assert daily_returns([100.0, 110.0, 99.0]) == [0.1, -0.1]


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/test_performance.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'performance'`

- [ ] **Step 3: Write performance.py**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/test_performance.py -v`
Expected: 11 PASS

- [ ] **Step 5: Commit**

```bash
git add mcps/alpaca/performance.py mcps/alpaca/tests/test_performance.py
git commit -m "feat(alpaca-mcp): pure scoreboard math"
```

---

### Task 5: Report assembly

**Files:**
- Modify: `mcps/alpaca/performance.py` (append)
- Create: `mcps/alpaca/tests/test_report_format.py`

**Interfaces:**
- Consumes: everything from Task 4, `PortfolioHistory` from `broker.py`.
- Produces: `format_report(history: PortfolioHistory, spy_bars: list[Bar], buys: list[dict], bars_by_symbol: dict[str, list[Bar]], rf_annual: float = 0.04) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_report_format.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/test_report_format.py -v`
Expected: FAIL with `ImportError: cannot import name 'format_report'`

- [ ] **Step 3: Append format_report to performance.py**

```python
def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2%}".replace("%", "%")


def format_report(
    history: "PortfolioHistory",
    spy_bars: list[Bar],
    buys: list[dict],
    bars_by_symbol: dict[str, list[Bar]],
    rf_annual: float = 0.04,
) -> str:
    from broker import PortfolioHistory  # noqa: F401  (type only)

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
```

- [ ] **Step 4: Run tests, fix the `_pct` leftover if flagged**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/ -v`
Expected: all PASS. Note: `_pct` above is unused — delete it before committing (it was a drafting artifact; the f-strings handle formatting).

- [ ] **Step 5: Commit**

```bash
git add mcps/alpaca/performance.py mcps/alpaca/tests/test_report_format.py
git commit -m "feat(alpaca-mcp): scoreboard report assembly"
```

---

### Task 6: Alpaca implementation

**Files:**
- Create: `mcps/alpaca/alpaca_impl.py`
- Create: `mcps/alpaca/tests/test_alpaca_impl.py`

**Interfaces:**
- Consumes: all dataclasses + `Broker` from `broker.py`.
- Produces: `AlpacaBroker(api_key: str, secret_key: str, paper: bool)` implementing `Broker`; module-level pure mappers `_to_account(raw)`, `_to_position(raw)`, `_to_order(raw)` (tested with `SimpleNamespace` fakes so no network is needed).

- [ ] **Step 1: Write the failing tests**

`tests/test_alpaca_impl.py`:

```python
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
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for py in root.glob("*.py"):
        if py.name == "alpaca_impl.py":
            continue
        text = py.read_text()
        assert "from alpaca" not in text and "import alpaca" not in text, py.name
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/test_alpaca_impl.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'alpaca_impl'`

- [ ] **Step 3: Write alpaca_impl.py**

```python
"""The only file that imports alpaca-py. Maps SDK models to broker.py shapes.

Paper vs live is decided here by the `paper` flag — base URLs and everything
else follow from it. Daily bars use adjustment='all' (splits + dividends) so
benchmark math is total-return, matching what the account actually earns.
"""
from datetime import date, datetime, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest, GetPortfolioHistoryRequest, LimitOrderRequest,
    MarketOrderRequest, StopLossRequest, TakeProfitRequest,
)

from broker import (
    Account, AssetInfo, Bar, Clock, Order, OrderRequest, PortfolioHistory,
    Position, Quote,
)


def _f(v) -> float | None:
    return None if v is None else float(v)


def _enum(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _to_account(raw) -> Account:
    return Account(equity=float(raw.equity), cash=float(raw.cash),
                   buying_power=float(raw.buying_power))


def _to_position(raw) -> Position:
    return Position(symbol=raw.symbol, qty=float(raw.qty),
                    avg_entry_price=float(raw.avg_entry_price),
                    market_value=float(raw.market_value),
                    unrealized_pl=float(raw.unrealized_pl))


def _to_order(raw) -> Order:
    return Order(
        id=str(raw.id), symbol=raw.symbol, side=_enum(raw.side),
        order_type=_enum(raw.order_type), status=_enum(raw.status),
        qty=_f(raw.qty), notional=_f(raw.notional), limit_price=_f(raw.limit_price),
        filled_qty=float(raw.filled_qty or 0),
        filled_avg_price=_f(raw.filled_avg_price),
        submitted_at=raw.submitted_at.isoformat() if raw.submitted_at else "",
    )


class AlpacaBroker:
    def __init__(self, api_key: str, secret_key: str, paper: bool):
        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._data = StockHistoricalDataClient(api_key, secret_key)

    def get_account(self) -> Account:
        return _to_account(self._trading.get_account())

    def get_positions(self) -> list[Position]:
        return [_to_position(p) for p in self._trading.get_all_positions()]

    def get_quote(self, symbol: str) -> Quote:
        q = self._data.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
        try:
            t = self._data.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol))[symbol]
            last, as_of = float(t.price), t.timestamp.isoformat()
        except Exception:
            last, as_of = None, q.timestamp.isoformat()
        return Quote(symbol=symbol, bid=_f(q.bid_price), ask=_f(q.ask_price),
                     last=last, as_of=as_of)

    def get_daily_bars(self, symbol: str, start: str, end: str) -> list[Bar]:
        # Free tier requires end ≥ 15 min old; clamping end to "yesterday" keeps
        # daily-cadence calls always valid.
        end_d = min(date.fromisoformat(end), date.today() - timedelta(days=1))
        resp = self._data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=datetime.fromisoformat(start), end=datetime.combine(end_d, datetime.max.time()),
            adjustment="all",
        ))
        return [
            Bar(date=b.timestamp.date().isoformat(), open=float(b.open),
                high=float(b.high), low=float(b.low), close=float(b.close),
                volume=float(b.volume))
            for b in resp.data.get(symbol, [])
        ]

    def get_clock(self) -> Clock:
        c = self._trading.get_clock()
        return Clock(is_open=c.is_open, next_open=c.next_open.isoformat(),
                     next_close=c.next_close.isoformat())

    def get_asset(self, symbol: str) -> AssetInfo:
        a = self._trading.get_asset(symbol)
        return AssetInfo(symbol=a.symbol, name=a.name or "",
                         asset_class=_enum(a.asset_class), tradable=bool(a.tradable))

    def place_order(self, req: OrderRequest) -> Order:
        side = OrderSide.BUY if req.side == "buy" else OrderSide.SELL
        kwargs: dict = dict(symbol=req.symbol, side=side, time_in_force=TimeInForce.DAY)
        if req.qty is not None:
            kwargs["qty"] = req.qty
        else:
            kwargs["notional"] = req.notional
        if req.stop_loss is not None or req.take_profit is not None:
            from alpaca.trading.enums import OrderClass
            kwargs["order_class"] = OrderClass.BRACKET
            if req.take_profit is not None:
                kwargs["take_profit"] = TakeProfitRequest(limit_price=req.take_profit)
            if req.stop_loss is not None:
                kwargs["stop_loss"] = StopLossRequest(stop_price=req.stop_loss)
        if req.order_type == "limit":
            order = self._trading.submit_order(
                LimitOrderRequest(limit_price=req.limit_price, **kwargs))
        else:
            order = self._trading.submit_order(MarketOrderRequest(**kwargs))
        return _to_order(order)

    def cancel_order(self, order_id: str) -> None:
        self._trading.cancel_order_by_id(order_id)

    def list_open_orders(self) -> list[Order]:
        from alpaca.trading.enums import QueryOrderStatus
        return [_to_order(o) for o in self._trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN))]

    def get_order(self, order_id: str) -> Order:
        return _to_order(self._trading.get_order_by_id(order_id))

    def get_portfolio_history(self, period: str) -> PortfolioHistory:
        h = self._trading.get_portfolio_history(
            GetPortfolioHistoryRequest(period=period, timeframe="1D"))
        return PortfolioHistory(
            timestamps=[datetime.fromtimestamp(t).date().isoformat() for t in h.timestamp],
            equity=[float(e) for e in h.equity if e is not None],
        )
```

Note for implementer: alpaca-py class/field names above match v0.43.x. If an import fails, check the installed version's names (`uv run python -c "import alpaca; print(alpaca.__version__)"`) and adjust — the mappers and tests, not the tests' expectations, are the flexible part. `GetPortfolioHistoryRequest` lives in `alpaca.trading.requests`; if absent in the installed version, call `self._trading.get_portfolio_history(period=period, timeframe="1D")` directly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/test_alpaca_impl.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add mcps/alpaca/alpaca_impl.py mcps/alpaca/tests/test_alpaca_impl.py
git commit -m "feat(alpaca-mcp): Alpaca broker implementation behind the protocol"
```

---

### Task 7: FastMCP server

**Files:**
- Create: `mcps/alpaca/server.py`
- Create: `mcps/alpaca/tests/test_server.py`
- Create: `mcps/alpaca/tests/fake_broker.py`

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: the 12 MCP tools. Testable seams: module globals `server._broker` (inject a fake), `server.STATE_DIR` (point at tmp_path), `server._today()` (ET date string, monkeypatchable). `server.startup_check(env: dict) -> None` raises `RuntimeError` on paper-guard violation.

- [ ] **Step 1: Write fake_broker.py**

```python
"""In-memory Broker for server tests. Records placed orders."""
from dataclasses import dataclass, field

from broker import (
    Account, AssetInfo, Bar, Clock, Order, OrderRequest, PortfolioHistory,
    Position, Quote,
)


@dataclass
class FakeBroker:
    account: Account = field(default_factory=lambda: Account(100_000.0, 60_000.0, 60_000.0))
    positions: list[Position] = field(default_factory=list)
    assets: dict[str, AssetInfo] = field(default_factory=dict)
    quotes: dict[str, Quote] = field(default_factory=dict)
    bars: dict[str, list[Bar]] = field(default_factory=dict)
    history: PortfolioHistory = field(
        default_factory=lambda: PortfolioHistory(["2026-08-03"], [100_000.0]))
    placed: list[OrderRequest] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    open_orders: list[Order] = field(default_factory=list)

    def get_account(self): return self.account
    def get_positions(self): return self.positions
    def get_quote(self, symbol): return self.quotes[symbol]
    def get_daily_bars(self, symbol, start, end): return self.bars.get(symbol, [])
    def get_clock(self):
        return Clock(is_open=True, next_open="2026-08-04T09:30:00-04:00",
                     next_close="2026-08-03T16:00:00-04:00")
    def get_asset(self, symbol):
        return self.assets.get(symbol, AssetInfo(symbol, symbol, "us_equity", True))
    def place_order(self, req):
        self.placed.append(req)
        return Order(id=f"ord-{len(self.placed)}", symbol=req.symbol, side=req.side,
                     order_type=req.order_type, status="accepted", qty=req.qty,
                     notional=req.notional, limit_price=req.limit_price,
                     filled_qty=0.0, filled_avg_price=None,
                     submitted_at="2026-08-03T14:00:00+00:00")
    def cancel_order(self, order_id): self.cancelled.append(order_id)
    def list_open_orders(self): return self.open_orders
    def get_order(self, order_id):
        return Order(id=order_id, symbol="SPY", side="buy", order_type="market",
                     status="filled", qty=None, notional=1000.0, limit_price=None,
                     filled_qty=2.0, filled_avg_price=500.0,
                     submitted_at="2026-08-03T14:00:00+00:00")
    def get_portfolio_history(self, period): return self.history
```

- [ ] **Step 2: Write the failing tests**

`tests/test_server.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/test_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server'`

- [ ] **Step 4: Write server.py**

```python
"""MCP server for the trader agent — a narrow, rail-guarded slice of Alpaca.

Not a general Alpaca bridge (the official alpaca-mcp-server exposes 60+ tools;
this exposes 12). Hard rails live here in code — position caps, order budget,
drawdown circuit breaker, long-only, kill switch — because rails in a prompt
are suggestions. See docs/superpowers/specs/2026-07-31-trading-agent-design.md.

Requires: ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER=true (live requires
ALPACA_LIVE_CONFIRMED=I_UNDERSTAND as a deliberate second switch).
"""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

import performance
import rails
from broker import Broker, OrderRequest

STATE_DIR = Path(os.environ.get("ALPACA_STATE_DIR", "/claub/data/alpaca"))
RF_ANNUAL = 0.04
_broker: Broker | None = None

mcp = FastMCP("alpaca")


def startup_check(env: dict) -> None:
    if env.get("ALPACA_PAPER", "").lower() != "true" and \
            env.get("ALPACA_LIVE_CONFIRMED") != "I_UNDERSTAND":
        raise RuntimeError(
            "Refusing to start: ALPACA_PAPER is not 'true'. Live trading requires "
            "ALPACA_LIVE_CONFIRMED=I_UNDERSTAND set deliberately."
        )


def _get_broker() -> Broker:
    global _broker
    if _broker is None:
        from alpaca_impl import AlpacaBroker
        startup_check(dict(os.environ))
        _broker = AlpacaBroker(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
            paper=os.environ.get("ALPACA_PAPER", "true").lower() == "true",
        )
    return _broker


def _today() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _cfg() -> rails.RailConfig:
    return rails.config_from_env(dict(os.environ))


def _state_path() -> Path:
    return STATE_DIR / "rail_state.json"


def _append_trade(row: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "trades.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


def _read_trades() -> list[dict]:
    p = STATE_DIR / "trades.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _fmt_order(o) -> str:
    size = f"{o.qty} sh" if o.qty is not None else f"${o.notional:,.2f}"
    price = f" @ limit {o.limit_price}" if o.limit_price else ""
    fill = (f", filled {o.filled_qty} @ {o.filled_avg_price}"
            if o.filled_qty else "")
    return f"[{o.id}] {o.side} {size} {o.symbol} ({o.order_type}{price}) — {o.status}{fill}"


@mcp.tool()
def get_account() -> str:
    """Account equity, cash, and buying power."""
    a = _get_broker().get_account()
    return (f"equity ${a.equity:,.2f} | cash ${a.cash:,.2f} | "
            f"buying power ${a.buying_power:,.2f}")


@mcp.tool()
def get_positions() -> str:
    """Open positions with unrealized P&L."""
    ps = _get_broker().get_positions()
    if not ps:
        return "No open positions."
    return "\n".join(
        f"{p.symbol}: {p.qty} sh @ avg {p.avg_entry_price:,.2f}, "
        f"value ${p.market_value:,.2f}, unrealized P&L ${p.unrealized_pl:,.2f}"
        for p in ps
    )


@mcp.tool()
def get_quote(symbol: str) -> str:
    """Latest quote. Free-tier data can be up to 15 minutes stale — fine for
    daily decisions; do not treat as an executable price."""
    q = _get_broker().get_quote(symbol.upper())
    return (f"{q.symbol}: last {q.last} bid {q.bid} ask {q.ask} (as of {q.as_of}; "
            f"may be up to 15 min stale on the free tier)")


@mcp.tool()
def get_daily_bars(symbol: str, start: str, end: str) -> str:
    """Daily OHLCV bars (split+dividend adjusted), dates YYYY-MM-DD."""
    bars = _get_broker().get_daily_bars(symbol.upper(), start, end)
    if not bars:
        return f"No bars for {symbol} in {start}..{end}."
    lines = [f"{b.date}: o {b.open} h {b.high} l {b.low} c {b.close} v {b.volume:.0f}"
             for b in bars]
    return "\n".join(lines)


@mcp.tool()
def get_market_clock() -> str:
    """Whether the market is open now, and the next open/close. Check this
    before assuming an order fills today — orders placed after close queue
    overnight."""
    c = _get_broker().get_clock()
    now = "OPEN" if c.is_open else "CLOSED"
    return f"Market is {now}. Next open: {c.next_open}. Next close: {c.next_close}."


@mcp.tool()
def place_order(
    symbol: str,
    side: str,
    order_type: str,
    qty: float | None = None,
    notional: float | None = None,
    limit_price: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> str:
    """Place an order. side: buy|sell. order_type: market|limit (limit needs
    limit_price). Size with qty (shares) or notional (dollars) — sells must use
    qty. Optional stop_loss/take_profit attach bracket exit legs. Hard rails
    apply: long-only, US stocks/ETFs only, max 10% of equity per symbol, max 3
    orders/day, drawdown circuit breaker. Rejections explain themselves — read
    the reason before deciding whether to resize or skip."""
    symbol = symbol.upper()
    if side not in ("buy", "sell"):
        return "REJECTED: side must be 'buy' or 'sell'"
    if order_type not in ("market", "limit"):
        return "REJECTED: order_type must be 'market' or 'limit'"
    if order_type == "limit" and limit_price is None:
        return "REJECTED: limit orders require limit_price"
    if qty is None and notional is None:
        return "REJECTED: provide qty or notional"
    if side == "sell" and qty is None:
        return "REJECTED: sells must be sized with qty (shares), not notional"

    b = _get_broker()
    account = b.get_account()
    position = next((p for p in b.get_positions() if p.symbol == symbol), None)
    asset = b.get_asset(symbol)
    if notional is not None:
        est_notional = notional
    else:
        ref = limit_price if order_type == "limit" else (b.get_quote(symbol).last or 0.0)
        est_notional = (qty or 0.0) * ref

    cfg = _cfg()
    state, warning = rails.load_state(_state_path(), _today(), cfg)
    state = rails.update_high_water(state, account.equity)
    reason = rails.check_order(
        OrderRequest(symbol=symbol, side=side, order_type=order_type, qty=qty,
                     notional=notional, limit_price=limit_price,
                     stop_loss=stop_loss, take_profit=take_profit),
        account=account, position=position, asset=asset,
        est_notional=est_notional, state=state, cfg=cfg,
    )
    rails.save_state(_state_path(), state)  # persist HWM/day-rollover even on reject
    prefix = f"WARNING: {warning}\n" if warning else ""
    if reason:
        return f"{prefix}REJECTED: {reason}"

    order = b.place_order(OrderRequest(
        symbol=symbol, side=side, order_type=order_type, qty=qty, notional=notional,
        limit_price=limit_price, stop_loss=stop_loss, take_profit=take_profit))
    from dataclasses import replace
    state = replace(state, orders_today=state.orders_today + 1)
    rails.save_state(_state_path(), state)
    _append_trade({
        "ts": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "fill_date": _today(), "order_id": order.id, "symbol": symbol,
        "side": side, "order_type": order_type, "qty": qty,
        "notional": notional if notional is not None else est_notional,
        "limit_price": limit_price,
    })
    remaining = cfg.max_orders_per_day - state.orders_today
    return (f"{prefix}{_fmt_order(order)}\n"
            f"Order budget remaining today: {remaining}. Journal this trade now.")


@mcp.tool()
def cancel_order(order_id: str) -> str:
    """Cancel an open order by id."""
    _get_broker().cancel_order(order_id)
    return f"Cancel requested for {order_id}."


@mcp.tool()
def list_open_orders() -> str:
    """All open (unfilled) orders."""
    orders = _get_broker().list_open_orders()
    return "\n".join(_fmt_order(o) for o in orders) or "No open orders."


@mcp.tool()
def get_order(order_id: str) -> str:
    """Status and fill price of one order."""
    return _fmt_order(_get_broker().get_order(order_id))


@mcp.tool()
def get_portfolio_history(period: str = "1M") -> str:
    """Daily equity curve. period: 1W|1M|3M|1A|all."""
    h = _get_broker().get_portfolio_history(period)
    if not h.equity:
        return "No portfolio history yet."
    pairs = list(zip(h.timestamps, h.equity))
    lines = [f"{t}: ${e:,.2f}" for t, e in pairs[-30:]]
    if len(pairs) > 30:
        lines.insert(0, f"(showing last 30 of {len(pairs)} days)")
    return "\n".join(lines)


@mcp.tool()
def get_performance_report(period: str = "1M") -> str:
    """The honest scoreboard, computed in code: your return/vol/Sharpe/drawdown
    vs SPY total return and vs buy-and-hold of everything you bought. Include
    this verbatim in your weekly reflection post — never self-estimate
    performance."""
    b = _get_broker()
    h = b.get_portfolio_history(period)
    if len(h.equity) < 2:
        return "Not enough history for a report yet (need ≥2 daily points)."
    start, end = h.timestamps[0], h.timestamps[-1]
    spy = b.get_daily_bars("SPY", start, end)
    trades = [t for t in _read_trades() if start <= t["fill_date"] <= end]
    symbols = {t["symbol"] for t in trades if t["side"] == "buy"}
    bars_by_symbol = {s: b.get_daily_bars(s, start, end) for s in symbols}
    return performance.format_report(h, spy, trades, bars_by_symbol, RF_ANNUAL)


@mcp.tool()
def get_rails() -> str:
    """Current hard-rail configuration and today's remaining order budget. Call
    this when planning trades so you size within the rails instead of tripping
    rejections."""
    cfg = _cfg()
    state, warning = rails.load_state(_state_path(), _today(), cfg)
    lines = [
        f"max_position_pct: {cfg.max_position_pct:.0f}% of equity per symbol",
        f"max_orders_per_day: {cfg.max_orders_per_day}",
        f"orders used today: {state.orders_today} of {cfg.max_orders_per_day}",
        f"drawdown_halt_pct: {cfg.drawdown_halt_pct:.0f}% below high-water mark "
        f"(HWM: {state.high_water_mark and f'${state.high_water_mark:,.0f}' or 'not seeded yet'})",
        f"trading_disabled: {cfg.trading_disabled}",
        "long-only, US stocks/ETFs only, sells by qty within held position",
    ]
    if warning:
        lines.insert(0, f"WARNING: {warning}")
    return "\n".join(lines)


if __name__ == "__main__":
    startup_check(dict(os.environ))
    mcp.run()
```

- [ ] **Step 5: Run all tests to verify they pass**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/ -v`
Expected: all PASS (the import-hygiene test from Task 6 also covers server.py now — server.py must not import alpaca directly; the lazy `from alpaca_impl import AlpacaBroker` inside `_get_broker` is fine because the hygiene test greps for `alpaca`-package imports: adjust the test's assertion to allow `from alpaca_impl` — it checks `"from alpaca "` and `"import alpaca\n"`-style patterns; make sure the Task 6 test uses `"from alpaca."` and `"from alpaca import"` and `"import alpaca."`/exact `import alpaca` matches so `alpaca_impl` doesn't false-positive).

- [ ] **Step 6: Commit**

```bash
git add mcps/alpaca/server.py mcps/alpaca/tests/test_server.py mcps/alpaca/tests/fake_broker.py
git commit -m "feat(alpaca-mcp): rail-guarded FastMCP server with 12 tools"
```

---

### Task 8: Gated integration test

**Files:**
- Create: `mcps/alpaca/tests/integration_alpaca.py`

**Interfaces:**
- Consumes: `AlpacaBroker` from Task 6.

- [ ] **Step 1: Write the gated test module**

```python
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
    assert order.id
    fetched = broker.get_order(order.id)
    assert fetched.symbol == "SPY"
    broker.cancel_order(order.id)
```

- [ ] **Step 2: Verify it skips cleanly without the gate**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/ -v`
Expected: integration tests SKIP, everything else PASS

- [ ] **Step 3: Commit**

```bash
git add mcps/alpaca/tests/integration_alpaca.py
git commit -m "test(alpaca-mcp): gated paper-API integration smoke test"
```

---

### Task 9: Repo wiring — compose env, example config, docs

**Files:**
- Modify: `docker-compose.yml` (environment block)
- Modify: `example/config/settings.json` (allow list) — check actual path with `ls example/`
- Create: `example/config/agents/trader.mcp.json.example` (or follow whatever naming `example/` already uses — inspect first and mirror)
- Modify: `CLAUDE.md` (project structure + new section)
- Modify: `scripts/playwright-bridge/config.example.json` (trader profile)

- [ ] **Step 1: Add env plumbing to docker-compose.yml**

In the `environment:` list, after the `NOTION_TOKEN` line:

```yaml
      # Alpaca paper-trading keys for the trader agent's MCP (mcps/alpaca).
      # Live keys deliberately stay out of the container; see the design spec.
      - ALPACA_API_KEY=${ALPACA_API_KEY}
      - ALPACA_SECRET_KEY=${ALPACA_SECRET_KEY}
      - ALPACA_PAPER=${ALPACA_PAPER:-true}
      - ALPACA_TRADING_DISABLED=${ALPACA_TRADING_DISABLED:-}
      - ALPACA_MAX_POSITION_PCT=${ALPACA_MAX_POSITION_PCT:-}
      - ALPACA_MAX_ORDERS_PER_DAY=${ALPACA_MAX_ORDERS_PER_DAY:-}
      - ALPACA_DRAWDOWN_HALT_PCT=${ALPACA_DRAWDOWN_HALT_PCT:-}
```

- [ ] **Step 2: Mirror into example/ config**

Inspect `example/` first (`ls -R example/ | head -30`) and follow its existing conventions exactly. Add: `mcp__alpaca__*` to the example settings.json allow list; a trader example agent entry in the example agents.yaml (channel_id `"YOUR_TRADER_CHANNEL_ID"`); an example `trader.mcp.json`:

```json
{
  "mcpServers": {
    "playwright": {
      "type": "http",
      "url": "http://host.docker.internal:3859/mcp"
    },
    "alpaca": {
      "command": "uv",
      "args": ["--directory", "/app/mcps/alpaca", "run", "server.py"],
      "env": {
        "ALPACA_API_KEY": "${ALPACA_API_KEY}",
        "ALPACA_SECRET_KEY": "${ALPACA_SECRET_KEY}",
        "ALPACA_PAPER": "${ALPACA_PAPER}",
        "ALPACA_TRADING_DISABLED": "${ALPACA_TRADING_DISABLED}",
        "ALPACA_MAX_POSITION_PCT": "${ALPACA_MAX_POSITION_PCT}",
        "ALPACA_MAX_ORDERS_PER_DAY": "${ALPACA_MAX_ORDERS_PER_DAY}",
        "ALPACA_DRAWDOWN_HALT_PCT": "${ALPACA_DRAWDOWN_HALT_PCT}"
      }
    }
  }
}
```

- [ ] **Step 3: Add trader to scripts/playwright-bridge/config.example.json**

```json
    "trader":             { "port": 3859, "user_data_dir": "/Users/you/Library/Application Support/claub-playwright-bridge/profiles/trader" }
```

- [ ] **Step 4: Update CLAUDE.md**

Add to the Project Structure block under `mcps/`:

```
  alpaca/                         # Rail-guarded Alpaca paper-trading slice for the trader agent
```

Add a short section after "LeetCode Solve Monitoring" (mirror its tone/length):

```markdown
### Trading Agent (paper)

The `trader` agent paper-trades US equities through `mcps/alpaca/` — a narrow,
rail-guarded slice of Alpaca (12 tools, not the official 60-tool server). Hard
rails live in code, not prompts: long-only, US stocks/ETFs only, max 10% of
equity per symbol, max 3 orders/day, a drawdown circuit breaker (buys halt >15%
below the high-water mark), a kill switch (`ALPACA_TRADING_DISABLED=1`), and a
paper guard (server refuses to start live without `ALPACA_LIVE_CONFIRMED`).
`broker.py` is a vendor-agnostic protocol; `alpaca_impl.py` is the only file
importing the SDK, so a broker swap is one new file. Every order is appended to
`/claub/data/alpaca/trades.jsonl` by the server; `get_performance_report`
computes the scoreboard (vs SPY total return and vs buy-and-hold-of-buys) in
code because the research behind the design found benchmark selection is where
LLM-trading evaluations fool themselves. Paper→live is a deliberate env flip.
Design: `docs/superpowers/specs/2026-07-31-trading-agent-design.md`.
```

- [ ] **Step 5: Verify compose still parses and commit**

Run: `docker compose config -q && echo OK`
Expected: OK

```bash
git add docker-compose.yml example/ CLAUDE.md scripts/playwright-bridge/config.example.json
git commit -m "feat(trader): compose env, example config, bridge profile, docs"
```

---

### Task 10: Instance configuration (host-side, not in repo)

**Files (all under `~/docker/claub/` and host paths — none are in the repo):**
- Modify: `~/docker/claub/config/agents.yaml`
- Modify: `~/docker/claub/config/settings.json`
- Create: `~/docker/claub/config/agents/trader.md`
- Create: `~/docker/claub/config/agents/trader.mcp.json`
- Create: `~/docker/claub/workspaces/trader/CLAUDE.md`
- Modify: `~/Library/Application Support/claub-playwright-bridge/config.json`
- Modify: `~/docker/claub/.envrc` (the user adds keys — see step 6)

- [ ] **Step 1: agents.yaml — add under `agents:`**

```yaml
  trader:
    channel_id: "YOUR_TRADER_CHANNEL_ID"
    display_name: "Trader"
    allowed_tools_additional:
      - "mcp__alpaca__*"
```

- [ ] **Step 2: settings.json — add `"mcp__alpaca__*"` to `permissions.allow`**

- [ ] **Step 3: trader.mcp.json** — same content as the example in Task 9 Step 2 (port 3859, real env passthrough).

- [ ] **Step 4: trader.md (Level 2 — stable identity only)**

```markdown
---
name: trader
description: Disciplined long-only US equity paper trader with hard safety rails
---

You are a portfolio manager running a long-only US equity account. Right now it
is a paper account — the money is fake, but you treat it as real, because the
entire point of this phase is to find out whether your process is worth real
money. Your temperament: patient, skeptical of hype, comfortable doing nothing.
Most sessions should end with no trade. "Nothing cleared my bar today" is a
good outcome, not a failure.

## What you know about the evidence

You were built after a deep review of the LLM-trading literature. The honest
summary: no published LLM agent has beaten buy-and-hold under fair evaluation,
and the ones that claimed to were measuring benchmark mistakes. What measurably
helps: hard computed risk limits (already enforced in your tools — respect
rejections, don't fight them), reflection anchored to realized P&L (your weekly
job), and news/text analysis over price-staring (your daily job). What
measurably hurts: overtrading, momentum-chasing hype, and trusting your own
confidence as if it were calibrated probability. Transaction-cost drag killed
every short-horizon strategy in the literature — you hold for days to weeks,
never hours.

## Session procedure (daily)

1. Read `memory/index.md` and `beliefs.md`. Read the open-position theses in
   the current journal file.
2. Reconcile: `get_positions`, `list_open_orders`, `get_account`,
   `get_market_clock`. If the journal and reality disagree, trust reality and
   fix the journal.
3. Research: news on held names first (has any thesis broken?), then watchlist
   and new ideas. Use WebSearch/WebFetch and the browser. Prefer primary
   sources. Markets move on surprises — ask "is this priced in already?"
4. Decide. For any trade, write the structured thesis in the journal BEFORE
   placing the order.
5. Trade via `place_order`. If a rail rejects you, read the reason; resize once
   or skip — never loop on rejections.
6. Post to Discord only if you traded or something matters. Silence is fine.

## Thesis schema (every trade, in the journal)

- symbol, direction, size and why that size
- thesis: 2-4 sentences, falsifiable
- catalyst / horizon (days-weeks)
- exit: profit case AND invalidation case ("I'm wrong if...")
- (filled at close, by weekly reflection): realized P&L, what the thesis got
  right/wrong

## Weekly reflection (scheduled, Fridays after close)

1. `get_performance_report` — post it verbatim to Discord. Never self-estimate
   performance; the computed report is the only scoreboard.
2. Close out journal entries for positions exited this week: realized P&L vs
   thesis, one honest sentence on why it worked or didn't.
3. Update `beliefs.md`: distill lessons FROM REALIZED P&L ONLY (not from
   feelings about open positions). Max ~20 beliefs; each cites the trades that
   earned it; merge or delete stale ones. Beliefs are earned, not brainstormed.

## Memory structure

```
memory/index.md          # read first, every session
beliefs.md               # P&L-earned lessons; bounded at ~20
journal/YYYY-MM.md       # thesis entries, one file per month
performance/weekly.md    # scoreboard snapshots
```

## Hard rules

- Your tunable strategy parameters (watchlist, self-imposed limits, focus) live
  in this workspace's `CLAUDE.md` — read it every session; update it when the user
  shifts your focus. Hard rails are in the tools and are not yours to change.
- Paper fills are optimistic. Never present paper results as proof of edge.
- Never claim performance numbers that didn't come from
  `get_performance_report`.
- You trade at most what the rails allow; you AIM for less (see workspace
  config). An order rejection is information, not an obstacle.
```

- [ ] **Step 5: workspaces/trader/CLAUDE.md (Level 3 — fluid, agent-editable)**

```markdown
# Trader — Living Config

The user or the agent updates this as focus shifts. Hard rails (in the MCP server)
always win over anything here.

## Universe / watchlist

Liquid US large-caps and broad ETFs. Starting watchlist: SPY, QQQ, plus any
S&P 500 name with a live thesis. Avoid: microcaps, meme surges, anything you
can't articulate a falsifiable thesis for.

## Self-imposed limits (at or below the hard rails)

- Target 5-10 positions; new position size 5-8% of equity (hard cap is 10%)
- Soft max 2 orders/day (hard cap is 3)
- Minimum holding intent: ~5 trading days. No intraday round-trips.
- Prefer limit orders when the market is open and calm; market orders are fine
  for liquid names.

## Current focus

(none yet — starting phase: build the journal, earn some beliefs)
```

- [ ] **Step 6: Playwright bridge profile (host)**

Add to the `agents` map in `~/Library/Application Support/claub-playwright-bridge/config.json`:

```json
    "trader": { "port": 3859, "user_data_dir": "/Users/you/Library/Application Support/claub-playwright-bridge/profiles/trader" }
```

Then restart the bridge daemon and apply zoom prefs per the `claub-playwright` skill (read it — it has the exact launchctl command and the `apply-zoom-prefs.py` usage).

- [ ] **Step 7: Ask the user for keys**

The user creates the paper account at https://app.alpaca.markets/signup (email only),
generates PAPER API keys from the dashboard, and adds to `~/docker/claub/.envrc`:

```bash
export ALPACA_API_KEY="PK..."
export ALPACA_SECRET_KEY="..."
```

(`.envrc` overrides `.env` in compose — deploy with `source .envrc && docker compose up -d --build`.)

No commit for this task (nothing is in the repo).

---

### Task 11: Deploy and smoke test

- [ ] **Step 1: Full test suite green**

Run: `cd mcps/alpaca && uv run --extra dev pytest tests/ -v` and `cd bot && uv run --extra dev pytest tests/ -v --ignore=tests/test_integration.py`
Expected: all PASS (bot tests confirm nothing regressed; the bot doesn't change in this feature, but config parsing sees the new agents.yaml on restart).

- [ ] **Step 2: Integration test against the real paper API** (needs Task 10 Step 7 done)

Run: `cd mcps/alpaca && ALPACA_INTEGRATION_TEST=1 ALPACA_API_KEY=$ALPACA_API_KEY ALPACA_SECRET_KEY=$ALPACA_SECRET_KEY uv run --extra dev pytest tests/integration_alpaca.py -v` (source `~/docker/claub/.envrc` first)
Expected: 3 PASS

- [ ] **Step 3: Build and deploy**

```bash
cd ~/docker/claub && source .envrc && docker compose up -d --build
docker compose logs --tail 30
```

Expected: bot starts, no alpaca-related errors. (Compose commands run from the project root per its own docs — use the directory that holds docker-compose.yml; check `docker-compose.yml` location conventions in CLAUDE.md.)

- [ ] **Step 4: Debug-CLI smoke test** (see `claub-logs` skill for the debug CLI)

Drive the trader agent by hand: ask it to `get_rails`, `get_account`, and
`get_market_clock`. Verify tools respond, rails echo the defaults, and the
paper account shows ~$100k.

- [ ] **Step 5: Merge per superpowers:finishing-a-development-branch**

Tests green → merge `feat/trading-agent` to `main`, push, delete branch.

- [ ] **Step 6: First contact (the user, in Discord)**

Say hello in #trader; ask the agent to create its two schedules:
- daily: cron `30 10 * * 1-5`, prompt: "Daily trading session. Follow your session procedure: reconcile, research, decide, journal, trade only what clears your bar."
- weekly: cron `0 17 * * 5`, prompt: "Weekly reflection. Run get_performance_report and post it verbatim, close out exited journal entries with realized P&L, update beliefs.md."

---

## Self-Review (done at write time)

- **Spec coverage:** broker choice/abstraction (T1, T6), all seven rails (T2, T3, T7), scoreboard + both benchmarks + trades.jsonl (T4, T5, T7), 12 tools (T7), paper guard (T7), gated integration (T8), compose/example/docs (T9), three-level agent config + bridge + schedules + rollout (T10, T11). Error handling: rail rejections (T2), corrupt state (T3), arg validation (T7), reconcile-on-start (trader.md T10).
- **Placeholder scan:** clean — every code step has full content; the two "see skill" pointers (claub-playwright restart command, debug CLI) reference skills the executor can load, not missing content.
- **Type consistency:** `check_order` kwargs match between T2 tests/impl and T7 server call; `load_state(path, today, cfg) -> (state, warning)` consistent T3/T7; `format_report(history, spy_bars, buys, bars_by_symbol, rf_annual)` consistent T5/T7; trades.jsonl rows carry `symbol/side/fill_date/notional` exactly as `buy_and_hold_of_buys` consumes (T4/T7).
