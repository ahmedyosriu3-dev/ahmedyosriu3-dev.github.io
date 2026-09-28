"""Backtester correctness, including the lookahead-bias audit."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis.backtest import run_backtest
from app.analysis.strategies.base import Params, Strategy, StrategyResult


def make_bars(closes, spread=0.0):
    """Build a bar frame where open == close, so fills are exactly predictable."""
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    c = pd.Series(closes, index=idx, dtype="float64")
    return pd.DataFrame(
        {
            "open": c,
            "high": c + spread,
            "low": c - spread,
            "close": c,
            "adj_close": c,
            "volume": 1e6,
        },
        index=idx,
    )


class ScriptedParams(Params):
    pass


class ScriptedStrategy(Strategy):
    """Emits a position series supplied by the test. No indicators involved."""

    key = "scripted"
    label = "Scripted"

    def __init__(self, positions, stops=None, targets=None):
        super().__init__()
        self._positions = positions
        self._stops = stops
        self._targets = targets

    @staticmethod
    def default_params() -> ScriptedParams:
        return ScriptedParams()

    @property
    def min_bars(self) -> int:
        return 0

    def run(self, bars: pd.DataFrame) -> StrategyResult:
        pos = pd.Series(self._positions, index=bars.index, dtype="float64")
        nan = pd.Series(np.nan, index=bars.index)
        stop = pd.Series(self._stops, index=bars.index, dtype="float64") if self._stops else nan
        tgt = pd.Series(self._targets, index=bars.index, dtype="float64") if self._targets else nan
        return StrategyResult(pos, stop, tgt, pd.Series("", index=bars.index), pd.DataFrame(index=bars.index))


def test_known_single_trade_pnl_is_exact():
    # Signal on bar 0 -> buy at bar 1 open (100). Exit signal bar 3 -> sell at
    # bar 4 open (120). 100 shares from 10_000 cash.
    bars = make_bars([100, 100, 110, 115, 120, 120])
    strat = ScriptedStrategy([1, 1, 1, 0, 0, 0])
    r = run_backtest(strat, bars, initial_cash=10_000, slippage_bps=0, use_stops=False)

    assert len(r.trades) == 1
    t = r.trades[0]
    assert t.entry_price == pytest.approx(100.0)
    assert t.exit_price == pytest.approx(120.0)
    assert t.qty == 100
    assert t.pnl == pytest.approx(2_000.0)
    assert r.metrics["final_equity"] == pytest.approx(12_000.0)


def test_execution_happens_at_next_bar_open_not_the_signal_bar():
    # Price spikes on the very bar the signal is generated. A backtester that
    # cheats would capture that spike; this one must not.
    bars = make_bars([100, 200, 200, 200])
    strat = ScriptedStrategy([1, 1, 1, 1])
    r = run_backtest(strat, bars, initial_cash=10_000, slippage_bps=0, use_stops=False)

    assert r.trades[0].entry_price == pytest.approx(200.0), "entered at signal bar price"


def test_stop_loss_fills_at_the_stop_level():
    bars = make_bars([100, 100, 100, 100], spread=0.0)
    # Drive the low of bar 2 through the stop.
    bars.loc[bars.index[2], "low"] = 88.0
    strat = ScriptedStrategy([1, 1, 1, 1], stops=[90.0] * 4)
    r = run_backtest(strat, bars, initial_cash=10_000, slippage_bps=0)

    assert len(r.trades) == 1
    assert r.trades[0].exit_kind == "stop"
    assert r.trades[0].exit_price == pytest.approx(90.0)


def test_stop_out_does_not_immediately_re_enter():
    """A stopped-out position must wait for a fresh signal, not re-buy at once.

    The strategy's position series still reads "long" after a stop fires, so
    without a latch the engine would re-enter every bar and manufacture fake
    performance.
    """
    bars = make_bars([100.0] * 8)
    bars.loc[bars.index[2], "low"] = 88.0
    strat = ScriptedStrategy([1] * 8, stops=[90.0] * 8)
    r = run_backtest(strat, bars, initial_cash=10_000, slippage_bps=0)

    assert len(r.trades) == 1
    assert r.trades[0].exit_kind == "stop"


def test_re_entry_is_allowed_after_the_signal_goes_flat_again():
    bars = make_bars([100.0] * 8)
    bars.loc[bars.index[2], "low"] = 88.0
    #        bar: 0  1  2  3  4  5  6  7
    strat = ScriptedStrategy([1, 1, 1, 0, 0, 1, 1, 1], stops=[90.0] * 8)
    r = run_backtest(strat, bars, initial_cash=10_000, slippage_bps=0)

    assert len(r.trades) == 2, "a fresh signal after going flat should re-enter"
    assert r.trades[0].exit_kind == "stop"


def test_gap_through_the_stop_fills_at_the_open_not_the_stop():
    bars = make_bars([100, 100, 100])
    bars.loc[bars.index[2], ["open", "high", "low", "close"]] = [80.0, 82.0, 79.0, 81.0]
    strat = ScriptedStrategy([1, 1, 1], stops=[90.0] * 3)
    r = run_backtest(strat, bars, initial_cash=10_000, slippage_bps=0)

    assert r.trades[0].exit_kind == "stop"
    assert r.trades[0].exit_price == pytest.approx(80.0), "gap should fill at the open"


def test_stop_wins_when_stop_and_target_are_both_touched():
    bars = make_bars([100, 100, 100])
    bars.loc[bars.index[2], "low"] = 85.0
    bars.loc[bars.index[2], "high"] = 130.0
    strat = ScriptedStrategy([1, 1, 1], stops=[90.0] * 3, targets=[120.0] * 3)
    r = run_backtest(strat, bars, initial_cash=10_000, slippage_bps=0)

    assert r.trades[0].exit_kind == "stop", "ambiguous bars must resolve pessimistically"


def test_slippage_makes_a_round_trip_cost_money():
    bars = make_bars([100] * 6)
    strat = ScriptedStrategy([1, 1, 1, 0, 0, 0])
    clean = run_backtest(strat, bars, initial_cash=10_000, slippage_bps=0, use_stops=False)
    dirty = run_backtest(strat, bars, initial_cash=10_000, slippage_bps=50, use_stops=False)

    assert clean.metrics["final_equity"] == pytest.approx(10_000.0)
    assert dirty.metrics["final_equity"] < 10_000.0


def test_never_long_means_no_trades_and_flat_equity():
    bars = make_bars([100, 150, 90, 200])
    r = run_backtest(ScriptedStrategy([0, 0, 0, 0]), bars, initial_cash=10_000, slippage_bps=0)
    assert r.trades == []
    assert r.metrics["final_equity"] == pytest.approx(10_000.0)
    assert r.metrics["max_drawdown"] == pytest.approx(0.0)


def test_open_position_is_closed_at_the_final_bar():
    bars = make_bars([100, 100, 130])
    r = run_backtest(ScriptedStrategy([1, 1, 1]), bars, initial_cash=10_000, slippage_bps=0, use_stops=False)
    assert len(r.trades) == 1
    assert r.trades[0].exit_kind == "end"


def test_max_drawdown_is_measured_from_the_running_peak():
    # Ride 100 -> 200 -> 100 fully invested: a 50% drawdown from the peak.
    bars = make_bars([100, 100, 200, 100, 100])
    r = run_backtest(ScriptedStrategy([1, 1, 1, 1, 0]), bars, initial_cash=10_000,
                     slippage_bps=0, use_stops=False)
    assert r.metrics["max_drawdown"] == pytest.approx(-0.5, abs=0.01)


def test_shifting_signals_forward_changes_the_result():
    """The lookahead audit.

    If delaying every signal by one bar leaves results identical, the engine is
    not actually respecting signal timing and results cannot be trusted.
    """
    rng = np.random.default_rng(3)
    closes = 100 + rng.standard_normal(300).cumsum()
    bars = make_bars(closes, spread=0.5)

    pos = (pd.Series(closes).diff() > 0).astype("float64").tolist()
    delayed = [0.0] + pos[:-1]

    a = run_backtest(ScriptedStrategy(pos), bars, initial_cash=10_000, use_stops=False)
    b = run_backtest(ScriptedStrategy(delayed), bars, initial_cash=10_000, use_stops=False)

    assert a.metrics["final_equity"] != pytest.approx(b.metrics["final_equity"])


def test_cannot_buy_more_than_cash_allows():
    bars = make_bars([100, 3_000, 3_000])
    r = run_backtest(ScriptedStrategy([1, 1, 1]), bars, initial_cash=10_000,
                     slippage_bps=0, use_stops=False)
    # 10_000 / 3_000 -> 3 whole shares, never 4.
    assert r.trades[0].qty == 3
