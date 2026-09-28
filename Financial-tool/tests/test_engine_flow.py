"""End-to-end: signal -> proposal -> approval -> order, against the simulator.

Uses a temporary database so it never touches your real one.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest


@pytest.fixture()
def temp_db(monkeypatch):
    """Point the whole app at a throwaway SQLite file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db_mod

    engine = create_engine(f"sqlite:///{path}", future=True,
                           connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(bind=engine, autoflush=False,
                                     expire_on_commit=False))

    from app.models import Base
    Base.metadata.create_all(engine)

    yield path

    engine.dispose()
    try:
        os.unlink(path)
    except PermissionError:
        pass


@pytest.fixture()
def fake_bars(monkeypatch):
    """A deterministic uptrend so price lookups never hit the network."""
    idx = pd.date_range(end=datetime.now(), periods=400, freq="B")
    close = pd.Series([100 + i * 0.5 for i in range(400)], index=idx)
    bars = pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "adj_close": close, "volume": 1e6},
        index=idx,
    )

    import app.data.store as store
    monkeypatch.setattr(store, "get_bars", lambda *a, **k: bars)
    monkeypatch.setattr(store, "latest_price", lambda *a, **k: float(close.iloc[-1]))

    import app.trading.sim_broker as sim
    monkeypatch.setattr(sim, "latest_price", lambda *a, **k: float(close.iloc[-1]))
    return bars


def _add_watch(symbol="TEST", mode=None, allocation=10_000.0):
    from app.db import session_scope
    from app.models import Mode, WatchItem

    with session_scope() as s:
        s.add(WatchItem(symbol=symbol, name=symbol,
                        mode=mode or Mode.SEMI_AUTO,
                        allocation_usd=allocation, risk_pct=1.0))


def test_simulator_starts_with_clean_cash(temp_db, fake_bars):
    from app.trading.sim_broker import SimBroker

    b = SimBroker()
    acct = b.get_account()
    assert acct.cash == pytest.approx(100_000.0)
    assert acct.is_paper is True
    assert b.get_positions() == []


def test_bracket_buy_without_a_stop_is_refused(temp_db, fake_bars):
    from app.trading.broker import BrokerError
    from app.trading.sim_broker import SimBroker

    b = SimBroker()
    with pytest.raises(BrokerError, match="without a stop-loss"):
        b.submit_bracket_buy("TEST", 10, stop_price=None, target_price=None)


def test_buy_then_close_round_trips_cash(temp_db, fake_bars):
    from app.trading.sim_broker import SimBroker

    b = SimBroker()
    start_cash = b.get_account().cash
    price = 299.5  # last close of the fake series

    b.submit_bracket_buy("TEST", 10, stop_price=price * 0.9, target_price=price * 1.2)
    assert len(b.get_positions()) == 1
    assert b.get_account().cash == pytest.approx(start_cash - 10 * price)

    b.close_position("TEST")
    assert b.get_positions() == []
    assert b.get_account().cash == pytest.approx(start_cash)


def test_kill_switch_blocks_a_proposal(temp_db, fake_bars):
    from app.models import Mode
    from app.trading.engine import get_watch, propose_order, SignalView
    from app.models import SignalType
    from app.trading.risk import set_kill_switch
    from app.trading.sim_broker import SimBroker

    _add_watch()
    set_kill_switch(True, "test")

    view = SignalView(
        symbol="TEST", strategy="ma_cross", strategy_label="EMA",
        signal=SignalType.BUY, price=299.5, bar_ts=datetime.now(),
        stop_price=280.0, target_price=340.0, reason="test",
        agreement=3, agreement_total=5, position_now=1,
    )
    prop = propose_order(view, get_watch("TEST"), SimBroker(), signal_id=1,
                         require_market_open=False)
    assert prop is None, "kill switch must block proposals"


def test_signal_only_mode_never_proposes(temp_db, fake_bars):
    from app.models import Mode, SignalType
    from app.trading.engine import get_watch, propose_order, SignalView
    from app.trading.sim_broker import SimBroker

    _add_watch(mode=Mode.SIGNAL_ONLY)
    view = SignalView(
        symbol="TEST", strategy="ma_cross", strategy_label="EMA",
        signal=SignalType.BUY, price=299.5, bar_ts=datetime.now(),
        stop_price=280.0, target_price=340.0, reason="test",
        agreement=3, agreement_total=5, position_now=1,
    )
    assert propose_order(view, get_watch("TEST"), SimBroker(), 1,
                         require_market_open=False) is None


