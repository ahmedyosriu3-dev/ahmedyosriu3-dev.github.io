"""Alpaca broker adapter.

Paper and live share one code path -- only the base URL differs -- which is the
point: what you validate on paper is exactly what runs live. The `is_paper`
flag is set from config and surfaced everywhere in the UI.
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.config import settings
from app.trading.broker import (
    Account,
    Broker,
    BrokerError,
    BrokerOrder,
    Position,
)

log = logging.getLogger(__name__)


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, paper: bool | None = None) -> None:
        from alpaca.trading.client import TradingClient

        if not settings.alpaca_configured:
            raise BrokerError(
                "Alpaca API keys are not set. Add ALPACA_API_KEY and "
                "ALPACA_API_SECRET to your .env file."
            )
        self.is_paper = (not settings.alpaca_live) if paper is None else paper
        self._client = TradingClient(
            settings.alpaca_api_key,
            settings.alpaca_api_secret,
            paper=self.is_paper,
        )

    # -- reads -----------------------------------------------------------
    def get_account(self) -> Account:
        a = self._client.get_account()
        return Account(
            equity=float(a.equity or 0),
            cash=float(a.cash or 0),
            buying_power=float(a.buying_power or 0),
            is_paper=self.is_paper,
            currency=a.currency or "USD",
            status=str(a.status),
        )

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._client.get_all_positions():
            out.append(
                Position(
                    symbol=p.symbol,
                    qty=float(p.qty),
                    avg_entry_price=float(p.avg_entry_price),
                    current_price=float(p.current_price or 0),
                    market_value=float(p.market_value or 0),
                    unrealized_pl=float(p.unrealized_pl or 0),
                    unrealized_plpc=float(p.unrealized_plpc or 0),
                )
            )
        return out

    def get_orders(self, limit: int = 50) -> list[BrokerOrder]:
        from alpaca.trading.requests import GetOrdersRequest

        orders = self._client.get_orders(GetOrdersRequest(limit=limit, status="all"))
        return [self._to_order(o) for o in orders]

    def is_market_open(self) -> bool:
        try:
            return bool(self._client.get_clock().is_open)
        except Exception as exc:
            log.warning("could not read market clock: %s", exc)
            return False

    # -- writes ----------------------------------------------------------
    def submit_bracket_buy(
        self,
        symbol: str,
        qty: float,
        stop_price: float | None,
        target_price: float | None,
    ) -> BrokerOrder:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (
            MarketOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        if stop_price is None or stop_price <= 0:
            # A bracket without a stop is just an unprotected position.
            raise BrokerError("refusing to submit an entry without a stop-loss")

        kwargs = dict(
            symbol=symbol,
            qty=int(qty),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=round(float(stop_price), 2)),
        )
        if target_price and target_price > 0:
            kwargs["take_profit"] = TakeProfitRequest(
                limit_price=round(float(target_price), 2)
            )
        else:
            # Alpaca brackets require both legs; park the target far away so the
            # stop is the only leg that realistically fires.
            kwargs["take_profit"] = TakeProfitRequest(
                limit_price=round(float(stop_price) * 10, 2)
            )

        try:
            order = self._client.submit_order(MarketOrderRequest(**kwargs))
        except Exception as exc:
            raise BrokerError(f"Alpaca rejected the order: {exc}") from exc
        return self._to_order(order)

    def close_position(self, symbol: str) -> BrokerOrder:
        try:
            order = self._client.close_position(symbol)
        except Exception as exc:
            raise BrokerError(f"Alpaca could not close {symbol}: {exc}") from exc
        return self._to_order(order)

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _to_order(o) -> BrokerOrder:
        return BrokerOrder(
            id=str(o.id),
            symbol=o.symbol,
            side=str(getattr(o.side, "value", o.side)),
            qty=float(o.qty or 0),
            status=str(getattr(o.status, "value", o.status)),
            filled_qty=float(o.filled_qty or 0),
            filled_avg_price=(
                float(o.filled_avg_price) if o.filled_avg_price else None
            ),
            submitted_at=o.submitted_at if isinstance(o.submitted_at, datetime) else None,
            order_type=str(getattr(o.order_type, "value", o.order_type)),
            legs=[str(l.id) for l in (o.legs or [])],
        )
