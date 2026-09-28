"""Strategy interface and registry.

A strategy turns a bar DataFrame into a position series plus human-readable
reasons. It never sees the future: every column it produces at bar *i* must be
computable from bars 0..i. The backtester enforces this by executing at the
*next* bar's open.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, fields
from typing import Any, ClassVar

import numpy as np
import pandas as pd

REGISTRY: dict[str, type["Strategy"]] = {}


def register(cls: type["Strategy"]) -> type["Strategy"]:
    REGISTRY[cls.key] = cls
    return cls


def get_strategy(key: str, **params: Any) -> "Strategy":
    if key not in REGISTRY:
        raise KeyError(f"unknown strategy {key!r}; have {sorted(REGISTRY)}")
    return REGISTRY[key](**params)


def list_strategies() -> list[type["Strategy"]]:
    return [REGISTRY[k] for k in sorted(REGISTRY)]


@dataclass
class StrategyResult:
    """What a strategy produces for a run of bars.

    position    1 = long, 0 = flat (target position for the *next* bar)
    stop        suggested protective stop for each bar (may be NaN)
    target      suggested profit target (may be NaN)
    reason      short human-readable explanation, non-empty on change bars
    """

    position: pd.Series
    stop: pd.Series
    target: pd.Series
    reason: pd.Series
    indicators: pd.DataFrame


@dataclass
class Params:
    """Base for strategy parameter dataclasses."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]


class Strategy(ABC):
    key: ClassVar[str] = "base"
    label: ClassVar[str] = "Base"
    description: ClassVar[str] = ""
    # Small grids the selector sweeps. Keep them small -- more combinations
    # means more chances to fit noise.
    grid: ClassVar[list[dict[str, Any]]] = [{}]

    def __init__(self, **params: Any) -> None:
        self.params = self.default_params()
        for k, v in params.items():
            if hasattr(self.params, k):
                setattr(self.params, k, v)

    @staticmethod
    @abstractmethod
    def default_params() -> Params:
        ...

    @abstractmethod
    def run(self, bars: pd.DataFrame) -> StrategyResult:
        """Compute target positions over `bars`."""

    # -- helpers ---------------------------------------------------------
    @property
    def min_bars(self) -> int:
        """Bars needed before this strategy can say anything useful."""
        return 60

    def params_dict(self) -> dict[str, Any]:
        return self.params.to_dict()

    def __repr__(self) -> str:
        return f"{self.key}({self.params.to_dict()})"


def hold_between(entry: pd.Series, exit_: pd.Series) -> pd.Series:
    """Turn entry/exit event flags into a held 1/0 position series.

    Entry wins on a bar where both fire. Uses forward-fill rather than a Python
    loop so it stays fast over long histories.
    """
    state = pd.Series(np.nan, index=entry.index, dtype="float64")
    state[exit_] = 0.0
    state[entry] = 1.0
    return state.ffill().fillna(0.0)
