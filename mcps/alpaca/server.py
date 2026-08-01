"""MCP server for the trader agent — a narrow, rail-guarded slice of Alpaca.

Not a general Alpaca bridge (the official alpaca-mcp-server exposes 60+ tools;
this exposes 12). Hard rails live here in code — position caps, order budget,
drawdown circuit breaker, long-only, kill switch — because rails in a prompt
are suggestions. See docs/superpowers/specs/2026-07-31-trading-agent-design.md.

Requires: ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER=true (live requires
ALPACA_LIVE_CONFIRMED=I_UNDERSTAND as a deliberate second switch).
"""
import json
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

import performance
import rails
from broker import Broker, OrderRequest

# stdout is the MCP protocol channel, so operator logging goes to stderr, where
# `docker compose logs` picks it up. Every order, rejection and rollback is
# logged: the agent's own account of what it did is not an audit trail.
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("alpaca.server")

STATE_DIR = Path(os.environ.get("ALPACA_STATE_DIR", "/claub/data/alpaca"))
RF_ANNUAL = 0.04
_broker: Broker | None = None

mcp = FastMCP("alpaca")


def startup_check(env: dict) -> None:
    if env.get("ALPACA_PAPER", "").lower() != "true" and \
            env.get("ALPACA_LIVE_CONFIRMED") != "I_UNDERSTAND":
        raise RuntimeError(
            "Refusing to start: not running in paper mode (ALPACA_PAPER is not "
            "'true'). Live trading requires ALPACA_LIVE_CONFIRMED=I_UNDERSTAND "
            "set deliberately."
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
    """Skip unparseable lines rather than raising — a torn write (crash mid-append)
    should drop that one row from the benchmark, not permanently break
    get_performance_report for every trade recorded before or after it."""
    p = STATE_DIR / "trades.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _fmt_order(o) -> str:
    size = f"{o.qty} sh" if o.qty is not None else f"${o.notional:,.2f}"
    price = f" @ limit {o.limit_price}" if o.limit_price else ""
    fill = (f", filled {o.filled_qty} @ {o.filled_avg_price}"
            if o.filled_qty else "")
    return f"[{o.id}] {o.side} {size} {o.symbol} ({o.order_type}{price}) — {o.status}{fill}"


def _pending_buy_exposure(b: Broker, symbol: str, ref: float | None) -> float:
    """Dollar exposure of already-open buy orders in `symbol`. These are
    committed but unfilled, so the position cap must see them — three queued
    10% buys are a 30% position waiting to happen.

    A qty-sized open *market* order with no reference price contributes 0
    rather than blocking the check: that combination only exists for an
    unfilled market order in a symbol with no quote, it is transient, and
    failing the whole order there would be a worse trade-off than under-
    counting one order for a few seconds."""
    exposure = 0.0
    for o in b.list_open_orders():
        if o.symbol != symbol or o.side != "buy":
            continue
        if o.notional is not None:
            exposure += o.notional
        elif o.qty is not None:
            price = o.limit_price or ref
            if price:
                exposure += o.qty * price
    return exposure


def _quote_ref_price(q) -> float | None:
    """Best-available reference price for a market buy's position-cap estimate:
    last trade, then ask, then bid. None (or a non-positive quote) means we
    have nothing trustworthy to size-check against."""
    for price in (q.last, q.ask, q.bid):
        if price is not None and price > 0:
            return price
    return None


def _load_state(cfg: rails.RailConfig) -> tuple[rails.RailState, str | None]:
    state, warning = rails.load_state(_state_path(), _today(), cfg)
    if warning:
        log.warning("rail state: %s", warning)
    return state, warning


@mcp.tool()
def get_account() -> str:
    """Account equity, cash, and buying power."""
    a = _get_broker().get_account()
    # Refresh the high-water mark here too, not only on order placement: the
    # drawdown circuit breaker measures against the peak, and an agent that
    # checks its account daily but trades weekly would otherwise be measured
    # against a peak weeks stale.
    state, _ = _load_state(_cfg())
    rails.save_state(_state_path(), rails.update_high_water(state, a.equity))
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
    orders/day, drawdown circuit breaker, and a DISABLED sentinel file in the
    state dir that stops all placement immediately. Rejections explain
    themselves — read the reason before deciding whether to resize or skip."""
    # Sentinel kill switch, checked before anything else: `touch
    # /claub/data/alpaca/DISABLED` on the host halts trading instantly, with no
    # container restart and no env edit — the emergency stop reachable while an
    # agent is mid-session.
    if (STATE_DIR / "DISABLED").exists():
        log.warning("order refused: DISABLED sentinel present in %s", STATE_DIR)
        return "REJECTED: trading disabled by sentinel file (DISABLED in state dir)"

    symbol = symbol.upper()
    if side not in ("buy", "sell"):
        return "REJECTED: side must be 'buy' or 'sell'"
    if order_type not in ("market", "limit"):
        return "REJECTED: order_type must be 'market' or 'limit'"
    if order_type == "limit" and limit_price is None:
        return "REJECTED: limit orders require limit_price"
    if qty is None and notional is None:
        return "REJECTED: provide qty or notional"
    if qty is not None and notional is not None:
        return "REJECTED: provide qty or notional, not both"
    if qty is not None and qty <= 0:
        return "REJECTED: qty must be positive"
    if notional is not None and notional <= 0:
        return "REJECTED: notional must be positive"
    if limit_price is not None and limit_price <= 0:
        return "REJECTED: limit_price must be positive"
    if side == "sell" and qty is None:
        return "REJECTED: sells must be sized with qty (shares), not notional"

    b = _get_broker()
    account = b.get_account()
    position = next((p for p in b.get_positions() if p.symbol == symbol), None)
    asset = b.get_asset(symbol)

    ref: float | None = None
    if side == "sell":
        # The sell rail only compares qty against the held position — it never
        # reads est_notional — so skip the quote lookup entirely. Sells must
        # already be tradable (we're closing an existing position) and must
        # not depend on a quote existing for a symbol we're exiting.
        est_notional = 0.0
    elif notional is not None:
        est_notional = notional
    elif order_type == "limit":
        est_notional = qty * limit_price
    else:
        ref = _quote_ref_price(b.get_quote(symbol))
        if ref is None:
            return (f"REJECTED: no reference price available for {symbol}; "
                    f"cannot size-check this order — try a limit order")
        est_notional = qty * ref

    pending = _pending_buy_exposure(b, symbol, ref) if side == "buy" else 0.0

    cfg = _cfg()
    state, warning = _load_state(cfg)
    state = rails.update_high_water(state, account.equity)
    reason = rails.check_order(
        OrderRequest(symbol=symbol, side=side, order_type=order_type, qty=qty,
                     notional=notional, limit_price=limit_price,
                     stop_loss=stop_loss, take_profit=take_profit),
        account=account, position=position, asset=asset,
        est_notional=est_notional, state=state, cfg=cfg,
        pending_buy_exposure=pending,
    )
    rails.save_state(_state_path(), state)  # persist HWM/day-rollover even on reject
    prefix = f"WARNING: {warning}\n" if warning else ""
    if reason:
        log.info("REJECTED %s %s (qty=%s notional=%s): %s",
                 side, symbol, qty, notional, reason)
        return f"{prefix}REJECTED: {reason}"

    # Fail-closed order counting: increment and persist the counter BEFORE
    # submitting, not after. A submit that raises after the broker actually
    # accepted the order (e.g. a flaky response) must not leave a live order
    # uncounted — over-counting on a genuine failure is the safe direction,
    # under-counting on a phantom failure is not.
    state = replace(state, orders_today=state.orders_today + 1)
    rails.save_state(_state_path(), state)
    try:
        order = b.place_order(OrderRequest(
            symbol=symbol, side=side, order_type=order_type, qty=qty, notional=notional,
            limit_price=limit_price, stop_loss=stop_loss, take_profit=take_profit))
    except Exception as exc:
        rolled_back, _ = rails.load_state(_state_path(), _today(), cfg)
        rolled_back = replace(rolled_back, orders_today=max(0, rolled_back.orders_today - 1))
        rails.save_state(_state_path(), rolled_back)
        log.error("submit failed for %s %s (qty=%s notional=%s); order counter "
                  "rolled back to %s: %s", side, symbol, qty, notional,
                  rolled_back.orders_today, exc)
        return f"{prefix}ERROR: order submission failed: {exc}"

    log.info("PLACED %s %s qty=%s notional=%s limit=%s -> order %s (%s)",
             side, symbol, qty, notional, limit_price, order.id, order.status)
    _append_trade({
        "ts": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "fill_date": _today(), "order_id": order.id, "symbol": symbol,
        "side": side, "order_type": order_type, "qty": qty,
        # Sells are sized in shares and never estimated, so their notional is
        # genuinely unknown until fill — null, not the 0.0 placeholder the sell
        # path uses internally, which the benchmark would read as a real number.
        "notional": (notional if notional is not None
                     else (est_notional if side == "buy" else None)),
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
    """Daily equity curve. period: 1W|1M|3M|1A."""
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
    state, warning = _load_state(cfg)  # read-only: no HWM refresh here
    lines = [
        f"max_position_pct: {cfg.max_position_pct:.0f}% of equity per symbol",
        f"max_orders_per_day: {cfg.max_orders_per_day}",
        f"orders used today: {state.orders_today} of {cfg.max_orders_per_day}",
        f"drawdown_halt_pct: {cfg.drawdown_halt_pct:.0f}% below high-water mark "
        f"(HWM: {state.high_water_mark and f'${state.high_water_mark:,.0f}' or 'not seeded yet'})",
        f"trading_disabled: {cfg.trading_disabled}"
        + (" (DISABLED sentinel file present — all orders refused)"
           if (STATE_DIR / "DISABLED").exists() else ""),
        "long-only, US stocks/ETFs only, sells by qty within held position",
    ]
    if warning:
        lines.insert(0, f"WARNING: {warning}")
    return "\n".join(lines)


if __name__ == "__main__":
    startup_check(dict(os.environ))
    mcp.run()
