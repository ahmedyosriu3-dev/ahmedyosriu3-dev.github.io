"""Broker interface.

Everything the app does to an account goes through this ABC, so the Alpaca
implementation can be swapped for a local simulator (no keys needed) or an
IBKR adapter later without the engine or the UI noticing.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Account:
    equity: float = 0.0
    cash: float = 0.0
    buying_power: float = 0.0
    is_paper: bool = True
    currency: str = "USD"
    status: str = "UNKNOWN"


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float
    unrealized_plpc: float


@dataclass
class BrokerOrder:
    id: str
    symbol: str
    side: str
    qty: float
    status: str
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    submitted_at: datetime | None = None
    order_type: str = "market"
    legs: list[str] = field(default_factory=list)


class BrokerError(RuntimeError):
    pass


class Broker(ABC):
    name: str = "base"
    is_paper: bool = True

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_orders(self, limit: int = 50) -> list[BrokerOrder]: ...

    @abstractmethod
    def submit_bracket_buy(
        self,
        symbol: str,
        qty: float,
        stop_price: float | None,
        target_price: float | None,
    ) -> BrokerOrder:
        """Buy with an attached protective stop. Never submit a naked entry."""

    @abstractmethod
    def close_position(self, symbol: str) -> BrokerOrder: ...

    @abstractmethod
    def is_market_open(self) -> bool: ...

    def describe(self) -> str:
        return f"{self.name} ({'paper' if self.is_paper else 'LIVE'})"
