"""Backtest engine.

Deliberately conservative, because a backtester that flatters a strategy is
worse than no backtester at all:

  * Signals computed on bar *i* are executed at bar *i+1*'s **open**. A
    strategy can never trade on information from the bar it is reacting to.
  * Stops and targets are checked against each bar's high/low while the
    position is held, and when both are touched in one bar the **stop** is
    assumed to have hit first (the pessimistic assumption).
  * Commission and slippage are charged on every fill.

Long-only, one position per symbol at a time.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from app.analysis.strategies.base import Strategy

TRADING_DAYS = 252


@dataclass
class Trade:
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    return_pct: float
    bars_held: int
    exit_kind: str          # "signal" | "stop" | "target" | "end"
    entry_reason: str = ""


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    symbol: str
    start: datetime | None
    end: datetime | None
    trades: list[Trade] = field(default_factory=list)
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "params": self.params,
            "symbol": self.symbol,
            "metrics": self.metrics,
            "n_trades": len(self.trades),
        }


def _metrics(
    equity: pd.Series, trades: list[Trade], initial: float
) -> dict:
    """Summary statistics. Returns zeros rather than NaN so the UI stays clean."""
    if equity.empty:
        return {
            "total_return": 0.0, "cagr": 0.0, "sharpe": 0.0, "max_drawdown": 0.0,
            "win_rate": 0.0, "profit_factor": 0.0, "n_trades": 0,
            "avg_bars_held": 0.0, "exposure": 0.0, "final_equity": initial,
        }

    total_return = float(equity.iloc[-1] / initial - 1.0)

    days = max((equity.index[-1] - equity.index[0]).days, 1)
    years = days / 365.25
    cagr = float((equity.iloc[-1] / initial) ** (1 / years) - 1.0) if years > 0 else 0.0

    rets = equity.pct_change().dropna()
    if len(rets) > 1 and rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * np.sqrt(TRADING_DAYS))
    else:
        sharpe = 0.0

    running_max = equity.cummax()
    max_dd = float((equity / running_max - 1.0).min())

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = float(gross_win > 0) * 99.0

    avg_bars = float(np.mean([t.bars_held for t in trades])) if trades else 0.0
    exposure = float(sum(t.bars_held for t in trades) / len(equity)) if trades else 0.0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "win_rate": win_rate,
        "profit_factor": float(profit_factor),
        "n_trades": len(trades),
        "avg_bars_held": avg_bars,
        "exposure": exposure,
        "final_equity": float(equity.iloc[-1]),
    }


def run_backtest(
    strategy: Strategy,
    bars: pd.DataFrame,
    symbol: str = "",
    initial_cash: float = 10_000.0,
    commission: float = 0.0,
    slippage_bps: float = 5.0,
    use_stops: bool = True,
) -> BacktestResult:
    """Run `strategy` over `bars` and return trades, equity curve and metrics."""
    result = BacktestResult(
        strategy=strategy.key,
        params=strategy.params_dict(),
        symbol=symbol,
        start=bars.index[0].to_pydatetime() if len(bars) else None,
        end=bars.index[-1].to_pydatetime() if len(bars) else None,
    )
    if len(bars) < strategy.min_bars + 2:
        result.metrics = _metrics(pd.Series(dtype="float64"), [], initial_cash)
        return result

    sig = strategy.run(bars)

    idx = bars.index
    open_ = bars["open"].to_numpy(dtype="float64")
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    close = bars["close"].to_numpy(dtype="float64")

    target_pos = sig.position.to_numpy(dtype="float64")
    stop_arr = sig.stop.to_numpy(dtype="float64")
    tgt_arr = sig.target.to_numpy(dtype="float64")
    reasons = sig.reason.to_numpy(dtype=object)

    slip = slippage_bps / 10_000.0

    cash = initial_cash
    qty = 0.0
    entry_price = 0.0
    entry_i = -1
    entry_reason = ""
    active_stop = np.nan
    active_target = np.nan
    # After a stop/target exit the strategy's position series still reads
    # "long" -- it has no idea the protective order fired. Without this latch
    # the engine would re-buy on the very next bar, over and over, which badly
    # flatters any strategy with stops. Re-entry waits for a fresh signal:
    # the target position must return to flat before it can trigger again.
    awaiting_flat = False

    trades: list[Trade] = []
    equity_vals = np.empty(len(bars), dtype="float64")

    for i in range(len(bars)):
        # ---- intrabar stop / target on an already-open position -----------
        if qty > 0 and use_stops:
            hit_stop = not np.isnan(active_stop) and low[i] <= active_stop
            hit_target = not np.isnan(active_target) and high[i] >= active_target

            if hit_stop or hit_target:
                # Pessimistic: if both were touched, assume the stop went first.
                kind = "stop" if hit_stop else "target"
                raw = active_stop if hit_stop else active_target
                # A gap through the level fills at the open, not the level.
                fill = min(raw, open_[i]) if hit_stop else max(raw, open_[i])
                fill *= (1 - slip)
                proceeds = qty * fill - commission
                cash += proceeds
                pnl = proceeds - qty * entry_price
                trades.append(
                    Trade(
                        entry_ts=idx[entry_i].to_pydatetime(),
                        exit_ts=idx[i].to_pydatetime(),
                        entry_price=entry_price,
                        exit_price=fill,
                        qty=qty,
                        pnl=pnl,
                        return_pct=pnl / (qty * entry_price) if qty else 0.0,
                        bars_held=i - entry_i,
                        exit_kind=kind,
                        entry_reason=entry_reason,
                    )
                )
                qty = 0.0
                active_stop = active_target = np.nan
                awaiting_flat = True

        # ---- act on the PREVIOUS bar's signal, at THIS bar's open ---------
        if i > 0:
            want = target_pos[i - 1]

            if want == 0:
                awaiting_flat = False

            if want == 1 and qty == 0 and not awaiting_flat:
                fill = open_[i] * (1 + slip)
                affordable = (cash - commission) / fill if fill > 0 else 0.0
                buy_qty = np.floor(affordable)
                if buy_qty >= 1:
                    cash -= buy_qty * fill + commission
                    qty = buy_qty
                    entry_price = fill
                    entry_i = i
                    entry_reason = str(reasons[i - 1] or "")
                    active_stop = stop_arr[i - 1] if use_stops else np.nan
                    active_target = tgt_arr[i - 1] if use_stops else np.nan

            elif want == 0 and qty > 0:
                fill = open_[i] * (1 - slip)
                proceeds = qty * fill - commission
                cash += proceeds
                pnl = proceeds - qty * entry_price
                trades.append(
                    Trade(
                        entry_ts=idx[entry_i].to_pydatetime(),
                        exit_ts=idx[i].to_pydatetime(),
                        entry_price=entry_price,
                        exit_price=fill,
                        qty=qty,
                        pnl=pnl,
                        return_pct=pnl / (qty * entry_price) if qty else 0.0,
                        bars_held=i - entry_i,
                        exit_kind="signal",
                        entry_reason=entry_reason,
                    )
                )
                qty = 0.0
                active_stop = active_target = np.nan

        # ---- trail the stop up while long ---------------------------------
        if qty > 0 and use_stops and not np.isnan(stop_arr[i]):
            active_stop = max(active_stop, stop_arr[i]) if not np.isnan(active_stop) else stop_arr[i]

        equity_vals[i] = cash + qty * close[i]

    # ---- close anything still open at the final close ---------------------
    if qty > 0:
        fill = close[-1] * (1 - slip)
        proceeds = qty * fill - commission
        cash += proceeds
        pnl = proceeds - qty * entry_price
        trades.append(
            Trade(
                entry_ts=idx[entry_i].to_pydatetime(),
                exit_ts=idx[-1].to_pydatetime(),
                entry_price=entry_price,
                exit_price=fill,
                qty=qty,
                pnl=pnl,
                return_pct=pnl / (qty * entry_price) if qty else 0.0,
                bars_held=len(bars) - 1 - entry_i,
                exit_kind="end",
                entry_reason=entry_reason,
            )
        )
        equity_vals[-1] = cash

    equity = pd.Series(equity_vals, index=idx, name="equity")
    result.trades = trades
    result.equity = equity
    result.metrics = _metrics(equity, trades, initial_cash)
    return result


def buy_and_hold(bars: pd.DataFrame, initial_cash: float = 10_000.0) -> dict:
    """Benchmark every strategy should be embarrassed to lose to."""
    if bars.empty:
        return {}
    qty = np.floor(initial_cash / bars["open"].iloc[0])
    equity = pd.Series(
        (initial_cash - qty * bars["open"].iloc[0]) + qty * bars["close"],
        index=bars.index,
    )
    return _metrics(equity, [], initial_cash)
