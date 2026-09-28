"""Which of today's signals deserve your attention first.

The overview lists every watched symbol and what its strategy currently says.
That is the right default -- it hides nothing -- but it answers "what is the
state of everything?" rather than "what, if anything, should I do?". With
thirty symbols on the list those are different questions, and the second one
is the one you actually sat down to ask.

This module ranks the live signals by how much evidence sits behind each one:

  edge        The out-of-sample score the selector gave this symbol's assigned
              strategy. A signal from a rule that never demonstrated an edge is
              not a suggestion, it is a rule firing.
  agreement   How many of the other strategies independently want the same
              side right now. A lone outlier and a unanimous read are different
              propositions.
  trend       Whether the longer-term trend is behind the signal or against it.
  freshness   How recently it fired. A buy signal from six weeks ago is a
              historical fact, not a suggestion.

Sell suggestions are only ever shown for symbols you **actually hold**. A sell
signal on something you do not own is not an instruction to short it -- this
app has no short side -- so listing it would be noise at best and an invitation
to do something dangerous at worst.

The number is a ranking of the app's own evidence, not a forecast, and the
components are always shown beside it so a high score can be argued with.

Nothing here places, sizes or proposes an order. Ranking is not permission:
an order still comes from the symbol's mode and still waits for your approval.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select

from app.analysis import indicators as ind
from app.data.store import get_bars
from app.db import session_scope
from app.models import Mode, SignalType, StrategyAssignment
from app.trading import engine
from app.trading.broker import Broker, Position

log = logging.getLogger(__name__)

# Weights for the four components. Edge and agreement carry most of it: one is
# "has this rule ever worked on this symbol", the other is "does anything else
# agree today", and a suggestion that fails both is not worth surfacing however
# good the chart looks.
WEIGHTS = {"edge": 0.35, "agreement": 0.35, "trend": 0.20, "freshness": 0.10}

# The selector's score is roughly a drawdown-penalised Sharpe. 2.5 is an
# excellent walk-forward result, so it is treated as the top of the scale
# rather than pretending the number is unbounded.
EDGE_FULL_MARKS = 2.5

# A signal older than this has stopped being news.
STALE_AFTER_DAYS = 21


@dataclass
class Suggestion:
    symbol: str
    name: str
    side: str                      # BUY | SELL
    conviction: float = 0.0

    strategy_label: str = ""
    price: float = 0.0
    bar_ts: datetime | None = None
    reason: str = ""
    agreement: int = 0
    agreement_total: int = 0
    mode: str = ""

    edge_score: float = 0.0
    agreement_score: float = 0.0
    trend_score: float = 0.0
    freshness_score: float = 0.0
    selector_score: float = 0.0

    owned: bool = False
    qty: float = 0.0
    market_value: float = 0.0
    unrealized_plpc: float = 0.0

    reasons: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)

    @property
    def components(self) -> list[tuple[str, float]]:
        return [
            ("Demonstrated edge", self.edge_score),
            ("Strategies agreeing", self.agreement_score),
            ("Trend behind it", self.trend_score),
            ("Freshness", self.freshness_score),
        ]

    @property
    def strength(self) -> str:
        if self.conviction >= 70:
            return "strong"
        if self.conviction >= 45:
            return "moderate"
        return "weak"


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------
def _edge_score(symbol: str, assignments: dict[str, StrategyAssignment]) -> tuple[float, float]:
    """How well the assigned strategy did out-of-sample, as 0-100."""
    row = assignments.get(symbol)
    if row is None or row.no_edge or row.strategy is None:
        return 0.0, 0.0
    raw = float(row.score)
    return float(max(0.0, min(raw / EDGE_FULL_MARKS, 1.0)) * 100.0), raw


def _agreement_score(view: engine.SignalView, side: str) -> float:
    """Share of strategies independently on the same side right now."""
    if not view.agreement_total:
        return 50.0        # nothing to compare against; do not reward or punish
    share = view.agreement / view.agreement_total
    # `agreement` counts strategies that want to be long, so a sell is
    # supported by the ones that do not.
    return float((share if side == "BUY" else 1.0 - share) * 100.0)


def _trend_score(bars: pd.DataFrame, side: str) -> float:
    """Is the longer-term trend behind this signal or against it?"""
    if bars is None or len(bars) < 210:
        return 50.0

    close = bars["adj_close"].astype(float)
    last = float(close.iloc[-1])
    sma200 = ind.sma(close, 200)
    sma50 = ind.sma(close, 50)
    if pd.isna(sma200.iloc[-1]) or pd.isna(sma50.iloc[-1]):
        return 50.0

    above_200 = last > float(sma200.iloc[-1])
    above_50 = last > float(sma50.iloc[-1])
    golden = float(sma50.iloc[-1]) > float(sma200.iloc[-1])

    points = (50.0 * above_200) + (25.0 * above_50) + (25.0 * golden)
    # A sell is supported by exactly the opposite picture.
    return points if side == "BUY" else 100.0 - points


def _freshness_score(bar_ts: datetime | None) -> float:
    if bar_ts is None:
        return 0.0
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - bar_ts).days
    if age <= 1:
        return 100.0
    if age >= STALE_AFTER_DAYS:
        return 0.0
    return float((1.0 - age / STALE_AFTER_DAYS) * 100.0)


# --------------------------------------------------------------------------
# Explanation
# --------------------------------------------------------------------------
def _explain(s: Suggestion) -> None:
    reasons: list[str] = []
    cautions: list[str] = []

    if s.edge_score >= 70:
        reasons.append(
            f"{s.strategy_label} scored {s.selector_score:.2f} out-of-sample on "
            f"this symbol — a strong walk-forward result"
        )
    elif s.edge_score >= 35:
        reasons.append(
            f"{s.strategy_label} cleared the walk-forward bar here "
            f"({s.selector_score:.2f})"
        )

    if s.agreement_total:
        if s.agreement_score >= 80:
            reasons.append(
                f"{s.agreement}/{s.agreement_total} strategies independently "
                f"want the same side"
            )
        elif s.agreement_score <= 35:
            cautions.append(
                f"only {s.agreement}/{s.agreement_total} other strategies agree "
                f"— this is close to a lone signal"
            )

    if s.trend_score >= 75:
        reasons.append("The longer-term trend is behind it")
    elif s.trend_score <= 25:
        cautions.append(
            "The longer-term trend points the other way"
            if s.side == "BUY"
            else "The longer-term trend is still up — this sells into strength"
        )

    if s.freshness_score <= 30:
        cautions.append("The signal is not fresh; the move may already be done")

    if s.side == "BUY" and s.owned:
        reasons.append(
            f"You already hold {s.qty:g} shares — this would add to a position"
        )
    if s.side == "SELL" and s.owned:
        direction = "up" if s.unrealized_plpc >= 0 else "down"
        reasons.append(
            f"You hold {s.qty:g} shares, {direction} "
            f"{abs(s.unrealized_plpc) * 100:.1f}% on the position"
        )

    if s.mode == Mode.SIGNAL_ONLY.value:
        cautions.append(
            "This symbol is signal-only — nothing will be proposed automatically"
        )

    s.reasons = reasons[:3]
    s.cautions = cautions[:3]


# --------------------------------------------------------------------------
# The ranking
# --------------------------------------------------------------------------
def _assignments() -> dict[str, StrategyAssignment]:
    with session_scope() as s:
        rows = s.execute(
            select(StrategyAssignment).where(StrategyAssignment.is_current.is_(True))
        ).scalars()
        return {r.symbol: r for r in rows}


def _positions(broker: Broker) -> dict[str, Position]:
    try:
        return {p.symbol: p for p in broker.get_positions()}
    except Exception as exc:
        log.warning("could not read positions: %s", exc)
        return {}


def rank(broker: Broker) -> tuple[list[Suggestion], list[Suggestion]]:
    """Today's signals as two ranked lists: what to buy, what to sell.

    Returns `(buys, sells)`, each sorted strongest first. Sells cover only
    symbols currently held.
    """
    assignments = _assignments()
    held = _positions(broker)
    end = datetime.now(timezone.utc).replace(tzinfo=None)

    buys: list[Suggestion] = []
    sells: list[Suggestion] = []

    for watch in engine.get_watchlist(enabled_only=True):
        if watch.mode == Mode.OFF:
            # Explicitly silenced. Ranking it would defeat the point of the mode.
            continue
        try:
            view = engine.evaluate_symbol(watch)
        except Exception as exc:
            log.warning("conviction: evaluation failed for %s: %s", watch.symbol, exc)
            continue
        if view is None or view.no_edge:
            # No demonstrated edge means the app deliberately has nothing to
            # say about this symbol, and a ranked list is not the place to
            # start saying something anyway.
            continue
        if view.signal not in (SignalType.BUY, SignalType.SELL):
            continue

        side = view.signal.value
        position = held.get(watch.symbol)
        if side == "SELL" and position is None:
            # Nothing to sell. This app has no short side.
            continue

        bars = get_bars(watch.symbol, end - timedelta(days=500), end)
        edge, raw_score = _edge_score(watch.symbol, assignments)

        s = Suggestion(
            symbol=watch.symbol,
            name=watch.name or watch.symbol,
            side=side,
            strategy_label=view.strategy_label,
            price=view.price,
            bar_ts=view.bar_ts,
            reason=view.reason,
            agreement=view.agreement,
            agreement_total=view.agreement_total,
            mode=watch.mode.value,
            edge_score=edge,
            selector_score=raw_score,
            agreement_score=_agreement_score(view, side),
            trend_score=_trend_score(bars, side),
            freshness_score=_freshness_score(view.bar_ts),
            owned=position is not None,
            qty=position.qty if position else 0.0,
            market_value=position.market_value if position else 0.0,
            unrealized_plpc=position.unrealized_plpc if position else 0.0,
        )
        s.conviction = round(
            WEIGHTS["edge"] * s.edge_score
            + WEIGHTS["agreement"] * s.agreement_score
            + WEIGHTS["trend"] * s.trend_score
            + WEIGHTS["freshness"] * s.freshness_score,
            1,
        )
        _explain(s)

        (buys if side == "BUY" else sells).append(s)

    buys.sort(key=lambda x: x.conviction, reverse=True)
    sells.sort(key=lambda x: x.conviction, reverse=True)
    return buys, sells
