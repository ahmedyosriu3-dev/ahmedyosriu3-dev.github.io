"""Risk management.

Every order passes through `check_order()` before it can reach a broker. The
rules are deliberately blunt and fail closed: when a limit cannot be evaluated
(no price, no ATR, no account), the answer is "no", not "probably fine".

Position sizing is ATR-based: risk a fixed fraction of a symbol's allocation
per trade, and let the distance to the stop decide the share count. A wide stop
buys fewer shares; a tight stop buys more. The dollars at risk stay constant,
which is the only quantity actually under your control.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.models import (
    AppState,
    AuditLog,
    DailyPnL,
    Mode,
    Order,
    WatchItem,
)
from app.trading.broker import Account, Broker, Position

KILL_SWITCH_KEY = "kill_switch"
LIVE_ENABLED_KEY = "live_enabled"


@dataclass
class SizedOrder:
    symbol: str
    qty: int
    est_price: float
    stop_price: float
    target_price: float | None
    notional: float
    risk_usd: float
    rationale: str


@dataclass
class RiskVerdict:
    ok: bool
    reason: str = ""
    sized: SizedOrder | None = None

    @property
    def blocked(self) -> bool:
        return not self.ok


# --------------------------------------------------------------------------
# App-state flags
# --------------------------------------------------------------------------
def _get_flag(key: str, default: bool = False) -> bool:
    with session_scope() as s:
        row = s.get(AppState, key)
        if row is None or not row.value:
            return default
        return row.value.strip().lower() in {"1", "true", "yes", "on"}


def _set_flag(key: str, value: bool) -> None:
    with session_scope() as s:
        row = s.get(AppState, key)
        if row is None:
            s.add(AppState(key=key, value="true" if value else "false"))
        else:
            row.value = "true" if value else "false"
            row.updated_at = datetime.now(timezone.utc)


def kill_switch_active() -> bool:
    return _get_flag(KILL_SWITCH_KEY, False)


def set_kill_switch(active: bool, reason: str = "") -> None:
    _set_flag(KILL_SWITCH_KEY, active)
    message = reason or ("Trading halted" if active else "Trading resumed")
    audit(
        "KILL_SWITCH_ON" if active else "KILL_SWITCH_OFF",
        message=message,
        level="WARNING" if active else "INFO",
    )

    # The kill switch tripping is the one event worth interrupting you for.
    from app.notify import fire
    from app.notify.events import notify_kill_switch

    fire(notify_kill_switch, active, message)


def live_enabled() -> bool:
    """Lock #2 of the live-trading triple lock (the dashboard toggle)."""
    return _get_flag(LIVE_ENABLED_KEY, False)


def set_live_enabled(enabled: bool) -> None:
    _set_flag(LIVE_ENABLED_KEY, enabled)
    audit(
        "LIVE_ENABLED" if enabled else "LIVE_DISABLED",
        message="Live trading armed" if enabled else "Live trading disarmed",
        level="WARNING" if enabled else "INFO",
    )


def live_trading_permitted() -> bool:
    """All three locks must agree before real money can move."""
    return settings.alpaca_live and live_enabled()


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------
def audit(
    event: str, message: str = "", symbol: str = "",
    level: str = "INFO", **detail,
) -> None:
    with session_scope() as s:
        s.add(
            AuditLog(
                event=event, message=message, symbol=symbol, level=level,
                detail_json=json.dumps(detail, default=str),
            )
        )


# --------------------------------------------------------------------------
# Daily loss limit
# --------------------------------------------------------------------------
def record_equity(equity: float) -> DailyPnL:
    """Track today's opening and latest equity; trip the kill switch if needed."""
    today = date.today()
    with session_scope() as s:
        row = s.execute(
            select(DailyPnL).where(DailyPnL.day == today)
        ).scalar_one_or_none()
        if row is None:
            row = DailyPnL(
                day=today, start_equity=equity, last_equity=equity
            )
            s.add(row)
        else:
            row.last_equity = equity
        start, last, tripped = row.start_equity, row.last_equity, row.kill_switch_tripped

    if start > 0 and not tripped:
        drawdown_pct = (last / start - 1.0) * 100.0
        if drawdown_pct <= -abs(settings.daily_loss_limit_pct):
            with session_scope() as s:
                r = s.execute(
                    select(DailyPnL).where(DailyPnL.day == today)
                ).scalar_one()
                r.kill_switch_tripped = True
            set_kill_switch(
                True,
                f"Daily loss limit hit: {drawdown_pct:.2f}% "
                f"(limit {settings.daily_loss_limit_pct}%)",
            )
    return row


def reset_daily_limits() -> None:
    """Called pre-open: clear a kill switch that was tripped by yesterday's loss."""
    today = date.today()
    with session_scope() as s:
        row = s.execute(
            select(DailyPnL).where(DailyPnL.day == today)
        ).scalar_one_or_none()
        if row is not None and row.kill_switch_tripped:
            return  # already tripped today; leave it alone
    if kill_switch_active():
        set_kill_switch(False, "New trading day - daily loss limit reset")


