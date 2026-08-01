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
