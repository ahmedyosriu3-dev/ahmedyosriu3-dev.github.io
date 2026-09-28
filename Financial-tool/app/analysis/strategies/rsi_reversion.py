"""RSI mean reversion, filtered to buy dips only inside an uptrend.

Buying oversold readings in a downtrend is how mean-reversion systems die, so
entries require price to be above a long moving average.
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
class RSIParams(Params):
    rsi_period: int = 14
    oversold: float = 30.0
    exit_level: float = 55.0
    trend_ma: int = 200        # 0 disables the trend filter
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    atr_target_mult: float = 3.0


@register
class RSIReversionStrategy(Strategy):
    key = "rsi_reversion"
    label = "RSI Mean Reversion"
    description = (
        "Buys oversold RSI dips while price is above its long-term moving "
        "average, and exits once RSI recovers to neutral."
    )
    grid = [
        {"oversold": 30.0, "exit_level": 55.0},
        {"oversold": 25.0, "exit_level": 60.0},
        {"oversold": 35.0, "exit_level": 50.0},
        {"oversold": 30.0, "exit_level": 55.0, "trend_ma": 0},
    ]

    @staticmethod
    def default_params() -> RSIParams:
        return RSIParams()

    @property
    def min_bars(self) -> int:
        return max(self.params.trend_ma, self.params.rsi_period * 3) + 10

    def run(self, bars: pd.DataFrame) -> StrategyResult:
        p: RSIParams = self.params
        close, high, low = bars["close"], bars["high"], bars["low"]

        rsi = ind.rsi(close, p.rsi_period)
        atr = ind.atr(high, low, close, p.atr_period)

        if p.trend_ma > 0:
            trend = ind.sma(close, p.trend_ma)
            in_uptrend = close > trend
        else:
            trend = pd.Series(float("nan"), index=bars.index)
            in_uptrend = pd.Series(True, index=bars.index)

        entry = ind.crossed_above(rsi, pd.Series(p.oversold, index=bars.index)) & in_uptrend
        exit_ = rsi >= p.exit_level

        position = hold_between(entry, exit_)

        stop = close - p.atr_stop_mult * atr
        target = close + p.atr_target_mult * atr

        reason = pd.Series("", index=bars.index, dtype="object")
        reason[entry] = (
            "RSI recovered above "
            + f"{p.oversold:g} (now "
            + rsi[entry].round(1).astype(str)
            + ")"
            + (" with price above its long MA" if p.trend_ma else "")
        )
        first_exit = exit_ & (position.shift(1) == 1)
        reason[first_exit] = (
            f"RSI back to neutral (" + rsi[first_exit].round(1).astype(str) + ")"
        )
        blocked = ind.crossed_above(rsi, pd.Series(p.oversold, index=bars.index)) & ~in_uptrend
        reason[blocked] = f"Oversold bounce ignored: price below its {p.trend_ma}-day average"

        indicators = pd.DataFrame(
            {"RSI": rsi, f"SMA{p.trend_ma}": trend, "ATR": atr}
        )
        return StrategyResult(position, stop, target, reason, indicators)
