"""Indicator correctness against hand-computed values."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis import indicators as ind


@pytest.fixture
def series() -> pd.Series:
    return pd.Series([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10])


def test_sma_matches_manual_mean(series):
    out = ind.sma(series, 3)
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)     # (1+2+3)/3
    assert out.iloc[9] == pytest.approx(9.0)     # (8+9+10)/3


def test_ema_first_value_is_sma_seeded_and_recursion_holds(series):
    period = 3
    out = ind.ema(series, period)
    alpha = 2 / (period + 1)
    # pandas ewm(adjust=False) seeds on the first observation, so recompute
    # the recursion directly and compare.
    expected = series.iloc[0]
    for v in series.iloc[1:]:
        expected = alpha * v + (1 - alpha) * expected
    assert out.iloc[-1] == pytest.approx(expected)


def test_rsi_all_gains_is_100():
    rising = pd.Series(np.arange(1, 40, dtype="float64"))
    out = ind.rsi(rising, 14)
    assert out.iloc[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_zero():
    falling = pd.Series(np.arange(40, 1, -1, dtype="float64"))
    out = ind.rsi(falling, 14)
    assert out.iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_rsi_stays_in_range_on_noisy_data():
    rng = np.random.default_rng(42)
    noisy = pd.Series(100 + rng.standard_normal(500).cumsum())
    out = ind.rsi(noisy, 14).dropna()
    assert out.between(0, 100).all()


def test_true_range_uses_the_widest_of_three_measures():
    high = pd.Series([10.0, 12.0])
    low = pd.Series([9.0, 11.0])
    close = pd.Series([9.5, 11.5])
    tr = ind.true_range(high, low, close)
    # bar 1: high-low=1, |high-prev_close|=|12-9.5|=2.5, |low-prev_close|=1.5
    assert tr.iloc[1] == pytest.approx(2.5)


def test_atr_of_constant_range_equals_that_range():
    n = 50
    high = pd.Series([11.0] * n)
    low = pd.Series([10.0] * n)
    close = pd.Series([10.5] * n)
    out = ind.atr(high, low, close, 14)
    assert out.iloc[-1] == pytest.approx(1.0, abs=1e-6)


def test_bollinger_bands_are_symmetric_about_the_mean():
    rng = np.random.default_rng(0)
    s = pd.Series(100 + rng.standard_normal(100).cumsum())
    bb = ind.bollinger(s, 20, 2.0).dropna()
    assert ((bb["upper"] - bb["mid"]) - (bb["mid"] - bb["lower"])).abs().max() < 1e-9


def test_bollinger_width_is_zero_for_a_flat_series():
    flat = pd.Series([50.0] * 60)
    bb = ind.bollinger(flat, 20, 2.0)
    assert bb["upper"].iloc[-1] == pytest.approx(50.0)
    assert bb["lower"].iloc[-1] == pytest.approx(50.0)


def test_adx_is_high_for_a_clean_trend_and_low_for_chop():
    n = 200
    trend_close = pd.Series(np.linspace(100, 200, n))
    trend = ind.adx(trend_close + 1, trend_close - 1, trend_close, 14)

    chop_close = pd.Series([100 + (i % 2) for i in range(n)], dtype="float64")
    chop = ind.adx(chop_close + 1, chop_close - 1, chop_close, 14)

    assert trend["adx"].iloc[-1] > 40
    assert chop["adx"].iloc[-1] < 25


def test_macd_histogram_is_line_minus_signal():
    rng = np.random.default_rng(7)
    s = pd.Series(100 + rng.standard_normal(300).cumsum())
    m = ind.macd(s).dropna()
    assert ((m["macd"] - m["signal"]) - m["hist"]).abs().max() < 1e-12


def test_cross_helpers_fire_once_on_the_crossing_bar():
    a = pd.Series([1.0, 2, 3, 4, 3, 1])
    b = pd.Series([2.0, 2, 2, 2, 2, 2])
    up = ind.crossed_above(a, b)
    down = ind.crossed_below(a, b)
    assert list(up) == [False, False, True, False, False, False]
    assert list(down) == [False, False, False, False, False, True]


def test_indicators_never_use_future_bars():
    """Truncating the series must not change earlier indicator values.

    This is the property that makes backtests meaningful: an indicator value at
    bar i must depend only on bars 0..i.
    """
    rng = np.random.default_rng(11)
    s = pd.Series(100 + rng.standard_normal(400).cumsum())
    high, low = s + 1, s - 1

    cut = 300
    for name, full, truncated in [
        ("sma", ind.sma(s, 20), ind.sma(s.iloc[:cut], 20)),
        ("ema", ind.ema(s, 20), ind.ema(s.iloc[:cut], 20)),
        ("rsi", ind.rsi(s, 14), ind.rsi(s.iloc[:cut], 14)),
        ("atr", ind.atr(high, low, s, 14), ind.atr(high.iloc[:cut], low.iloc[:cut], s.iloc[:cut], 14)),
    ]:
        diff = (full.iloc[:cut] - truncated).abs().max()
        assert diff < 1e-9, f"{name} changed retroactively by {diff}"
