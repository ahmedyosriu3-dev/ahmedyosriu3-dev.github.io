"""Bollinger Bands, in either breakout or mean-reversion mode.

The same bands support opposite trades depending on regime, so the mode is a
parameter and the selector decides which one a given symbol deserves.
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
class BollingerParams(Params):
    period: int = 20
    std: float = 2.0
    mode: str = "reversion"    # "reversion" | "breakout"
    atr_period: int = 14
    atr_stop_mult: float = 2.5
    atr_target_mult: float = 4.0


@register
class BollingerStrategy(Strategy):
    key = "bollinger"
    label = "Bollinger Bands"
    description = (
        "Reversion mode buys the lower band and sells back at the middle. "
        "Breakout mode buys a close above the upper band and rides it."
    )
    grid = [
        {"period": 20, "std": 2.0, "mode": "reversion"},
        {"period": 20, "std": 2.5, "mode": "reversion"},
        {"period": 20, "std": 2.0, "mode": "breakout"},
        {"period": 50, "std": 2.0, "mode": "breakout"},
    ]

    @staticmethod
    def default_params() -> BollingerParams:
        return BollingerParams()

    @property
    def min_bars(self) -> int:
        return self.params.period * 3 + 10

    def run(self, bars: pd.DataFrame) -> StrategyResult:
        p: BollingerParams = self.params
        close, high, low = bars["close"], bars["high"], bars["low"]

        bb = ind.bollinger(close, p.period, p.std)
        atr = ind.atr(high, low, close, p.atr_period)

        if p.mode == "breakout":
            entry = ind.crossed_above(close, bb["upper"])
            exit_ = ind.crossed_below(close, bb["mid"])
            entry_reason = f"Closed above the upper band ({p.period}, {p.std:g}sd) - breakout"
            exit_reason = "Fell back through the middle band"
        else:
            entry = ind.crossed_above(close, bb["lower"])
            exit_ = ind.crossed_above(close, bb["mid"])
            entry_reason = f"Bounced back above the lower band ({p.period}, {p.std:g}sd)"
            exit_reason = "Reverted to the middle band"

        position = hold_between(entry, exit_)

        stop = close - p.atr_stop_mult * atr
        target = bb["mid"] if p.mode == "reversion" else close + p.atr_target_mult * atr

        reason = pd.Series("", index=bars.index, dtype="object")
        reason[entry] = entry_reason
        reason[exit_] = exit_reason

        indicators = pd.DataFrame(
            {
                "BB upper": bb["upper"],
                "BB mid": bb["mid"],
                "BB lower": bb["lower"],
                "%B": bb["pct_b"],
                "ATR": atr,
            }
        )
        return StrategyResult(position, stop, target, reason, indicators)
