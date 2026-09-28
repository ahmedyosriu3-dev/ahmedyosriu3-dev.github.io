"""Local simulated broker.

Lets the whole app -- dashboard, approvals, positions, P&L -- run end to end
before any Alpaca keys exist, and gives the test suite a broker it can drive
without a network.

State lives in the app_state table, so it survives restarts. Stops and targets
are checked against cached daily bars whenever `mark_to_market()` runs, which
is close enough for a rehearsal and never pretends to be a real fill.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.data.store import latest_price
from app.db import session_scope
from app.models import AppState
from app.trading.broker import (
    Account,
    Broker,
    BrokerError,
    BrokerOrder,
    Position,
)

STATE_KEY = "sim_broker_state"
DEFAULT_CASH = 100_000.0


@dataclass
class _SimPosition:
    symbol: str
    qty: float
    avg_entry_price: float
    stop_price: float | None = None
    target_price: float | None = None
    opened_at: str = ""


@dataclass
class _SimState:
    cash: float = DEFAULT_CASH
    starting_cash: float = DEFAULT_CASH
    positions: dict[str, dict] = field(default_factory=dict)
    orders: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls) -> "_SimState":
        with session_scope() as s:
            row = s.get(AppState, STATE_KEY)
            if row and row.value:
                try:
                    return cls(**json.loads(row.value))
                except Exception:
                    pass
        return cls()

    def save(self) -> None:
        payload = json.dumps(asdict(self))
        with session_scope() as s:
            row = s.get(AppState, STATE_KEY)
            if row is None:
                s.add(AppState(key=STATE_KEY, value=payload))
            else:
                row.value = payload
                row.updated_at = datetime.now(timezone.utc)


class SimBroker(Broker):
    name = "simulator"
    is_paper = True

    def __init__(self) -> None:
        self._state = _SimState.load()

    # -- reads -----------------------------------------------------------
    def get_account(self) -> Account:
        self.mark_to_market()
        equity = self._state.cash + sum(
            p["qty"] * (latest_price(sym) or p["avg_entry_price"])
            for sym, p in self._state.positions.items()
        )
        return Account(
            equity=equity,
            cash=self._state.cash,
            buying_power=self._state.cash,
            is_paper=True,
            status="SIMULATED",
        )

    def get_positions(self) -> list[Position]:
        out = []
        for sym, p in self._state.positions.items():
            price = latest_price(sym) or p["avg_entry_price"]
            mv = p["qty"] * price
            cost = p["qty"] * p["avg_entry_price"]
            out.append(
                Position(
                    symbol=sym,
                    qty=p["qty"],
                    avg_entry_price=p["avg_entry_price"],
                    current_price=price,
                    market_value=mv,
                    unrealized_pl=mv - cost,
                    unrealized_plpc=(mv - cost) / cost if cost else 0.0,
                )
            )
        return out

    def get_orders(self, limit: int = 50) -> list[BrokerOrder]:
        return [
            BrokerOrder(
                id=o["id"], symbol=o["symbol"], side=o["side"], qty=o["qty"],
                status=o["status"], filled_qty=o.get("filled_qty", 0.0),
                filled_avg_price=o.get("filled_avg_price"),
                submitted_at=datetime.fromisoformat(o["submitted_at"]),
                order_type=o.get("order_type", "market"),
            )
            for o in reversed(self._state.orders[-limit:])
        ]

    def is_market_open(self) -> bool:
        now = datetime.now(timezone.utc)
        # Rough US session in UTC; the simulator does not need exchange holidays.
        return now.weekday() < 5 and 13 <= now.hour < 21

    # -- writes ----------------------------------------------------------
    def submit_bracket_buy(
        self, symbol: str, qty: float, stop_price: float | None,
        target_price: float | None,
    ) -> BrokerOrder:
        if stop_price is None or stop_price <= 0:
            raise BrokerError("refusing to submit an entry without a stop-loss")

        price = latest_price(symbol)
        if price is None:
            raise BrokerError(f"no price available for {symbol}")

        qty = float(int(qty))
        cost = qty * price
        if qty < 1:
            raise BrokerError("quantity rounds to zero shares")
        if cost > self._state.cash:
            raise BrokerError(
                f"insufficient simulated cash: need ${cost:,.2f}, have "
                f"${self._state.cash:,.2f}"
            )

        self._state.cash -= cost
        existing = self._state.positions.get(symbol)
        if existing:
            total_qty = existing["qty"] + qty
            existing["avg_entry_price"] = (
                existing["avg_entry_price"] * existing["qty"] + cost
            ) / total_qty
            existing["qty"] = total_qty
            existing["stop_price"] = stop_price
            existing["target_price"] = target_price
        else:
            self._state.positions[symbol] = asdict(
                _SimPosition(
                    symbol=symbol, qty=qty, avg_entry_price=price,
                    stop_price=stop_price, target_price=target_price,
                    opened_at=datetime.now(timezone.utc).isoformat(),
                )
            )

        return self._record_order(symbol, "buy", qty, price)

    def close_position(self, symbol: str) -> BrokerOrder:
        p = self._state.positions.get(symbol)
        if not p:
            raise BrokerError(f"no simulated position in {symbol}")
        price = latest_price(symbol) or p["avg_entry_price"]
        qty = p["qty"]
        self._state.cash += qty * price
        del self._state.positions[symbol]
        return self._record_order(symbol, "sell", qty, price)

    def mark_to_market(self) -> list[str]:
        """Fire any stop or target that the latest close has passed.

        Returns the symbols that were closed out.
        """
        closed: list[str] = []
        for sym in list(self._state.positions):
            p = self._state.positions[sym]
            price = latest_price(sym)
            if price is None:
                continue
            stop, target = p.get("stop_price"), p.get("target_price")
            # Pessimistic, same as the backtester: stop is checked first.
            if stop and price <= stop:
                self.close_position(sym)
                closed.append(sym)
            elif target and price >= target:
                self.close_position(sym)
                closed.append(sym)
        if closed:
            self._state.save()
        return closed

    def reset(self, cash: float = DEFAULT_CASH) -> None:
        self._state = _SimState(cash=cash, starting_cash=cash)
        self._state.save()

    # -- helpers ---------------------------------------------------------
    def _record_order(
        self, symbol: str, side: str, qty: float, price: float
    ) -> BrokerOrder:
        oid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        self._state.orders.append(
            {
                "id": oid, "symbol": symbol, "side": side, "qty": qty,
                "status": "filled", "filled_qty": qty, "filled_avg_price": price,
                "submitted_at": now.isoformat(), "order_type": "market",
            }
        )
        self._state.save()
        return BrokerOrder(
            id=oid, symbol=symbol, side=side, qty=qty, status="filled",
            filled_qty=qty, filled_avg_price=price, submitted_at=now,
        )
