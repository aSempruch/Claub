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
                f"rejected by rail long-only: cannot sell {req.qty or 'notional'} of "
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
