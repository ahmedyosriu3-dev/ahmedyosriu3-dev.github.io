"""Idea discovery: what else might deserve your money?

The scout screens a curated universe (`app/analysis/universe.py`) and ranks it
on five components, each scored 0-100 so the dashboard can show you *why*
something surfaced rather than handing you a number to trust:

  trend      Is it in an uptrend? Position relative to its own moving averages.
  momentum   Risk-adjusted return over 1/3/6/12 months, ranked against peers.
  quality    Low volatility, shallow drawdowns, real liquidity.
  diversify  How *little* it moves with what you already own. This is the
             component most screeners ignore and the one most likely to
             actually improve a portfolio you already hold.
  value      How far it has pulled back from its 52-week high -- but only
             counted when the long-term trend is still intact, because
             "cheap and falling" is not value.

Weights differ by asset class: a bond ETF earns its place by being
uncorrelated and steady, not by outrunning the S&P.

Nothing here places, sizes or proposes an order. The scout's entire output is
a list of things to go and look at, and the honest answer some weeks is an
empty list.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import delete, select

from app.analysis import indicators as ind
from app.analysis.universe import (
    BENCHMARK,
    DEFENSIVE,
    Instrument,
    instruments,
    lookup,
)
from app.config import settings
from app.data.store import get_bars
from app.db import session_scope
from app.models import ScoutIdea, WatchItem

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 500          # ~2 years of trading history
MIN_BARS = 260               # need a year before any of this means anything
CORR_WINDOW = 126            # half a year of daily returns for correlations

# Below this annualised volatility an instrument is a cash equivalent (T-bill
# funds, ultra-short Treasuries). They pin every trend and stability measure
# at 100 by construction -- an instrument that never falls is always "in an
# uptrend with shallow drawdowns" -- which would let them crowd out every real
# idea. They are useful for parking cash and are scored as such, not as
# investments competing with equities.
CASH_LIKE_VOL = 0.025
CASH_LIKE_CAP = 45.0

WEIGHTS = {
    # asset_class -> (trend, momentum, quality, diversify, value)
    "equity":    (0.28, 0.30, 0.14, 0.18, 0.10),
    "etf":       (0.28, 0.28, 0.16, 0.18, 0.10),
    "bond":      (0.20, 0.20, 0.25, 0.30, 0.05),
    "commodity": (0.25, 0.25, 0.10, 0.35, 0.05),
}


# --------------------------------------------------------------------------
# Raw per-symbol metrics
# --------------------------------------------------------------------------
@dataclass
class Metrics:
    inst: Instrument
    price: float = 0.0
    ret_1m: float = 0.0
    ret_3m: float = 0.0
    ret_6m: float = 0.0
    ret_12m: float = 0.0
    volatility: float = 0.0
    max_drawdown: float = 0.0
    dollar_volume: float = 0.0
    above_200: bool = False
    above_50: bool = False
    golden: bool = False            # 50-day above 200-day
    ma200_rising: bool = False
    dist_from_high: float = 0.0     # negative = below the 52-week high
    corr_to_book: float = 0.0
    cash_like: bool = False
    standout: dict = field(default_factory=dict)
    returns: pd.Series = field(default_factory=pd.Series)
    spark: list[float] = field(default_factory=list)

    # Filled in during ranking.
    trend_score: float = 0.0
    momentum_score: float = 0.0
    quality_score: float = 0.0
    diversify_score: float = 0.0
    value_score: float = 0.0
    score: float = 0.0
    headline: str = ""
    reasons: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)


def _pct_change_over(close: pd.Series, n: int) -> float:
    if len(close) <= n:
        return 0.0
    prev = float(close.iloc[-n - 1])
    return (float(close.iloc[-1]) / prev - 1.0) if prev else 0.0


def _max_drawdown(close: pd.Series) -> float:
    if close.empty:
        return 0.0
    running = close.cummax()
    return float((close / running - 1.0).min())


def compute_metrics(inst: Instrument, bars: pd.DataFrame) -> Metrics | None:
    """Everything measurable about one instrument from its own bars."""
    if bars is None or len(bars) < MIN_BARS:
        return None

    close = bars["adj_close"].astype(float)
    raw_close = bars["close"].astype(float)
    if close.isna().all() or float(raw_close.iloc[-1]) <= 0:
        return None

    rets = close.pct_change().dropna()
    recent = rets.tail(126)
    vol = float(recent.std() * np.sqrt(252)) if len(recent) > 20 else 0.0

    sma200 = ind.sma(close, 200)
    sma50 = ind.sma(close, 50)
    last = float(close.iloc[-1])
    year = close.tail(252)

    dollar_vol = float(
        (raw_close * bars["volume"].astype(float)).tail(63).median()
    )

    ma200_rising = False
    if len(sma200.dropna()) > 21:
        s = sma200.dropna()
        ma200_rising = float(s.iloc[-1]) > float(s.iloc[-21])

    max_dd_year = _max_drawdown(year)
    return Metrics(
        inst=inst,
        cash_like=(vol < CASH_LIKE_VOL and max_dd_year > -0.02),
        price=float(raw_close.iloc[-1]),
        ret_1m=_pct_change_over(close, 21),
        ret_3m=_pct_change_over(close, 63),
        ret_6m=_pct_change_over(close, 126),
        ret_12m=_pct_change_over(close, 252),
        volatility=vol,
        max_drawdown=max_dd_year,
        dollar_volume=dollar_vol,
        above_200=bool(pd.notna(sma200.iloc[-1]) and last > float(sma200.iloc[-1])),
        above_50=bool(pd.notna(sma50.iloc[-1]) and last > float(sma50.iloc[-1])),
        golden=bool(
            pd.notna(sma50.iloc[-1])
            and pd.notna(sma200.iloc[-1])
            and float(sma50.iloc[-1]) > float(sma200.iloc[-1])
        ),
        ma200_rising=ma200_rising,
        dist_from_high=(last / float(year.max()) - 1.0) if float(year.max()) else 0.0,
        returns=rets.tail(CORR_WINDOW),
        spark=[round(float(v), 4) for v in close.tail(180).tolist()],
    )


# --------------------------------------------------------------------------
# Market regime
# --------------------------------------------------------------------------
@dataclass
class Regime:
    label: str
    detail: str
    risk_on: bool
    spy_above_200: bool
    spy_ret_3m: float
    bonds_beating_stocks: bool
    volatility: float


def market_regime() -> Regime:
    """A one-line read on what kind of market this is.

    Not a forecast -- a description of where things currently stand, used to
    tilt the scout's weights and to set expectations on the dashboard.
    """
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    spy = get_bars(BENCHMARK, end - timedelta(days=LOOKBACK_DAYS), end)
    ief = get_bars(DEFENSIVE, end - timedelta(days=LOOKBACK_DAYS), end)

    if spy.empty or len(spy) < 210:
        return Regime("Unknown", "Not enough benchmark history yet.",
                      True, True, 0.0, False, 0.0)

    close = spy["adj_close"].astype(float)
    sma200 = ind.sma(close, 200)
    above = bool(pd.notna(sma200.iloc[-1]) and float(close.iloc[-1]) > float(sma200.iloc[-1]))
    ret3 = _pct_change_over(close, 63)
    vol = float(close.pct_change().tail(21).std() * np.sqrt(252))

    bonds_win = False
    if not ief.empty and len(ief) > 70:
        bonds_win = _pct_change_over(ief["adj_close"].astype(float), 63) > ret3

    if above and not bonds_win and vol < 0.22:
        label = "Risk-on"
        detail = (
            f"{BENCHMARK} is above its 200-day average and up "
            f"{ret3 * 100:.1f}% over three months, with calm volatility. "
            f"Trend-following strategies have the wind behind them."
        )
    elif above and (bonds_win or vol >= 0.22):
        label = "Choppy"
        detail = (
            f"{BENCHMARK} is still above its 200-day average, but "
            + ("bonds are outrunning stocks" if bonds_win else
               f"volatility is elevated ({vol * 100:.0f}% annualised)")
            + ". Trends break more often here; expect more false starts."
        )
    else:
        label = "Defensive"
        detail = (
            f"{BENCHMARK} is below its 200-day average ({ret3 * 100:+.1f}% "
            f"over three months). Most long strategies do badly in this "
            f"regime. Smaller size, or none, is a position."
        )

    return Regime(label, detail, above and not bonds_win, above, ret3, bonds_win, vol)


# --------------------------------------------------------------------------
# The book you already have -- used for the diversification score
# --------------------------------------------------------------------------
def _book_returns() -> tuple[pd.DataFrame, list[str]]:
    """Daily returns of everything you already hold or watch."""
    with session_scope() as s:
        symbols = [w.symbol for w in s.execute(select(WatchItem)).scalars()]

    try:
        from app.trading.factory import get_broker

        symbols += [p.symbol for p in get_broker().get_positions()]
    except Exception:
        pass

    symbols = sorted(set(symbols))
    if not symbols:
        return pd.DataFrame(), []

    end = datetime.now(timezone.utc).replace(tzinfo=None)
    cols = {}
    for sym in symbols:
        bars = get_bars(sym, end - timedelta(days=LOOKBACK_DAYS), end)
        if len(bars) > 60:
            cols[sym] = bars["adj_close"].astype(float).pct_change().tail(CORR_WINDOW)
    if not cols:
        return pd.DataFrame(), []
    return pd.DataFrame(cols).dropna(how="all"), list(cols)


def _correlation_to_book(rets: pd.Series, book: pd.DataFrame) -> float:
    """Average correlation against the existing book.

    Falls back to the benchmark when the book is empty -- 'uncorrelated with
    the market' is still the useful question on day one.
    """
    if rets.empty or book.empty:
        return 0.0
    joined = book.join(rets.rename("__cand__"), how="inner")
    if len(joined) < 40:
        return 0.0
    corrs = []
    for col in joined.columns:
        if col == "__cand__":
            continue
        c = joined[col].corr(joined["__cand__"])
        if pd.notna(c):
            corrs.append(float(c))
    return float(np.mean(corrs)) if corrs else 0.0


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def _rank_pct(values: list[float]) -> list[float]:
    """Percentile-rank a list into 0-100. Ties share a rank."""
    if not values:
        return []
    s = pd.Series(values, dtype="float64")
    if s.nunique() <= 1:
        return [50.0] * len(values)
    return (s.rank(pct=True) * 100.0).tolist()


def _trend_points(m: Metrics) -> float:
    """Trend as an explainable checklist rather than an opaque indicator."""
    pts = 0.0
    if m.above_200:
        pts += 40
    if m.golden:
        pts += 25
    if m.ma200_rising:
        pts += 20
    if m.above_50:
        pts += 15
    return pts


def score_all(metrics: list[Metrics], regime: Regime) -> list[Metrics]:
    """Turn raw metrics into comparable 0-100 components and a final score."""
    if not metrics:
        return []

    # Momentum: risk-adjusted blend, ranked against the rest of the pool.
    mom_raw = []
    for m in metrics:
        blend = (0.15 * m.ret_1m + 0.25 * m.ret_3m
                 + 0.30 * m.ret_6m + 0.30 * m.ret_12m)
        mom_raw.append(blend / m.volatility if m.volatility > 0.01 else blend)
    mom_ranked = _rank_pct(mom_raw)

    # Quality: steady and liquid. All three ranked, then blended.
    vol_ranked = _rank_pct([-m.volatility for m in metrics])
    dd_ranked = _rank_pct([m.max_drawdown for m in metrics])   # less negative is better
    liq_ranked = _rank_pct([np.log10(max(m.dollar_volume, 1.0)) for m in metrics])

    for i, m in enumerate(metrics):
        m.trend_score = _trend_points(m)
        m.momentum_score = mom_ranked[i]
        m.quality_score = (
            0.40 * vol_ranked[i] + 0.40 * dd_ranked[i] + 0.20 * liq_ranked[i]
        )
        # Correlation of 1.0 with what you own adds nothing; -1.0 is a hedge.
        # Genuinely uncorrelated (0.0) should read as clearly good, not average.
        m.diversify_score = float(np.clip((1.0 - m.corr_to_book) * 62.5, 0, 100))

        # A pullback only counts as value while the long trend is intact.
        depth = abs(m.dist_from_high)
        if m.above_200 and 0.05 <= depth <= 0.30:
            m.value_score = float(np.clip(depth / 0.30 * 100.0, 0, 100))
        elif depth < 0.05:
            m.value_score = 15.0        # at the highs: nothing on sale
        else:
            m.value_score = 0.0

        w = WEIGHTS.get(m.inst.asset_class, WEIGHTS["equity"])
        base = (
            w[0] * m.trend_score + w[1] * m.momentum_score + w[2] * m.quality_score
            + w[3] * m.diversify_score + w[4] * m.value_score
        )

        # Regime tilt. In a defensive tape, reward things that hold up and
        # stop rewarding high-volatility equities for going up fastest.
        if not regime.risk_on:
            if m.inst.asset_class in ("bond", "commodity"):
                base += 8.0
            elif m.volatility > 0.30:
                base -= 6.0
        elif regime.risk_on and m.inst.asset_class == "bond":
            base -= 3.0

        if m.cash_like:
            # Capped rather than excluded: parking cash at 4% is a real and
            # sometimes correct choice, and hiding the option would be its own
            # kind of dishonesty. It just is not an *idea*.
            base = min(base, CASH_LIKE_CAP)

        m.score = float(np.clip(base, 0, 100))

    # Which component makes each instrument *unusual* within this run.
    #
    # The raw component values cannot answer that. In a bull market most of
    # the universe maxes out the trend checklist, so trend led every headline
    # while carrying almost no information. Ranking each component within the
    # pool fixes it: scoring 100 on trend alongside 200 other names is
    # unremarkable, while a negative correlation when everything else is
    # positive is the thing worth saying out loud.
    standout = {
        "trend": _rank_pct([m.trend_score for m in metrics]),
        "momentum": _rank_pct([m.momentum_score for m in metrics]),
        "quality": _rank_pct([m.quality_score for m in metrics]),
        "diversify": _rank_pct([m.diversify_score for m in metrics]),
        "value": _rank_pct([m.value_score for m in metrics]),
    }
    for i, m in enumerate(metrics):
        m.standout = {k: v[i] for k, v in standout.items()}
        _explain(m, regime)

    metrics.sort(key=lambda x: x.score, reverse=True)
    return metrics


def _explain(m: Metrics, regime: Regime) -> None:
    """Write the plain-English case, and the case against."""
    reasons, cautions = [], []

    if m.cash_like:
        # Do not dress a money-market fund up as a trade.
        m.headline = (
            "Cash parking, not an investment. Near-zero price risk, pays "
            "roughly the short-term rate, and sells same-day."
        )
        m.reasons = [
            f"Effectively no price risk ({m.volatility * 100:.1f}% volatility)",
            f"Up {m.ret_12m * 100:.1f}% over a year, in an almost straight line",
            "Somewhere to hold cash between ideas",
        ]
        m.cautions = [
            "It cannot grow your money meaningfully — it roughly keeps pace "
            "with inflation at best",
            "The smooth chart is the nature of the instrument, not evidence "
            "of a good trade",
        ]
        return

    if m.trend_score >= 80:
        reasons.append("In a clean uptrend on every timeframe")
    elif m.trend_score >= 55:
        reasons.append("Trend is intact but not unanimous")
    elif m.above_200:
        reasons.append("Above its 200-day average")

    if m.momentum_score >= 85:
        reasons.append(f"Top-decile risk-adjusted momentum ({m.ret_6m * 100:+.0f}% in 6m)")
    elif m.momentum_score >= 65:
        reasons.append(f"Strong relative momentum ({m.ret_6m * 100:+.0f}% in 6m)")

    if m.diversify_score >= 75:
        reasons.append(
            f"Barely moves with what you already own (correlation {m.corr_to_book:.2f})"
        )
    elif m.diversify_score >= 55:
        reasons.append(f"Only loosely correlated with your book ({m.corr_to_book:.2f})")

    if m.quality_score >= 75:
        reasons.append(
            f"Steady: {m.volatility * 100:.0f}% volatility, worst drawdown "
            f"{m.max_drawdown * 100:.0f}%"
        )

    if m.value_score >= 60:
        reasons.append(
            f"{abs(m.dist_from_high) * 100:.0f}% off its 52-week high while still "
            f"above the 200-day"
        )

    # The case against.
    if m.volatility > 0.35:
        cautions.append(f"Volatile: {m.volatility * 100:.0f}% annualised")
    if m.max_drawdown < -0.30:
        cautions.append(f"Fell {abs(m.max_drawdown) * 100:.0f}% at its worst this year")
    if m.corr_to_book > 0.75:
        cautions.append(
            f"Moves almost in lockstep with your book ({m.corr_to_book:.2f}) — "
            f"buying it mostly doubles a bet you already have"
        )
    if not m.above_200:
        cautions.append("Below its 200-day average — the long trend is down")
    if m.dollar_volume < settings.scout_min_dollar_volume * 2:
        cautions.append("Thinner trading volume; expect wider spreads")
    if m.ret_12m > 1.0:
        cautions.append(
            f"Already up {m.ret_12m * 100:.0f}% in a year — you are late to this move"
        )
    if regime.label == "Defensive" and m.inst.asset_class in ("equity", "etf"):
        cautions.append("Equity exposure while the broad market is below its 200-day")

    m.reasons = reasons[:4]
    m.cautions = cautions[:3]

    m.headline = _headline(m)


# Two clauses, drawn from the two components that actually earned the
# ranking. One clause per idea made every card read "trend is firmly up",
# because trend is an absolute checklist while the others are percentile
# ranks -- so trend led almost every time and the headline said nothing.
_LEAD = {
    "trend":     "Trend is up on every timeframe",
    "momentum":  "Among the strongest risk-adjusted performers screened",
    "quality":   "Steady: low volatility and shallow drawdowns",
    "diversify": "Goes its own way relative to what you already hold",
    "value":     "Pulled back inside an intact uptrend",
}
_FOLLOW = {
    "trend":     "and the long-term trend is intact",
    "momentum":  "and it has been outrunning its peers",
    "quality":   "without the volatility that usually comes with it",
    "diversify": "and it barely moves with your current book",
    "value":     "and it is some way off its highs rather than at them",
}


def _headline(m: Metrics) -> str:
    raw = {
        "trend": m.trend_score,
        "momentum": m.momentum_score,
        "quality": m.quality_score,
        "diversify": m.diversify_score,
        "value": m.value_score,
    }
    # Fall back to raw values when scored outside a pool (a single symbol has
    # no peers to be unusual against).
    parts = m.standout or raw
    ranked = sorted(parts, key=lambda k: parts[k], reverse=True)
    lead, second = ranked[0], ranked[1]

    if raw[lead] < 45:
        return (
            "Nothing here stands out — it cleared the screen without excelling "
            "at anything in particular."
        )

    head = _LEAD[lead]
    if parts[second] >= 65 and raw[second] >= 50:
        return f"{head}, {_FOLLOW[second]}."
    return f"{head}."


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------
def run_scout(
    universe_keys: list[str] | None = None,
    max_ideas: int | None = None,
    exclude_watched: bool = True,
) -> tuple[str, list[ScoutIdea]]:
    """Screen the universe and persist the top ideas. Returns (run_id, ideas)."""
    universe_keys = universe_keys or settings.scout_universe_keys
    max_ideas = max_ideas or settings.scout_max_ideas
    run_id = uuid.uuid4().hex[:12]

    regime = market_regime()
    book, book_symbols = _book_returns()
    watched = set(book_symbols) if exclude_watched else set()

    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=LOOKBACK_DAYS)

    pool: list[Metrics] = []
    skipped = 0
    for inst in instruments(universe_keys):
        if inst.symbol in watched:
            continue
        try:
            bars = get_bars(inst.symbol, start, end)
        except Exception as exc:
            log.debug("scout: no bars for %s (%s)", inst.symbol, exc)
            continue

        m = compute_metrics(inst, bars)
        if m is None:
            skipped += 1
            continue
        if m.dollar_volume < settings.scout_min_dollar_volume:
            skipped += 1
            continue
        m.corr_to_book = _correlation_to_book(m.returns, book)
        pool.append(m)

    log.info("scout screened %d instruments (%d skipped)", len(pool), skipped)
    ranked = score_all(pool, regime)[:max_ideas]

    rows: list[ScoutIdea] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        # Keep only the most recent run; the history is noise, not evidence.
        s.execute(delete(ScoutIdea))
        for m in ranked:
            row = ScoutIdea(
                run_id=run_id, symbol=m.inst.symbol, name=m.inst.name,
                universe=m.inst.universe, asset_class=m.inst.asset_class,
                score=round(m.score, 2), price=round(m.price, 4),
                trend_score=round(m.trend_score, 1),
                momentum_score=round(m.momentum_score, 1),
                quality_score=round(m.quality_score, 1),
                diversify_score=round(m.diversify_score, 1),
                value_score=round(m.value_score, 1),
                ret_1m=m.ret_1m, ret_3m=m.ret_3m, ret_6m=m.ret_6m,
                ret_12m=m.ret_12m, volatility=m.volatility,
                max_drawdown=m.max_drawdown, dollar_volume=m.dollar_volume,
                corr_to_book=m.corr_to_book, headline=m.headline,
                reasons_json=json.dumps(m.reasons),
                cautions_json=json.dumps(m.cautions),
                spark_json=json.dumps(m.spark),
                created_at=now,
            )
            s.add(row)
            rows.append(row)

    from app.trading.risk import audit

    audit(
        "SCOUT_RUN",
        message=f"{len(rows)} ideas from {len(pool)} screened ({regime.label})",
        regime=regime.label, run_id=run_id,
    )
    return run_id, rows


def latest_ideas() -> list[ScoutIdea]:
    with session_scope() as s:
        return list(
            s.execute(
                select(ScoutIdea).order_by(ScoutIdea.score.desc())
            ).scalars()
        )


def idea_detail(symbol: str) -> dict | None:
    """Everything known about one idea, for the expanded card."""
    with session_scope() as s:
        row = s.execute(
            select(ScoutIdea).where(ScoutIdea.symbol == symbol.upper())
        ).scalar_one_or_none()
    if row is None:
        return None
    inst = lookup(row.symbol)
    return {
        "row": row,
        "sector": inst.sector if inst else "",
        "reasons": json.loads(row.reasons_json or "[]"),
        "cautions": json.loads(row.cautions_json or "[]"),
        "spark": json.loads(row.spark_json or "[]"),
    }