def test_full_approval_flow_creates_an_order(temp_db, fake_bars):
    from app.models import Order, ProposalStatus, SignalType
    from app.db import session_scope
    from app.trading.engine import (
        approve_proposal, get_watch, pending_proposals, propose_order, SignalView,
    )
    from app.trading.sim_broker import SimBroker

    _add_watch()
    broker = SimBroker()

    view = SignalView(
        symbol="TEST", strategy="ma_cross", strategy_label="EMA",
        signal=SignalType.BUY, price=299.5, bar_ts=datetime.now(),
        stop_price=280.0, target_price=340.0,
        reason="EMA20 crossed above EMA50", agreement=4, agreement_total=5,
        position_now=1,
    )

    prop = propose_order(view, get_watch("TEST"), broker, signal_id=1,
                         require_market_open=False)
    assert prop is not None
    # $10,000 allocation, 1% risk = $100; stop is $19.50 away -> 5 shares.
    assert prop.qty == 5
    assert prop.status == ProposalStatus.PENDING

    ok, msg = approve_proposal(prop.id, broker, require_market_open=False)
    assert ok, msg

    assert len(broker.get_positions()) == 1
    with session_scope() as s:
        orders = s.query(Order).all()
        assert len(orders) == 1
        assert orders[0].symbol == "TEST"
        assert orders[0].is_paper is True


def test_a_proposal_cannot_be_approved_twice(temp_db, fake_bars):
    from app.models import SignalType
    from app.trading.engine import (
        approve_proposal, get_watch, propose_order, SignalView,
    )
    from app.trading.sim_broker import SimBroker

    _add_watch()
    broker = SimBroker()
    view = SignalView(
        symbol="TEST", strategy="ma_cross", strategy_label="EMA",
        signal=SignalType.BUY, price=299.5, bar_ts=datetime.now(),
        stop_price=280.0, target_price=340.0, reason="test",
        agreement=4, agreement_total=5, position_now=1,
    )
    prop = propose_order(view, get_watch("TEST"), broker, 1,
                         require_market_open=False)

    ok1, _ = approve_proposal(prop.id, broker, require_market_open=False)
    ok2, msg2 = approve_proposal(prop.id, broker, require_market_open=False)
    assert ok1 is True
    assert ok2 is False and "already" in msg2


def test_expired_proposals_cannot_be_approved(temp_db, fake_bars):
    from app.db import session_scope
    from app.models import OrderProposal, ProposalStatus, SignalType
    from app.trading.engine import (
        approve_proposal, get_watch, propose_order, SignalView,
    )
    from app.trading.sim_broker import SimBroker

    _add_watch()
    broker = SimBroker()
    view = SignalView(
        symbol="TEST", strategy="ma_cross", strategy_label="EMA",
        signal=SignalType.BUY, price=299.5, bar_ts=datetime.now(),
        stop_price=280.0, target_price=340.0, reason="test",
        agreement=4, agreement_total=5, position_now=1,
    )
    prop = propose_order(view, get_watch("TEST"), broker, 1,
                         require_market_open=False)

    with session_scope() as s:
        row = s.get(OrderProposal, prop.id)
        row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)

    ok, msg = approve_proposal(prop.id, broker, require_market_open=False)
    assert ok is False and "expired" in msg


def test_rejecting_a_proposal_places_no_order(temp_db, fake_bars):
    from app.db import session_scope
    from app.models import Order, SignalType
    from app.trading.engine import (
        get_watch, propose_order, reject_proposal, SignalView,
    )
    from app.trading.sim_broker import SimBroker

    _add_watch()
    broker = SimBroker()
    view = SignalView(
        symbol="TEST", strategy="ma_cross", strategy_label="EMA",
        signal=SignalType.BUY, price=299.5, bar_ts=datetime.now(),
        stop_price=280.0, target_price=340.0, reason="test",
        agreement=4, agreement_total=5, position_now=1,
    )
    prop = propose_order(view, get_watch("TEST"), broker, 1,
                         require_market_open=False)

    assert reject_proposal(prop.id, "not today") is True
    assert broker.get_positions() == []
    with session_scope() as s:
        assert s.query(Order).count() == 0


def test_duplicate_position_is_blocked(temp_db, fake_bars):
    from app.models import SignalType
    from app.trading.engine import (
        approve_proposal, get_watch, propose_order, SignalView,
    )
    from app.trading.sim_broker import SimBroker

    _add_watch()
    broker = SimBroker()
    view = SignalView(
        symbol="TEST", strategy="ma_cross", strategy_label="EMA",
        signal=SignalType.BUY, price=299.5, bar_ts=datetime.now(),
        stop_price=280.0, target_price=340.0, reason="test",
        agreement=4, agreement_total=5, position_now=1,
    )
    prop = propose_order(view, get_watch("TEST"), broker, 1,
                         require_market_open=False)
    approve_proposal(prop.id, broker, require_market_open=False)

    # Already holding TEST -- a second proposal must not be created.
    second = propose_order(view, get_watch("TEST"), broker, 2,
                           require_market_open=False)
    assert second is None
