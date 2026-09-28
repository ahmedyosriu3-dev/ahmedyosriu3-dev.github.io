"""EMA crossover with a trend filter and an ATR trailing stop."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
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
class MACrossParams(Params):
    fast: int = 20
    slow: int = 50
    adx_period: int = 14
    adx_min: float = 20.0      # below this the market is chopping; stand aside
    atr_period: int = 14
    atr_stop_mult: float = 2.5
    atr_target_mult: float = 5.0


@register
class MACrossStrategy(Strategy):
    key = "ma_cross"
    label = "EMA Crossover"
    description = (
        "Goes long when the fast EMA crosses above the slow EMA while ADX "
        "confirms a real trend. Exits on the opposite cross or an ATR stop."
    )
    grid = [
        {"fast": 10, "slow": 30},
        {"fast": 20, "slow": 50},
        {"fast": 50, "slow": 200},
        {"fast": 20, "slow": 50, "adx_min": 25.0},
    ]

    @staticmethod
    def default_params() -> MACrossParams:
        return MACrossParams()

    @property
    def min_bars(self) -> int:
        return self.params.slow + self.params.adx_period + 10

    def run(self, bars: pd.DataFrame) -> StrategyResult:
        p: MACrossParams = self.params
        close, high, low = bars["close"], bars["high"], bars["low"]

        fast = ind.ema(close, p.fast)
        slow = ind.ema(close, p.slow)
        adx_df = ind.adx(high, low, close, p.adx_period)
        atr = ind.atr(high, low, close, p.atr_period)

        entry = ind.crossed_above(fast, slow) & (adx_df["adx"] >= p.adx_min)
        exit_ = ind.crossed_below(fast, slow)

        position = hold_between(entry, exit_)

        stop = close - p.atr_stop_mult * atr
        target = close + p.atr_target_mult * atr

        reason = pd.Series("", index=bars.index, dtype="object")
        reason[entry] = (
            f"EMA{p.fast} crossed above EMA{p.slow}, ADX "
            + adx_df["adx"][entry].round(1).astype(str)
            + f" >= {p.adx_min:g}"
        )
        reason[exit_] = f"EMA{p.fast} crossed back below EMA{p.slow}"
        # Crosses blocked by the ADX filter are worth showing -- they explain
        # why an obvious-looking signal did not fire.
        blocked = ind.crossed_above(fast, slow) & (adx_df["adx"] < p.adx_min)
        reason[blocked] = (
            f"EMA cross ignored: ADX "
            + adx_df["adx"][blocked].round(1).astype(str)
            + f" below {p.adx_min:g} (no trend)"
        )

        indicators = pd.DataFrame(
            {
                f"EMA{p.fast}": fast,
                f"EMA{p.slow}": slow,
                "ADX": adx_df["adx"],
                "ATR": atr,
            }
        )
        return StrategyResult(position, stop, target, reason, indicators)

