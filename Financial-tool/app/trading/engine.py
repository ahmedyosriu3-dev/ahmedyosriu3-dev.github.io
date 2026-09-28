"""The decision pipeline.

    fresh bars
      -> the symbol's assigned strategy produces a signal
      -> other strategies vote, giving an agreement score
      -> SIGNAL_ONLY: record it, show it, stop
      -> SEMI_AUTO:   size it, risk-check it, create a proposal you must approve
      -> on approval:  submit a bracket order and write the audit trail

Nothing here submits an order on its own. The only path from a signal to a
broker runs through `approve_proposal()`, which a human calls.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select

from app.analysis.selector import current_assignment
from app.analysis.strategies import get_strategy, list_strategies
from app.config import settings
from app.data.store import get_bars
from app.db import session_scope
from app.models import (
    Mode,
    Order,
    OrderProposal,
    ProposalStatus,
    Signal,
    SignalType,
    WatchItem,
)
from app.trading.broker import Broker, BrokerError
from app.trading.risk import audit, check_order, record_equity

log = logging.getLogger(__name__)


@dataclass
class SignalView:
    """A signal plus everything the dashboard wants to say about it."""

    symbol: str
    strategy: str
    strategy_label: str
    signal: SignalType
    price: float
    bar_ts: datetime
    stop_price: float | None
    target_price: float | None
    reason: str
    agreement: int
    agreement_total: int
    position_now: int
    no_edge: bool = False
    note: str = ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_watchlist(enabled_only: bool = True) -> list[WatchItem]:
    with session_scope() as s:
        q = select(WatchItem).order_by(WatchItem.symbol)
        if enabled_only:
            q = q.where(WatchItem.enabled.is_(True))
        return list(s.execute(q).scalars())


def _agreement(bars: pd.DataFrame, winner_key: str) -> tuple[int, int]:
    """How many of the other strategies also want to be long right now.

    Not used to gate trades -- it is shown to you as context. A signal every
    strategy agrees with is a different proposition from a lone outlier.
    """
    agree = total = 0
    for cls in list_strategies():
        try:
            res = get_strategy(cls.key).run(bars)
        except Exception:
            continue
        if res.position.empty:
            continue
        total += 1
        if float(res.position.iloc[-1]) == 1.0:
            agree += 1
    return agree, total


def evaluate_symbol(watch: WatchItem, lookback_days: int = 1200) -> SignalView | None:
    """Run the assigned strategy over fresh bars and describe what it says."""
    end = _utcnow()
    bars = get_bars(watch.symbol, end - timedelta(days=lookback_days), end)
    if bars.empty:
        return None

    assignment = current_assignment(watch.symbol)

    if watch.pinned_strategy:
        key, params = watch.pinned_strategy, {}
        note = f"Pinned to {key} by you (selector overridden)."
    elif assignment is None:
        return SignalView(
            symbol=watch.symbol, strategy="", strategy_label="Not evaluated",
            signal=SignalType.HOLD, price=float(bars["close"].iloc[-1]),
            bar_ts=bars.index[-1].to_pydatetime(), stop_price=None,
            target_price=None, reason="", agreement=0, agreement_total=0,
            position_now=0, no_edge=False,
            note="No strategy assigned yet - run strategy selection.",
        )
    elif assignment.no_edge or not assignment.strategy:
        return SignalView(
            symbol=watch.symbol, strategy="", strategy_label="No edge",
            signal=SignalType.HOLD, price=float(bars["close"].iloc[-1]),
            bar_ts=bars.index[-1].to_pydatetime(), stop_price=None,
            target_price=None, reason="", agreement=0, agreement_total=0,
            position_now=0, no_edge=True,
            note="No strategy cleared the bar for this symbol, so it stays flat.",
        )
    else:
        key = assignment.strategy
        params = json.loads(assignment.params_json or "{}")
        note = ""

    strat = get_strategy(key, **params)
    if len(bars) < strat.min_bars + 2:
        return SignalView(
            symbol=watch.symbol, strategy=key, strategy_label=strat.label,
            signal=SignalType.HOLD, price=float(bars["close"].iloc[-1]),
            bar_ts=bars.index[-1].to_pydatetime(), stop_price=None,
            target_price=None, reason="", agreement=0, agreement_total=0,
            position_now=0,
            note=f"Needs {strat.min_bars} bars of history, has {len(bars)}.",
        )

    res = strat.run(bars)
    pos_now = int(res.position.iloc[-1])
    pos_prev = int(res.position.iloc[-2]) if len(res.position) > 1 else 0

    if pos_now == 1 and pos_prev == 0:
        sig = SignalType.BUY
    elif pos_now == 0 and pos_prev == 1:
        sig = SignalType.SELL
    else:
        sig = SignalType.HOLD

    reason = str(res.reason.iloc[-1] or "")
    if not reason:
        reason = (
            "Holding - entry conditions still met."
            if pos_now == 1
            else "Flat - no entry condition met."
        )

    agree, total = _agreement(bars, key)
    stop = res.stop.iloc[-1]
    target = res.target.iloc[-1]

    return SignalView(
        symbol=watch.symbol,
        strategy=key,
        strategy_label=strat.label,
        signal=sig,
        price=float(bars["close"].iloc[-1]),
        bar_ts=bars.index[-1].to_pydatetime(),
        stop_price=None if pd.isna(stop) else float(stop),
        target_price=None if pd.isna(target) else float(target),
        reason=reason,
        agreement=agree,
        agreement_total=total,
        position_now=pos_now,
        note=note,
    )


def _already_recorded(symbol: str, bar_ts: datetime, sig: SignalType) -> bool:
    with session_scope() as s:
        return s.execute(
            select(Signal.id).where(
                Signal.symbol == symbol,
                Signal.bar_ts == bar_ts,
                Signal.signal == sig,
            )
        ).first() is not None


def record_signal(view: SignalView) -> int | None:
    """Persist a BUY/SELL signal once per bar. Returns the row id."""
    if view.signal == SignalType.HOLD:
        return None
    if _already_recorded(view.symbol, view.bar_ts, view.signal):
        return None

    with session_scope() as s:
        row = Signal(
            symbol=view.symbol, strategy=view.strategy, signal=view.signal,
            bar_ts=view.bar_ts, price=view.price, stop_price=view.stop_price,
            target_price=view.target_price, reason=view.reason,
            agreement=view.agreement, agreement_total=view.agreement_total,
        )
        s.add(row)
        s.flush()
        sid = row.id

    audit(
        "SIGNAL", message=f"{view.signal.value} {view.symbol} - {view.reason}",
        symbol=view.symbol, strategy=view.strategy, price=view.price,
    )
    return sid


def propose_order(
    view: SignalView, watch: WatchItem, broker: Broker, signal_id: int,
    require_market_open: bool = True,
) -> OrderProposal | None:
    """Risk-check a BUY signal and queue it for your approval."""
    if view.signal != SignalType.BUY or watch.mode != Mode.SEMI_AUTO:
        return None

    account = broker.get_account()
    positions = broker.get_positions()
    record_equity(account.equity)

    verdict = check_order(
        symbol=view.symbol, price=view.price, stop_price=view.stop_price or 0.0,
        target_price=view.target_price, watch=watch, account=account,
        positions=positions, broker=broker,
        require_market_open=require_market_open,
    )

    if verdict.blocked:
        audit(
            "PROPOSAL_BLOCKED", message=verdict.reason, symbol=view.symbol,
            level="INFO",
        )
        return None

    sized = verdict.sized
    with session_scope() as s:
        prop = OrderProposal(
            signal_id=signal_id, symbol=sized.symbol, side="buy", qty=sized.qty,
            est_price=sized.est_price, stop_price=sized.stop_price,
            target_price=sized.target_price, notional=sized.notional,
            risk_usd=sized.risk_usd,
            rationale=f"{view.reason} | {sized.rationale}",
            status=ProposalStatus.PENDING,
            expires_at=_utcnow() + timedelta(minutes=settings.proposal_ttl_minutes),
        )
        s.add(prop)
        s.flush()
        s.refresh(prop)

    audit(
        "PROPOSAL_CREATED",
        message=f"BUY {sized.qty} {sized.symbol} @ ~${sized.est_price:,.2f}",
        symbol=sized.symbol, qty=sized.qty, risk_usd=sized.risk_usd,
    )

    from app.notify import fire
    from app.notify.events import notify_proposal

    fire(notify_proposal, prop)
    return prop


def pending_proposals() -> list[OrderProposal]:
    expire_stale()
    with session_scope() as s:
        return list(
            s.execute(
                select(OrderProposal)
                .where(OrderProposal.status == ProposalStatus.PENDING)
                .order_by(OrderProposal.created_at.desc())
            ).scalars()
        )


def expire_stale() -> int:
    """Time out proposals nobody acted on.

    A share count computed from Tuesday's close is not a sensible order on
    Thursday, so stale proposals lapse rather than linger.
    """
    now = _utcnow()
    n = 0
    with session_scope() as s:
        rows = s.execute(
            select(OrderProposal).where(
                OrderProposal.status == ProposalStatus.PENDING,
                OrderProposal.expires_at <= now,
            )
        ).scalars().all()
        for r in rows:
            r.status = ProposalStatus.EXPIRED
            r.decided_at = now
            n += 1
    if n:
        audit("PROPOSALS_EXPIRED", message=f"{n} proposal(s) expired unactioned")
    return n


def approve_proposal(
    proposal_id: int, broker: Broker, require_market_open: bool = True
) -> tuple[bool, str]:
    """The only path from a signal to a real order. Called by a human.

    `require_market_open` exists so the UI can offer a deliberate override
    when you want an order queued for the next session -- it is never
    relaxed silently.
    """
    with session_scope() as s:
        prop = s.get(OrderProposal, proposal_id)
        if prop is None:
            return False, "proposal not found"
        if prop.status != ProposalStatus.PENDING:
            return False, f"proposal is already {prop.status.value.lower()}"
        if prop.expires_at <= _utcnow():
            prop.status = ProposalStatus.EXPIRED
            prop.decided_at = _utcnow()
            return False, "proposal expired before it was approved"
        snapshot = {
            "symbol": prop.symbol, "qty": prop.qty, "stop": prop.stop_price,
            "target": prop.target_price, "est_price": prop.est_price,
        }

    # Re-check the gate at approval time -- minutes may have passed and the
    # kill switch, position count or market hours may all have changed.
    watch = get_watch(snapshot["symbol"])
    if watch is None:
        return _fail(proposal_id, "symbol is no longer on the watchlist")

    account = broker.get_account()
    positions = broker.get_positions()
    verdict = check_order(
        symbol=snapshot["symbol"], price=snapshot["est_price"],
        stop_price=snapshot["stop"] or 0.0, target_price=snapshot["target"],
        watch=watch, account=account, positions=positions, broker=broker,
        require_market_open=require_market_open,
    )
    if verdict.blocked:
        return _fail(proposal_id, f"risk check failed at approval: {verdict.reason}")

    try:
        order = broker.submit_bracket_buy(
            symbol=snapshot["symbol"],
            qty=min(snapshot["qty"], verdict.sized.qty),
            stop_price=snapshot["stop"],
            target_price=snapshot["target"],
        )
    except BrokerError as exc:
        return _fail(proposal_id, str(exc))

    now = _utcnow()
    with session_scope() as s:
        prop = s.get(OrderProposal, proposal_id)
        prop.status = ProposalStatus.APPROVED
        prop.decided_at = now
        s.add(
            Order(
                proposal_id=proposal_id, broker_order_id=order.id,
                symbol=order.symbol, side=order.side, qty=order.qty,
                filled_qty=order.filled_qty,
                filled_avg_price=order.filled_avg_price,
                status=order.status, is_paper=broker.is_paper,
                submitted_at=now, updated_at=now,
            )
        )

    audit(
        "ORDER_SUBMITTED",
        message=(
            f"BUY {order.qty} {order.symbol} via {broker.describe()} "
            f"(stop ${snapshot['stop']:,.2f})"
        ),
        symbol=order.symbol, level="WARNING" if not broker.is_paper else "INFO",
        broker_order_id=order.id,
    )

    from app.notify import fire
    from app.notify.events import notify_order_submitted, retire_proposal_message

    fire(retire_proposal_message, proposal_id,
         f"Approved - order {order.id} submitted.")
    fire(notify_order_submitted, order.symbol, order.qty, snapshot["est_price"],
         snapshot["stop"], not broker.is_paper, order.id)
    return True, f"Submitted: buy {order.qty:.0f} {order.symbol}"


def reject_proposal(proposal_id: int, reason: str = "rejected by user") -> bool:
    with session_scope() as s:
        prop = s.get(OrderProposal, proposal_id)
        if prop is None or prop.status != ProposalStatus.PENDING:
            return False
        prop.status = ProposalStatus.REJECTED
        prop.decided_at = _utcnow()
        prop.error = reason
        symbol = prop.symbol
    audit("PROPOSAL_REJECTED", message=reason, symbol=symbol)

    from app.notify import fire
    from app.notify.events import retire_proposal_message

    fire(retire_proposal_message, proposal_id, f"Dismissed - {reason}.")
    return True


def _fail(proposal_id: int, reason: str) -> tuple[bool, str]:
    with session_scope() as s:
        prop = s.get(OrderProposal, proposal_id)
        if prop is not None:
            prop.status = ProposalStatus.FAILED
            prop.error = reason
            prop.decided_at = _utcnow()
            symbol = prop.symbol
        else:
            symbol = ""
    audit("ORDER_FAILED", message=reason, symbol=symbol, level="ERROR")
    return False, reason


def get_watch(symbol: str) -> WatchItem | None:
    with session_scope() as s:
        return s.execute(
            select(WatchItem).where(WatchItem.symbol == symbol.upper())
        ).scalar_one_or_none()


def run_signal_scan(
    broker: Broker, require_market_open: bool = True
) -> list[SignalView]:
    """Evaluate the whole watchlist; record signals and queue proposals."""
    views: list[SignalView] = []
    for watch in get_watchlist():
        try:
            view = evaluate_symbol(watch)
        except Exception as exc:
            log.exception("evaluation failed for %s", watch.symbol)
            audit("EVAL_FAILED", message=str(exc), symbol=watch.symbol, level="ERROR")
            continue
        if view is None:
            continue
        views.append(view)

        if watch.mode == Mode.OFF:
            continue

        sid = record_signal(view)
        if sid and watch.mode == Mode.SIGNAL_ONLY:
            # Semi-auto symbols get a richer message with buttons when the
            # proposal is created, so only signal-only symbols announce here.
            from app.notify import fire
            from app.notify.events import notify_signal

            fire(notify_signal, view)
        if sid and watch.mode == Mode.SEMI_AUTO:
            try:
                propose_order(view, watch, broker, sid, require_market_open)
            except Exception as exc:
                log.exception("proposal failed for %s", watch.symbol)
                audit("PROPOSAL_FAILED", message=str(exc), symbol=watch.symbol,
                      level="ERROR")
    return views


def propose_manual(
    symbol: str, broker: Broker, require_market_open: bool = False,
    atr_mult: float = 2.0, reward_mult: float = 3.0,
) -> tuple[OrderProposal | None, str]:
    """Size a buy you decided on yourself, with the same rails as a signal.

    The strategies are not the only source of a good idea, and forcing you to
    leave the app to act on your own read was the fastest way to end up with
    an unprotected position placed elsewhere. This goes through the identical
    risk gate and still produces a proposal you have to approve -- the only
    thing it skips is waiting for a strategy to agree with you.

    The stop is ATR-based rather than chosen by you, because a stop picked to
    justify a position size is not a stop.
    """
    from app.analysis import indicators as ind

    symbol = symbol.upper().strip()
    watch = get_watch(symbol)
    if watch is None:
        return None, f"{symbol} is not on your watchlist"

    end = _utcnow()
    bars = get_bars(symbol, end - timedelta(days=400), end)
    if bars.empty:
        return None, f"no price data for {symbol}"

    price = float(bars["close"].iloc[-1])
    atr_series = ind.atr(bars["high"], bars["low"], bars["close"], 14)
    atr = float(atr_series.iloc[-1]) if not atr_series.empty else float("nan")
    if pd.isna(atr) or atr <= 0:
        return None, f"cannot measure volatility for {symbol}; refusing to size blind"

    stop = price - atr_mult * atr
    target = price + reward_mult * atr

    account = broker.get_account()
    positions = broker.get_positions()
    record_equity(account.equity)

    verdict = check_order(
        symbol=symbol, price=price, stop_price=stop, target_price=target,
        watch=watch, account=account, positions=positions, broker=broker,
        require_market_open=require_market_open,
    )
    if verdict.blocked:
        audit("MANUAL_BLOCKED", message=verdict.reason, symbol=symbol)
        return None, verdict.reason

    sized = verdict.sized
    rationale = (
        f"Manual entry you requested (no strategy signal). "
        f"Stop {atr_mult:.0f}x ATR below at ${stop:,.2f}, target "
        f"{reward_mult:.0f}x ATR above. | {sized.rationale}"
    )

    with session_scope() as s:
        row = Signal(
            symbol=symbol, strategy="manual", signal=SignalType.BUY,
            bar_ts=bars.index[-1].to_pydatetime(), price=price,
            stop_price=stop, target_price=target,
            reason="Manual entry requested from the dashboard.",
            agreement=0, agreement_total=0,
        )
        s.add(row)
        s.flush()
        signal_id = row.id

        prop = OrderProposal(
            signal_id=signal_id, symbol=symbol, side="buy", qty=sized.qty,
            est_price=sized.est_price, stop_price=sized.stop_price,
            target_price=sized.target_price, notional=sized.notional,
            risk_usd=sized.risk_usd, rationale=rationale,
            status=ProposalStatus.PENDING,
            expires_at=_utcnow() + timedelta(minutes=settings.proposal_ttl_minutes),
        )
        s.add(prop)
        s.flush()
        s.refresh(prop)

    audit(
        "MANUAL_PROPOSAL",
        message=f"manual BUY {sized.qty} {symbol} @ ~${sized.est_price:,.2f}",
        symbol=symbol, qty=sized.qty, risk_usd=sized.risk_usd,
    )

    from app.notify import fire
    from app.notify.events import notify_proposal

    fire(notify_proposal, prop)
    return prop, f"Proposed: buy {sized.qty:.0f} {symbol}"
