"""The only file that imports alpaca-py. Maps SDK models to broker.py shapes.

Paper vs live is decided here by the `paper` flag — base URLs and everything
else follow from it. Daily bars use adjustment='all' (splits + dividends) so
benchmark math is total-return, matching what the account actually earns.
"""
from datetime import date, datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
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


def _to_portfolio_history(raw) -> PortfolioHistory:
    # Alpaca pads `equity` with None for points it hasn't priced yet (e.g. an
    # in-progress bar); `timestamp` has no corresponding gap. Filter as
    # (timestamp, equity) pairs so the two output lists can never drift apart
    # — filtering each list separately (as the original code did for equity
    # only) misaligns every point after the first None.
    pairs = [(t, e) for t, e in zip(raw.timestamp, raw.equity) if e is not None]
    return PortfolioHistory(
        timestamps=[datetime.fromtimestamp(t).date().isoformat() for t, _ in pairs],
        equity=[float(e) for _, e in pairs],
    )


def _clamped_bars_end(end: str) -> datetime:
    """Clamp a requested daily-bars `end` to satisfy Alpaca's free-tier
    15-min-old data rule. Two clamps stack: never later than yesterday (date
    clamp), and never within 16 minutes of now (buffer clamp). The date
    clamp alone leaves a near-zero margin shortly after local midnight, when
    "yesterday's end-of-day" is only minutes in the past.
    """
    end_d = min(date.fromisoformat(end), date.today() - timedelta(days=1))
    return min(
        datetime.combine(end_d, datetime.max.time(), tzinfo=timezone.utc),
        datetime.now(timezone.utc) - timedelta(minutes=16),
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
        resp = self._data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=datetime.fromisoformat(start), end=_clamped_bars_end(end),
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
        return [_to_order(o) for o in self._trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN))]

    def get_order(self, order_id: str) -> Order:
        return _to_order(self._trading.get_order_by_id(order_id))

    def get_portfolio_history(self, period: str) -> PortfolioHistory:
        h = self._trading.get_portfolio_history(
            GetPortfolioHistoryRequest(period=period, timeframe="1D"))
        return _to_portfolio_history(h)
