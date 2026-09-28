"""Technical indicators.

Hand-rolled on pandas rather than pulled from a TA package: it is a small
amount of code, it has no dependency that breaks on every numpy release, and
every formula here is unit-tested against known values in tests/.

Convention: every function returns a Series (or DataFrame) aligned to the input
index, with NaN for the warm-up period. Nothing here looks at future bars.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period, min_periods=period).mean()


def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(s: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing == EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # All-gain windows give avg_loss == 0 -> RSI is 100 by definition.
    return out.where(avg_loss.ne(0.0) | avg_gain.isna(), 100.0)


def macd(
    s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    macd_line = ema(s, fast) - ema(s, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "hist": macd_line - signal_line,
        }
    )


def bollinger(s: pd.Series, period: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = sma(s, period)
    dev = s.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + std * dev
    lower = mid - std * dev
    width = (upper - lower) / mid.replace(0.0, np.nan)
    pct_b = (s - lower) / (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {"mid": mid, "upper": upper, "lower": lower, "width": width, "pct_b": pct_b}
    )


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.DataFrame:
    """Average Directional Index -- trend *strength*, direction-agnostic."""
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    alpha = 1 / period
    atr_ = true_range(high, low, close).ewm(
        alpha=alpha, adjust=False, min_periods=period
    ).mean()

    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_.replace(0.0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_.replace(0.0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_ = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return pd.DataFrame({"adx": adx_, "plus_di": plus_di, "minus_di": minus_di})


def rolling_return(s: pd.Series, period: int) -> pd.Series:
    return s.pct_change(period)


def slope(s: pd.Series, period: int) -> pd.Series:
    """Normalised linear-regression slope over `period` bars (per-bar % change)."""
    def _fit(window: np.ndarray) -> float:
        x = np.arange(len(window), dtype="float64")
        b = np.polyfit(x, window, 1)[0]
        mean = window.mean()
        return b / mean if mean else np.nan

    return s.rolling(period, min_periods=period).apply(_fit, raw=True)


def crossed_above(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossed_below(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))
