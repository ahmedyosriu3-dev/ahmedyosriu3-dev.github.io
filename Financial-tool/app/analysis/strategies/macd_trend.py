"""MACD histogram trend-following with a long-MA regime filter."""
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
class MACDParams(Params):
    fast: int = 12
    slow: int = 26
    signal: int = 9
    trend_ma: int = 100        # 0 disables the regime filter
    atr_period: int = 14
    atr_stop_mult: float = 3.0
    atr_target_mult: float = 6.0


@register
class MACDTrendStrategy(Strategy):
    key = "macd_trend"
    label = "MACD Trend"
    description = (
        "Goes long when the MACD line crosses above its signal line while "
        "price is in a healthy regime; exits on the reverse cross."
    )
    grid = [
        {"fast": 12, "slow": 26, "signal": 9},
        {"fast": 8, "slow": 21, "signal": 5},
        {"fast": 12, "slow": 26, "signal": 9, "trend_ma": 0},
        {"fast": 19, "slow": 39, "signal": 9},
    ]

    @staticmethod
    def default_params() -> MACDParams:
        return MACDParams()

    @property
    def min_bars(self) -> int:
        return max(self.params.trend_ma, self.params.slow * 3) + 10

    def run(self, bars: pd.DataFrame) -> StrategyResult:
        p: MACDParams = self.params
        close, high, low = bars["close"], bars["high"], bars["low"]

        m = ind.macd(close, p.fast, p.slow, p.signal)
        atr = ind.atr(high, low, close, p.atr_period)

        if p.trend_ma > 0:
            trend = ind.sma(close, p.trend_ma)
            regime_ok = close > trend
        else:
            trend = pd.Series(float("nan"), index=bars.index)
            regime_ok = pd.Series(True, index=bars.index)

        cross_up = ind.crossed_above(m["macd"], m["signal"])
        entry = cross_up & regime_ok
        exit_ = ind.crossed_below(m["macd"], m["signal"])

        position = hold_between(entry, exit_)

        stop = close - p.atr_stop_mult * atr
        target = close + p.atr_target_mult * atr

        reason = pd.Series("", index=bars.index, dtype="object")
        reason[entry] = "MACD crossed above its signal line in an uptrend"
        reason[exit_] = "MACD crossed below its signal line"
        reason[cross_up & ~regime_ok] = (
            f"MACD cross ignored: price below its {p.trend_ma}-day average"
        )

        indicators = pd.DataFrame(
            {
                "MACD": m["macd"],
                "Signal": m["signal"],
                "Hist": m["hist"],
                f"SMA{p.trend_ma}": trend,
                "ATR": atr,
            }
        )
        return StrategyResult(position, stop, target, reason, indicators)