# --------------------------------------------------------------------------
# Cooldown
# --------------------------------------------------------------------------
def in_cooldown(symbol: str) -> tuple[bool, str]:
    """True if this symbol was stopped out too recently to re-enter.

    Re-entering a name straight after it stopped you out is the classic way to
    turn one loss into three.
    """
    days = settings.cooldown_days_after_stop
    if days <= 0:
        return False, ""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    with session_scope() as s:
        recent = s.execute(
            select(Order)
            .where(
                Order.symbol == symbol,
                Order.side == "sell",
                Order.submitted_at >= cutoff,
            )
            .order_by(Order.submitted_at.desc())
        ).scalars().first()
    if recent is None:
        return False, ""
    resumes = recent.submitted_at + timedelta(days=days)
    return True, (
        f"{symbol} exited on {recent.submitted_at:%Y-%m-%d}; "
        f"cooldown runs until {resumes:%Y-%m-%d}"
    )


# --------------------------------------------------------------------------
# Sizing
# --------------------------------------------------------------------------
def size_position(
    symbol: str, price: float, stop_price: float, allocation_usd: float,
    risk_pct: float, cash_available: float,
) -> tuple[int, str]:
    """Return (shares, explanation). Zero shares means "do not trade"."""
    if price <= 0:
        return 0, "no valid price"
    if stop_price <= 0 or stop_price >= price:
        return 0, (
            f"stop ${stop_price:,.2f} is not below the entry ${price:,.2f}; "
            "refusing to size a trade with no downside protection"
        )

    risk_per_share = price - stop_price
    risk_budget = allocation_usd * (risk_pct / 100.0)
    by_risk = int(risk_budget // risk_per_share)
    by_allocation = int(allocation_usd // price)
    by_cash = int(cash_available // price)

    qty = max(0, min(by_risk, by_allocation, by_cash))
    if qty == 0:
        if by_cash == 0:
            return 0, f"not enough cash for one share at ${price:,.2f}"
        if by_risk == 0:
            return 0, (
                f"stop is ${risk_per_share:,.2f} away; risking "
                f"${risk_budget:,.2f} would not buy a single share"
            )
        return 0, "allocation too small for one share"

    binding = min(
        [(by_risk, "risk budget"), (by_allocation, "allocation"), (by_cash, "cash")],
        key=lambda t: t[0],
    )[1]
    return qty, (
        f"{qty} shares at ~${price:,.2f} (${qty * price:,.2f}), "
        f"risking ${qty * risk_per_share:,.2f} to the ${stop_price:,.2f} stop "
        f"-- capped by {binding}"
    )


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------
def check_order(
    symbol: str, price: float, stop_price: float, target_price: float | None,
    watch: WatchItem, account: Account, positions: list[Position],
    broker: Broker, require_market_open: bool = True,
) -> RiskVerdict:
    """Every rule, in one place. Returns a sized order or the reason it cannot."""

    if kill_switch_active():
        return RiskVerdict(False, "Kill switch is active - all new orders are blocked")

    if not watch.enabled:
        return RiskVerdict(False, f"{symbol} is disabled in the watchlist")

    if watch.mode != Mode.SEMI_AUTO:
        return RiskVerdict(
            False, f"{symbol} is set to {watch.mode.value}, which never places orders"
        )

    if not broker.is_paper and not live_trading_permitted():
        return RiskVerdict(
            False,
            "Live trading is not armed (needs ALPACA_LIVE in .env plus the "
            "dashboard toggle)",
        )

    held = {p.symbol for p in positions}
    if symbol in held:
        return RiskVerdict(False, f"already holding {symbol}")

    if len(positions) >= settings.max_open_positions:
        return RiskVerdict(
            False,
            f"at the {settings.max_open_positions}-position limit "
            f"({len(positions)} open)",
        )

    cooling, why = in_cooldown(symbol)
    if cooling:
        return RiskVerdict(False, why)

    if account.equity <= 0:
        return RiskVerdict(False, "account equity unavailable - failing closed")

    deployed = sum(abs(p.market_value) for p in positions)
    max_deployed = account.equity * (settings.max_deployed_pct / 100.0)
    if deployed >= max_deployed:
        return RiskVerdict(
            False,
            f"${deployed:,.0f} already deployed of a ${max_deployed:,.0f} ceiling "
            f"({settings.max_deployed_pct}% of equity)",
        )

    if require_market_open and not broker.is_market_open():
        return RiskVerdict(False, "market is closed")

    headroom = min(account.buying_power, max_deployed - deployed)
    qty, explanation = size_position(
        symbol, price, stop_price, watch.allocation_usd, watch.risk_pct, headroom
    )
    if qty <= 0:
        return RiskVerdict(False, explanation)

    return RiskVerdict(
        True,
        "",
        SizedOrder(
            symbol=symbol,
            qty=qty,
            est_price=price,
            stop_price=stop_price,
            target_price=target_price,
            notional=qty * price,
            risk_usd=qty * (price - stop_price),
            rationale=explanation,
        ),
    )
