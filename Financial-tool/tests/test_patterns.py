"""Pattern statistics.

These tests feed the module series whose behaviour is known in advance -- a
random walk, a deliberately mean-reverting series, a deliberately trending one
-- and check it says the right thing about each. A statistic that cannot tell
a random walk from a real pattern is worse than no statistic, because it comes
with a number attached.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis.patterns import (
    Z_SIGNIFICANT,
    analyse,
    dip_edge,
    dip_profile,
    monthly_stats,
    variance_ratio,
)


def series(values: np.ndarray) -> pd.Series:
    idx = pd.date_range("2010-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype="float64")


def random_walk(n: int = 3000, seed: int = 7, sigma: float = 0.01) -> pd.Series:
    rng = np.random.default_rng(seed)
    return series(100.0 * np.exp(np.cumsum(rng.normal(0, sigma, n))))


def mean_reverting(n: int = 3000, seed: int = 7, phi: float = -0.25) -> pd.Series:
    """An AR(1) in returns with a negative coefficient: moves get undone."""
    rng = np.random.default_rng(seed)
    shocks = rng.normal(0, 0.01, n)
    rets = np.zeros(n)
    for t in range(1, n):
        rets[t] = phi * rets[t - 1] + shocks[t]
    return series(100.0 * np.exp(np.cumsum(rets)))


def trending(n: int = 3000, seed: int = 7, phi: float = 0.25) -> pd.Series:
    rng = np.random.default_rng(seed)
    shocks = rng.normal(0, 0.01, n)
    rets = np.zeros(n)
    for t in range(1, n):
        rets[t] = phi * rets[t - 1] + shocks[t]
    return series(100.0 * np.exp(np.cumsum(rets)))


def log_rets(prices: pd.Series) -> pd.Series:
    return np.log(prices / prices.shift(1)).dropna()


# --------------------------------------------------------------------------
# Variance ratio
# --------------------------------------------------------------------------
def test_random_walk_is_not_called_a_pattern():
    """The test that matters most: no finding where there is nothing to find."""
    vr = variance_ratio(log_rets(random_walk()), 5)

    assert vr is not None
    assert vr.ratio == pytest.approx(1.0, abs=0.15)
    assert abs(vr.z) < Z_SIGNIFICANT
    assert vr.verdict == "random walk"


def test_mean_reverting_series_is_detected():
    vr = variance_ratio(log_rets(mean_reverting()), 5)

    assert vr is not None
    assert vr.ratio < 1.0
    assert vr.z <= -Z_SIGNIFICANT
    assert vr.verdict == "mean reverting"


def test_trending_series_is_detected():
    vr = variance_ratio(log_rets(trending()), 5)

    assert vr is not None
    assert vr.ratio > 1.0
    assert vr.z >= Z_SIGNIFICANT
    assert vr.verdict == "trending"


def test_variance_ratio_needs_enough_data():
    assert variance_ratio(log_rets(random_walk(n=30)), 21) is None


# --------------------------------------------------------------------------
# Dips and recoveries
# --------------------------------------------------------------------------
def test_a_single_dip_and_recovery_is_counted_once():
    """One V-shaped fall is one episode, not one per day spent below the line."""
    path = [100.0] * 10 + [90.0, 85.0, 80.0, 85.0, 95.0] + [105.0] * 10
    p = dip_profile(series(np.array(path)), -0.10)

    assert p.episodes == 1
    assert p.recovered == 1
    assert p.still_underwater is False
    assert p.median_days_to_recover > 0


def test_an_unrecovered_fall_is_not_counted_as_a_recovery():
    path = [100.0] * 10 + [80.0] * 20
    p = dip_profile(series(np.array(path)), -0.10)

    assert p.episodes == 1
    assert p.recovered == 0
    assert p.recovery_rate == 0.0
    assert p.still_underwater is True


def test_two_separate_falls_are_two_episodes():
    path = ([100.0] * 5 + [85.0] * 3 + [101.0] * 5
            + [88.0] * 3 + [102.0] * 5)
    p = dip_profile(series(np.array(path)), -0.10)

    assert p.episodes == 2
    assert p.recovered == 2


def sawtooth(cycles: int, trough: float, rebound: float,
             drift: float = 1.0) -> pd.Series:
    """Repeated falls and rebounds, optionally drifting down over time."""
    path: list[float] = []
    level = 100.0
    for _ in range(cycles):
        path += list(np.linspace(level, level * trough, 40))
        path += list(np.linspace(level * trough, level * rebound, 40))
        level *= drift
    return series(np.array(path))


def test_repeated_falls_that_recover_are_counted_as_recoveries():
    """The shape the question is really about: it drops, it comes back."""
    p = dip_profile(sawtooth(cycles=6, trough=0.85, rebound=1.02), -0.10)

    assert p.episodes >= 5, "each fall should register separately"
    assert p.recovery_rate > 0.8, "these falls all regained the prior level"
    assert p.median_days_to_recover > 0


def test_a_structural_decline_is_not_reported_as_recovering():
    """The USO case, and the one a dip-buyer most needs to be told about.

    An instrument losing ground every cycle spends its life below its 52-week
    high. The measure must not launder that into a tidy series of recoveries;
    it must also not collapse into one episode that never resolves.
    """
    p = dip_profile(sawtooth(cycles=6, trough=0.75, rebound=0.95, drift=0.90),
                    -0.10)

    assert p.episodes >= 1
    assert p.recovery_rate == 0.0, "it never regained the level it fell from"
    assert p.still_underwater is False, "the bounded horizon must resolve it"


def test_a_series_that_never_falls_has_no_episodes():
    p = dip_profile(series(np.linspace(100, 200, 500)), -0.10)

    assert p.episodes == 0
    assert p.recovery_rate == 0.0


# --------------------------------------------------------------------------
# Dip edge
# --------------------------------------------------------------------------
def test_dip_edge_reports_no_edge_on_a_random_walk():
    edge = dip_edge(random_walk(n=4000), trigger=-0.10, forward=126)

    if edge.samples_after_dip:
        # A random walk has no memory, so being down should not predict the
        # next six months either way. The bound is loose because overlapping
        # windows make this measure noisy -- which is why the module reports
        # it without a p-value.
        assert abs(edge.edge) < 0.25


def test_dip_edge_needs_enough_history():
    edge = dip_edge(random_walk(n=100))
    assert edge.samples_after_dip == 0


# --------------------------------------------------------------------------
# Seasonality
# --------------------------------------------------------------------------
def test_monthly_stats_cover_the_calendar():
    stats = monthly_stats(random_walk(n=3000))

    assert len(stats) == 12
    assert [s.month for s in stats] == list(range(1, 13))
    assert all(0.0 <= s.hit_rate <= 1.0 for s in stats)
    assert stats[0].label == "January"


# --------------------------------------------------------------------------
# The full report
# --------------------------------------------------------------------------
def _bars(prices: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": prices, "high": prices, "low": prices, "close": prices,
         "adj_close": prices, "volume": 1e7},
        index=prices.index,
    )


def test_report_on_a_random_walk_claims_nothing():
    report = analyse("RAND", "Random walk", bars=_bars(random_walk(n=3000)))

    assert report is not None
    assert "random walk" in report.headline.lower()
    assert report.split_agrees is False


def test_report_on_a_real_pattern_agrees_across_both_halves():
    report = analyse("REV", "Mean reverter", bars=_bars(mean_reverting(n=4000)))

    assert report is not None
    assert report.split_agrees is True
    assert "mean reverting" in report.headline.lower()
    assert report.ratio_at(21) is not None


def test_short_history_is_refused():
    assert analyse("TINY", bars=_bars(random_walk(n=100))) is None
