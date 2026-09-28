"""Idea scout.

The scout's job is to be *honestly* useful, so the tests here are less about
arithmetic than about the ways a screener can quietly mislead you:

  * ranking a money-market fund as a great investment because it never falls
  * calling something a diversifier when it is the same bet you already hold
  * treating a collapsing chart as "value" because it is far off its high
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app.analysis.scout import (
    CASH_LIKE_CAP,
    Metrics,
    Regime,
    _correlation_to_book,
    _trend_points,
    compute_metrics,
    score_all,
)
from app.analysis.universe import UNIVERSES, instruments, lookup
from app.analysis.scout import WEIGHTS


def make_bars(values: list[float], volume: float = 5e6) -> pd.DataFrame:
    idx = pd.date_range(end=datetime(2026, 9, 11), periods=len(values), freq="B")
    close = pd.Series(values, index=idx, dtype="float64")
    return pd.DataFrame(
        {"open": close, "high": close * 1.005, "low": close * 0.995,
         "close": close, "adj_close": close, "volume": volume},
        index=idx,
    )


def trending(n: int = 400, start: float = 100.0, daily: float = 0.0006,
             noise: float = 0.008, seed: int = 0) -> list[float]:
    rng = np.random.default_rng(seed)
    out, price = [], start
    for _ in range(n):
        price *= 1 + daily + rng.normal(0, noise)
        out.append(price)
    return out


RISK_ON = Regime("Risk-on", "", True, True, 0.05, False, 0.15)
DEFENSIVE = Regime("Defensive", "", False, False, -0.08, True, 0.28)


# --------------------------------------------------------------------------
# The failure mode that matters most: cash dressed up as an idea
# --------------------------------------------------------------------------
def test_cash_equivalent_is_capped_and_labelled():
    """A T-bill fund maxes out trend and stability by construction.

    Left alone it outranks every real idea every week, which would make the
    whole page useless. It must be capped and described as what it is.
    """
    # A near-straight line up: exactly what SGOV/BIL look like.
    cash = make_bars([100 * (1.00016 ** i) for i in range(400)])
    inst = lookup("BIL")
    m = compute_metrics(inst, cash)

    assert m is not None
    assert m.cash_like, "a 4%/yr straight line must be detected as cash-like"

    scored = score_all([m], RISK_ON)[0]
    assert scored.score <= CASH_LIKE_CAP
    assert "not an investment" in scored.headline.lower()
    assert any("cannot grow your money" in c for c in scored.cautions)


def test_volatile_instrument_is_not_cash_like():
    m = compute_metrics(lookup("HYG"), make_bars(trending(400, noise=0.02)))
    assert m is not None
    assert not m.cash_like


# --------------------------------------------------------------------------
# Diversification is the component the user cannot eyeball themselves
# --------------------------------------------------------------------------
def test_diversify_score_falls_as_correlation_rises():
    def scored_with(corr: float) -> float:
        m = compute_metrics(lookup("GLD"), make_bars(trending(400, seed=3)))
        m.corr_to_book = corr
        return score_all([m], RISK_ON)[0].diversify_score

    assert scored_with(-0.5) > scored_with(0.0) > scored_with(0.9)
    assert scored_with(1.0) == pytest.approx(0.0, abs=1e-6)


def test_high_correlation_earns_a_caution():
    m = compute_metrics(lookup("XLK"), make_bars(trending(400, seed=5)))
    m.corr_to_book = 0.92
    scored = score_all([m], RISK_ON)[0]
    assert any("lockstep" in c for c in scored.cautions)


def test_correlation_to_an_empty_book_is_neutral():
    rets = pd.Series(np.random.default_rng(1).normal(0, 0.01, 120))
    assert _correlation_to_book(rets, pd.DataFrame()) == 0.0


def test_correlation_matches_numpy_on_a_known_pair():
    idx = pd.date_range("2025-01-01", periods=120, freq="B")
    a = pd.Series(np.random.default_rng(2).normal(0, 0.01, 120), index=idx)
    book = pd.DataFrame({"AAA": a})
    # Perfectly correlated with itself.
    assert _correlation_to_book(a, book) == pytest.approx(1.0, abs=1e-9)
    # Perfectly anti-correlated.
    assert _correlation_to_book(-a, book) == pytest.approx(-1.0, abs=1e-9)


# --------------------------------------------------------------------------
# "Cheap and falling" must not read as value
# --------------------------------------------------------------------------
def test_falling_knife_scores_no_value():
    """Something 60% off its high with a broken trend is not a pullback."""
    peak = trending(200, seed=7)
    crash = [peak[-1] * (0.995 ** i) for i in range(200)]
    m = compute_metrics(lookup("FCX"), make_bars(peak + crash))
    scored = score_all([m], RISK_ON)[0]

    assert not scored.above_200
    assert scored.value_score == 0.0
    assert any("below its 200-day" in c.lower() for c in scored.cautions)


def test_pullback_in_an_uptrend_scores_value():
    up = trending(380, daily=0.0025, noise=0.004, seed=11)
    dip = [up[-1] * (1 - 0.09 * i / 20) for i in range(20)]   # ~9% pullback
    m = compute_metrics(lookup("MSFT"), make_bars(up + dip))
    scored = score_all([m], RISK_ON)[0]

    assert scored.above_200, "a 9% dip should not break a long uptrend"
    assert scored.value_score > 0


# --------------------------------------------------------------------------
# Trend is a checklist, so it should be exactly reproducible
# --------------------------------------------------------------------------
def test_trend_points_are_the_sum_of_the_checklist():
    m = Metrics(inst=lookup("AAPL"))
    assert _trend_points(m) == 0
    m.above_200 = True
    assert _trend_points(m) == 40
    m.golden = True
    assert _trend_points(m) == 65
    m.ma200_rising = True
    assert _trend_points(m) == 85
    m.above_50 = True
    assert _trend_points(m) == 100


# --------------------------------------------------------------------------
# Regime tilt
# --------------------------------------------------------------------------
def test_defensive_regime_favours_bonds_over_equities():
    """Same chart, two asset classes: in a defensive tape the bond wins."""
    series = make_bars(trending(400, seed=13))
    equity = compute_metrics(lookup("AAPL"), series)
    bond = compute_metrics(lookup("LQD"), series)
    for m in (equity, bond):
        m.corr_to_book = 0.3

    defensive = {m.inst.symbol: m.score for m in score_all([equity, bond], DEFENSIVE)}

    equity2 = compute_metrics(lookup("AAPL"), series)
    bond2 = compute_metrics(lookup("LQD"), series)
    for m in (equity2, bond2):
        m.corr_to_book = 0.3
    risk_on = {m.inst.symbol: m.score for m in score_all([equity2, bond2], RISK_ON)}

    assert defensive["LQD"] > risk_on["LQD"]
    assert defensive["AAPL"] <= risk_on["AAPL"]


def test_equities_are_cautioned_in_a_defensive_regime():
    m = compute_metrics(lookup("AAPL"), make_bars(trending(400, seed=17)))
    scored = score_all([m], DEFENSIVE)[0]
    assert any("below its 200-day" in c for c in scored.cautions)


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------
def test_short_history_is_rejected():
    """Under a year of bars, none of these measures mean anything."""
    assert compute_metrics(lookup("AAPL"), make_bars(trending(100))) is None


def test_scores_stay_in_range():
    pool = [
        compute_metrics(lookup(s), make_bars(trending(400, seed=i)))
        for i, s in enumerate(["AAPL", "TLT", "GLD", "XLE", "HYG"])
    ]
    for m in score_all(pool, RISK_ON):
        for value in (m.score, m.trend_score, m.momentum_score,
                      m.quality_score, m.diversify_score, m.value_score):
            assert 0.0 <= value <= 100.0


# --------------------------------------------------------------------------
# The universe itself
# --------------------------------------------------------------------------
def test_universe_has_no_duplicate_symbols():
    all_syms = [i.symbol for g in UNIVERSES.values() for i in g]
    assert len(all_syms) == len(set(all_syms))


def test_every_asset_class_has_weights():
    for inst in instruments():
        assert inst.asset_class in WEIGHTS, f"{inst.symbol} has no weights"


def test_weights_sum_to_one():
    for cls, w in WEIGHTS.items():
        assert sum(w) == pytest.approx(1.0), f"{cls} weights do not sum to 1"


def test_all_four_universes_are_populated():
    for key in ("stocks", "sector_etf", "bonds", "commodities"):
        assert len(UNIVERSES[key]) >= 15
