"""Absolute momentum (time-series trend following).

Holds while the symbol's own trailing return and trend slope are positive, and
steps aside when they are not. The dashboard also ranks symbols by this
strategy's momentum score, which is the cross-sectional view of the same idea.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.analysis import indicators as ind
from app.analysis.strategies.base import (
    Params,
    Strategy,
    StrategyResult,
    hold_between,
    register,
)


@dataclass
class MomentumParams(Params):
    lookback: int = 126        # ~6 months of trading days
    trend_ma: int = 100
    min_return: float = 0.0    # required trailing return to be long
    atr_period: int = 14
    atr_stop_mult: float = 3.0
    atr_target_mult: float = 8.0


@register
class MomentumStrategy(Strategy):
    key = "momentum"
    label = "Momentum"
    description = (
        "Holds while the symbol's trailing return is positive and it trades "
        "above its trend average. Steps aside when momentum turns negative."
    )
    grid = [
        {"lookback": 63, "trend_ma": 50},
        {"lookback": 126, "trend_ma": 100},
        {"lookback": 252, "trend_ma": 200},
        {"lookback": 126, "trend_ma": 100, "min_return": 0.05},
    ]

    @staticmethod
    def default_params() -> MomentumParams:
        return MomentumParams()

    @property
    def min_bars(self) -> int:
        return max(self.params.lookback, self.params.trend_ma) + 20

    def run(self, bars: pd.DataFrame) -> StrategyResult:
        p: MomentumParams = self.params
        close, high, low = bars["close"], bars["high"], bars["low"]

        ret = ind.rolling_return(close, p.lookback)
        trend = ind.sma(close, p.trend_ma)
        atr = ind.atr(high, low, close, p.atr_period)

        bullish = (ret > p.min_return) & (close > trend)
        entry = bullish & ~bullish.shift(1, fill_value=False)
        exit_ = ~bullish & bullish.shift(1, fill_value=False)

        position = hold_between(entry, exit_)

        stop = close - p.atr_stop_mult * atr
        target = close + p.atr_target_mult * atr

        reason = pd.Series("", index=bars.index, dtype="object")
        reason[entry] = (
            f"{p.lookback}-day return "
            + (ret[entry] * 100).round(1).astype(str)
            + f"% and price above its {p.trend_ma}-day average"
        )
        reason[exit_] = "Momentum faded - trailing return or trend turned negative"

        indicators = pd.DataFrame(
            {
                f"Return {p.lookback}d": ret,
                f"SMA{p.trend_ma}": trend,
                "ATR": atr,
            }
        )
        return StrategyResult(position, stop, target, reason, indicators)


def momentum_score(bars: pd.DataFrame, lookback: int = 126) -> float:
    """Cross-sectional ranking score: trailing return scaled by volatility."""
    if len(bars) < lookback + 2:
        return float("nan")
    close = bars["close"]
    ret = float(close.iloc[-1] / close.iloc[-lookback - 1] - 1.0)
    vol = float(close.pct_change().tail(lookback).std())
    if not vol:
        return float("nan")
    return ret / (vol * (lookback ** 0.5))
