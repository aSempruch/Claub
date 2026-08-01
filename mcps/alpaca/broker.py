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
