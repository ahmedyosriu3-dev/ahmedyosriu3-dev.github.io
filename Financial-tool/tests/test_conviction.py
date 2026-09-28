"""Ranking today's signals.

The ranking is the only screen in the app that puts things in an order and
calls the top one the best. That makes it the screen most able to mislead, so
the tests are about what it must refuse to do:

  * rank a symbol the selector found no edge on
  * rank a symbol you explicitly switched off
  * suggest selling something you do not own
  * read agreement or trend the same way for a sell as for a buy
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.analysis import conviction
from app.models import Mode, SignalType
from app.trading.broker import Position
from app.trading.engine import SignalView


def bars(direction: str = "up", n: int = 400) -> pd.DataFrame:
    idx = pd.date_range(end=datetime(2026, 9, 11), periods=n, freq="B")
    drift = 0.0008 if direction == "up" else -0.0008
    close = pd.Series(100.0 * np.exp(np.cumsum(np.full(n, drift))), index=idx)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close,
         "adj_close": close, "volume": 1e7},
        index=idx,
    )


class FakeWatch:
    def __init__(self, symbol: str, mode: Mode = Mode.SIGNAL_ONLY):
        self.symbol = symbol
        self.name = symbol
        self.mode = mode
        self.enabled = True


class FakeAssignment:
    def __init__(self, symbol: str, score: float, no_edge: bool = False):
        self.symbol = symbol
        self.score = score
        self.no_edge = no_edge
        self.strategy = None if no_edge else "momentum"


class FakeBroker:
    name = "fake"

    def __init__(self, positions: list[Position] | None = None):
        self._positions = positions or []

    def get_positions(self) -> list[Position]:
        return self._positions


def view(symbol: str, signal: SignalType, agreement: int = 4,
         total: int = 5, age_days: int = 0, no_edge: bool = False) -> SignalView:
    return SignalView(
        symbol=symbol, strategy="momentum", strategy_label="Momentum",
        signal=signal, price=100.0,
        bar_ts=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days),
        stop_price=90.0, target_price=None, reason="test",
        agreement=agreement, agreement_total=total, position_now=0,
        no_edge=no_edge,
    )


@pytest.fixture
def wire(monkeypatch):
    """Point the module at fake watchlist / signals / assignments / bars."""
    def _wire(watchlist, views, assignments, direction="up"):
        monkeypatch.setattr(conviction.engine, "get_watchlist",
                            lambda enabled_only=True: watchlist)
        monkeypatch.setattr(conviction.engine, "evaluate_symbol",
                            lambda w: views.get(w.symbol))
        monkeypatch.setattr(conviction, "_assignments", lambda: assignments)
        monkeypatch.setattr(conviction, "get_bars",
                            lambda *a, **k: bars(direction))
    return _wire


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------
def test_buys_are_sorted_strongest_first(wire):
    watch = [FakeWatch("AAA"), FakeWatch("BBB"), FakeWatch("CCC")]
    views = {
        # Same signal, decreasing evidence behind it.
        "AAA": view("AAA", SignalType.BUY, agreement=5, total=5),
        "BBB": view("BBB", SignalType.BUY, agreement=3, total=5),
        "CCC": view("CCC", SignalType.BUY, agreement=1, total=5, age_days=18),
    }
    assignments = {
        "AAA": FakeAssignment("AAA", 2.5),
        "BBB": FakeAssignment("BBB", 1.2),
        "CCC": FakeAssignment("CCC", 0.2),
    }
    wire(watch, views, assignments)

    buys, sells = conviction.rank(FakeBroker())

    assert [b.symbol for b in buys] == ["AAA", "BBB", "CCC"]
    assert buys[0].conviction > buys[1].conviction > buys[2].conviction
    assert sells == []


def test_conviction_is_bounded_and_labelled(wire):
    watch = [FakeWatch("AAA")]
    views = {"AAA": view("AAA", SignalType.BUY, agreement=5, total=5)}
    wire(watch, views, {"AAA": FakeAssignment("AAA", 2.5)})

    buy = conviction.rank(FakeBroker())[0][0]

    assert 0.0 <= buy.conviction <= 100.0
    assert buy.strength == "strong"
    assert len(buy.components) == 4
    assert all(0.0 <= v <= 100.0 for _label, v in buy.components)


# --------------------------------------------------------------------------
# What must never be ranked
# --------------------------------------------------------------------------
def test_no_edge_symbols_are_never_suggested(wire):
    """The selector already said it has no rule it trusts here."""
    watch = [FakeWatch("AAA")]
    views = {"AAA": view("AAA", SignalType.BUY, no_edge=True)}
    wire(watch, views, {"AAA": FakeAssignment("AAA", 0.0, no_edge=True)})

    buys, sells = conviction.rank(FakeBroker())

    assert buys == [] and sells == []


def test_symbols_switched_off_are_never_suggested(wire):
    watch = [FakeWatch("AAA", mode=Mode.OFF)]
    views = {"AAA": view("AAA", SignalType.BUY)}
    wire(watch, views, {"AAA": FakeAssignment("AAA", 2.0)})

    buys, _sells = conviction.rank(FakeBroker())

    assert buys == [], "OFF means watched and silent"


def test_hold_signals_are_not_suggestions(wire):
    watch = [FakeWatch("AAA")]
    views = {"AAA": view("AAA", SignalType.HOLD)}
    wire(watch, views, {"AAA": FakeAssignment("AAA", 2.0)})

    buys, sells = conviction.rank(FakeBroker())

    assert buys == [] and sells == []


# --------------------------------------------------------------------------
# Sells, and only from holdings
# --------------------------------------------------------------------------
def test_sell_is_ignored_when_the_position_is_not_held(wire):
    """There is no short side, so this would be an invitation, not a suggestion."""
    watch = [FakeWatch("AAA")]
    views = {"AAA": view("AAA", SignalType.SELL)}
    wire(watch, views, {"AAA": FakeAssignment("AAA", 2.0)})

    _buys, sells = conviction.rank(FakeBroker(positions=[]))

    assert sells == []


def test_sell_is_suggested_when_the_position_is_held(wire):
    watch = [FakeWatch("AAA")]
    views = {"AAA": view("AAA", SignalType.SELL)}
    wire(watch, views, {"AAA": FakeAssignment("AAA", 2.0)})
    position = Position("AAA", qty=10, avg_entry_price=90.0, current_price=100.0,
                        market_value=1000.0, unrealized_pl=100.0,
                        unrealized_plpc=0.111)

    _buys, sells = conviction.rank(FakeBroker(positions=[position]))

    assert [s.symbol for s in sells] == ["AAA"]
    assert sells[0].owned is True
    assert sells[0].qty == 10


def test_agreement_is_read_the_other_way_round_for_a_sell():
    """`agreement` counts strategies wanting to be long.

    Four of five strategies long is strong support for a buy and weak support
    for a sell. Reading it the same way for both would rank the loudest sell
    exactly where the loudest buy belongs.
    """
    buy_side = conviction._agreement_score(view("A", SignalType.BUY, 4, 5), "BUY")
    sell_side = conviction._agreement_score(view("A", SignalType.SELL, 4, 5), "SELL")

    assert buy_side == pytest.approx(80.0)
    assert sell_side == pytest.approx(20.0)


def test_trend_is_read_the_other_way_round_for_a_sell():
    up = bars("up")
    assert conviction._trend_score(up, "BUY") > 70
    assert conviction._trend_score(up, "SELL") < 30


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------
def test_a_stale_signal_scores_nothing_for_freshness():
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    assert conviction._freshness_score(now) == 100.0
    assert conviction._freshness_score(
        now - timedelta(days=conviction.STALE_AFTER_DAYS + 5)
    ) == 0.0
    mid = conviction._freshness_score(now - timedelta(days=10))
    assert 0.0 < mid < 100.0


def test_missing_bar_timestamp_scores_nothing():
    assert conviction._freshness_score(None) == 0.0
