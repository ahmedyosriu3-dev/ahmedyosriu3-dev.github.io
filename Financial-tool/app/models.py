"""Database tables.

Everything the app knows that is not a price lives here: the watchlist and its
per-symbol execution settings, the strategy the selector assigned to each
symbol, generated signals, order proposals awaiting your approval, submitted
orders, and an append-only audit log.
"""
from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------
class Mode(str, enum.Enum):
    """How much authority the app has over a given symbol."""

    OFF = "OFF"                  # watched but silent
    SIGNAL_ONLY = "SIGNAL_ONLY"  # tells you, never proposes an order
    SEMI_AUTO = "SEMI_AUTO"      # proposes an order, you click Approve


class SignalType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class ProposalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


# --------------------------------------------------------------------------
# Price cache
# --------------------------------------------------------------------------
class Bar(Base):
    """One OHLCV bar. The local price cache backing every calculation."""

    __tablename__ = "bars"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "ts", name="uq_bar"),
        Index("ix_bars_symbol_tf_ts", "symbol", "timeframe", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="1D")
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    adj_close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="yfinance")


# --------------------------------------------------------------------------
# Watchlist
# --------------------------------------------------------------------------
class WatchItem(Base):
    """A symbol you care about, plus how much rope the app gets on it."""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="")
    mode: Mapped[Mode] = mapped_column(Enum(Mode), default=Mode.SIGNAL_ONLY)
    allocation_usd: Mapped[float] = mapped_column(Float, default=1000.0)
    risk_pct: Mapped[float] = mapped_column(Float, default=1.0)
    pinned_strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StrategyAssignment(Base):
    """Which strategy the selector decided has earned the right to trade a symbol.

    `strategy` is NULL when nothing cleared the bar -- the symbol is marked
    NO_EDGE and deliberately produces no signals.
    """

    __tablename__ = "strategy_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    all_results_json: Mapped[str] = mapped_column(Text, default="[]")
    no_edge: Mapped[bool] = mapped_column(Boolean, default=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------
# Signals / proposals / orders
# --------------------------------------------------------------------------
class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_symbol_ts", "symbol", "bar_ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    signal: Mapped[SignalType] = mapped_column(Enum(SignalType), nullable=False)
    bar_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    agreement: Mapped[int] = mapped_column(Integer, default=0)   # how many strategies concur
    agreement_total: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrderProposal(Base):
    """A sized, risk-checked order waiting for you to press Approve."""

    __tablename__ = "order_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    est_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    notional: Mapped[float] = mapped_column(Float, default=0.0)
    risk_usd: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[ProposalStatus] = mapped_column(
        Enum(ProposalStatus), default=ProposalStatus.PENDING, index=True
    )
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Order(Base):
    """An order actually submitted to the broker."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    broker_order_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    filled_avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="new")
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """Append-only record of everything the app decided or did."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    event: Mapped[str] = mapped_column(String(48), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")


class AppState(Base):
    """Small mutable singletons: kill switch, live toggle, job timestamps."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DailyPnL(Base):
    """Per-day equity snapshot, used to enforce the daily loss limit."""

    __tablename__ = "daily_pnl"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    start_equity: Mapped[float] = mapped_column(Float, default=0.0)
    last_equity: Mapped[float] = mapped_column(Float, default=0.0)
    kill_switch_tripped: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------
# Idea discovery
# --------------------------------------------------------------------------
class ScoutIdea(Base):
    """A candidate the scout surfaced, with the evidence behind the score.

    Ideas are suggestions to *look at*, never orders. Nothing downstream reads
    this table -- adding a symbol to the watchlist is a decision you make.
    """

    __tablename__ = "scout_ideas"
    __table_args__ = (Index("ix_scout_run_score", "run_id", "score"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="")
    universe: Mapped[str] = mapped_column(String(24), default="")
    asset_class: Mapped[str] = mapped_column(String(16), default="equity")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)

    # The component scores, each 0-100, so the UI can show *why*.
    trend_score: Mapped[float] = mapped_column(Float, default=0.0)
    momentum_score: Mapped[float] = mapped_column(Float, default=0.0)
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    diversify_score: Mapped[float] = mapped_column(Float, default=0.0)
    value_score: Mapped[float] = mapped_column(Float, default=0.0)

    ret_1m: Mapped[float] = mapped_column(Float, default=0.0)
    ret_3m: Mapped[float] = mapped_column(Float, default=0.0)
    ret_6m: Mapped[float] = mapped_column(Float, default=0.0)
    ret_12m: Mapped[float] = mapped_column(Float, default=0.0)
    volatility: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    dollar_volume: Mapped[float] = mapped_column(Float, default=0.0)
    corr_to_book: Mapped[float] = mapped_column(Float, default=0.0)

    headline: Mapped[str] = mapped_column(Text, default="")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    cautions_json: Mapped[str] = mapped_column(Text, default="[]")
    spark_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class DogsEntry(Base):
    """One Dow component's place in a year's Dogs of the Dow ranking.

    A row is a snapshot, not a live quote: `price` and `dividend_yield` are
    what they were on `ranking_date`, because that is the date the strategy
    picks on. Recomputing them later would quietly change history.

    Like `ScoutIdea`, nothing downstream reads this. It is a list to look at.
    """

    __tablename__ = "dogs_of_the_dow"
    __table_args__ = (
        UniqueConstraint("year", "symbol", name="uq_dogs_year_symbol"),
        Index("ix_dogs_year_rank", "year", "rank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    ranking_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="")
    rank: Mapped[int] = mapped_column(Integer, default=0)

    price: Mapped[float] = mapped_column(Float, default=0.0)
    annual_dividend: Mapped[float] = mapped_column(Float, default=0.0)
    dividend_yield: Mapped[float] = mapped_column(Float, default=0.0)
    trailing_12m: Mapped[float] = mapped_column(Float, default=0.0)
    payments_per_year: Mapped[int] = mapped_column(Integer, default=0)
    last_ex_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    is_dog: Mapped[bool] = mapped_column(Boolean, default=False)
    is_small_dog: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NotifiedEvent(Base):
    """Dedupe ledger so a restart or a re-scan cannot re-send the same alert."""

    __tablename__ = "notified_events"
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_notified"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(96), nullable=False)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
