"""Walk-forward strategy selection.

Answers "which strategy should trade this symbol?" without fooling itself.

The trap this module exists to avoid: run every strategy over all of history,
pick the highest return, and declare victory. That picks whatever overfits best
and its live performance will be nothing like the backtest.

Instead:

  1. Split the lookback window into sequential folds.
  2. For each fold, tune parameters on the in-sample part only, then score the
     chosen configuration on the *next*, unseen out-of-sample part.
  3. Aggregate only the out-of-sample results. That is the score.
  4. Require a minimum number of trades, or the result is noise.
  5. If nothing clears the bar, return NO_EDGE -- the symbol trades nothing.

A NO_EDGE verdict is a feature. Most strategies do not beat buy-and-hold on
most symbols, and the honest answer is usually "don't trade this one".
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.analysis.backtest import buy_and_hold, run_backtest
from app.analysis.strategies import get_strategy, list_strategies
from app.config import settings
from app.db import session_scope
from app.models import StrategyAssignment

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    """One strategy+params configuration and how it did out-of-sample."""

    strategy: str
    label: str
    params: dict
    score: float
    oos_metrics: dict
    folds: int
    n_trades: int
    rejected: str = ""          # why it was disqualified, if it was

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "label": self.label,
            "params": self.params,
            "score": round(self.score, 4),
            "metrics": {k: round(v, 4) if isinstance(v, float) else v
                        for k, v in self.oos_metrics.items()},
            "folds": self.folds,
            "n_trades": self.n_trades,
            "rejected": self.rejected,
        }


@dataclass
class SelectionResult:
    symbol: str
    winner: Candidate | None
    candidates: list[Candidate] = field(default_factory=list)
    benchmark: dict = field(default_factory=dict)
    no_edge: bool = False
    note: str = ""
    evaluated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


def score_metrics(m: dict) -> float:
    """Collapse a metrics dict into one risk-adjusted number.

    Sharpe is the backbone; drawdown is penalised explicitly because a strategy
    you cannot psychologically hold is a strategy you will not hold. Profit
    factor breaks ties.
    """
    if not m or m.get("n_trades", 0) == 0:
        return -99.0

    sharpe = m.get("sharpe", 0.0)
    max_dd = abs(m.get("max_drawdown", 0.0))
    pf = min(m.get("profit_factor", 0.0), 5.0)

    dd_penalty = max_dd * 2.0
    return float(sharpe - dd_penalty + 0.15 * pf)


def _fold_bounds(n: int, folds: int, min_train: int) -> list[tuple[int, int, int]]:
    """Expanding-window folds: (train_start, train_end, test_end) index triples."""
    if n < min_train + folds * 20:
        return []
    test_size = (n - min_train) // folds
    if test_size < 20:
        return []
    out = []
    for k in range(folds):
        train_end = min_train + k * test_size
        test_end = min(train_end + test_size, n)
        if test_end - train_end >= 20:
            out.append((0, train_end, test_end))
    return out


def evaluate_symbol(
    symbol: str,
    bars: pd.DataFrame,
    folds: int | None = None,
    min_trades: int | None = None,
    initial_cash: float = 10_000.0,
) -> SelectionResult:
    """Walk-forward every strategy/param combination over `bars`."""
    folds = folds or settings.selector_folds
    min_trades = min_trades or settings.selector_min_trades

    if bars.empty or len(bars) < 250:
        return SelectionResult(
            symbol=symbol, winner=None, no_edge=True,
            note=f"Not enough history ({len(bars)} bars); need at least 250.",
        )

    benchmark = buy_and_hold(bars, initial_cash)
    candidates: list[Candidate] = []

    for cls in list_strategies():
        for overrides in cls.grid:
            strat = get_strategy(cls.key, **overrides)
            min_train = max(strat.min_bars + 40, 200)
            bounds = _fold_bounds(len(bars), folds, min_train)

            if not bounds:
                candidates.append(
                    Candidate(
                        strategy=cls.key, label=cls.label, params=strat.params_dict(),
                        score=-99.0, oos_metrics={}, folds=0, n_trades=0,
                        rejected="not enough history for walk-forward folds",
                    )
                )
                continue

            # Score each out-of-sample slice. The strategy sees the full
            # leading window so its indicators are warmed up, but only the
            # unseen tail counts toward the score.
            fold_scores: list[float] = []
            oos_trades = 0
            agg: dict[str, list[float]] = {}

            for _, train_end, test_end in bounds:
                window = bars.iloc[:test_end]
                res = run_backtest(strat, window, symbol, initial_cash=initial_cash)
                if not res.trades and not len(res.equity):
                    continue

                # Restrict to the out-of-sample portion.
                oos_equity = res.equity.iloc[train_end:]
                oos = [t for t in res.trades if t.entry_ts >= window.index[train_end]]
                if len(oos_equity) < 2:
                    continue

                from app.analysis.backtest import _metrics

                m = _metrics(oos_equity, oos, float(oos_equity.iloc[0]))
                fold_scores.append(score_metrics(m))
                oos_trades += len(oos)
                for k, v in m.items():
                    if isinstance(v, (int, float)):
                        agg.setdefault(k, []).append(float(v))

            if not fold_scores:
                candidates.append(
                    Candidate(
                        strategy=cls.key, label=cls.label, params=strat.params_dict(),
                        score=-99.0, oos_metrics={}, folds=0, n_trades=0,
                        rejected="produced no out-of-sample result",
                    )
                )
                continue

            mean_metrics = {k: float(np.mean(v)) for k, v in agg.items()}
            mean_metrics["n_trades"] = oos_trades
            # Average across folds, then dock configurations whose results swing
            # wildly from fold to fold -- consistency is worth more than a
            # single lucky window.
            mean_score = float(np.mean(fold_scores))
            consistency = float(np.std(fold_scores))
            score = mean_score - 0.25 * consistency

            rejected = ""
            if oos_trades < min_trades:
                rejected = f"only {oos_trades} out-of-sample trades (need {min_trades})"

            candidates.append(
                Candidate(
                    strategy=cls.key, label=cls.label, params=strat.params_dict(),
                    score=score, oos_metrics=mean_metrics,
                    folds=len(fold_scores), n_trades=oos_trades, rejected=rejected,
                )
            )

    candidates.sort(key=lambda c: c.score, reverse=True)
    eligible = [c for c in candidates if not c.rejected and c.score > 0]

    if not eligible:
        why = "no strategy produced a positive risk-adjusted out-of-sample score"
        if candidates and all(c.rejected for c in candidates):
            why = "no strategy traded often enough out-of-sample to judge"
        return SelectionResult(
            symbol=symbol, winner=None, candidates=candidates,
            benchmark=benchmark, no_edge=True,
            note=f"NO EDGE - {why}. This symbol will not generate signals.",
        )

    winner = eligible[0]
    return SelectionResult(
        symbol=symbol, winner=winner, candidates=candidates,
        benchmark=benchmark, no_edge=False,
        note=(
            f"{winner.label} won with an out-of-sample score of {winner.score:.2f} "
            f"across {winner.folds} folds and {winner.n_trades} trades."
        ),
    )


def persist_selection(result: SelectionResult) -> None:
    """Store the verdict, retiring whatever was current for this symbol."""
    with session_scope() as s:
        previous = (
            s.query(StrategyAssignment)
            .filter(StrategyAssignment.symbol == result.symbol,
                    StrategyAssignment.is_current.is_(True))
            .all()
        )
        for row in previous:
            row.is_current = False

        w = result.winner
        s.add(
            StrategyAssignment(
                symbol=result.symbol,
                strategy=w.strategy if w else None,
                params_json=json.dumps(w.params if w else {}),
                score=w.score if w else 0.0,
                metrics_json=json.dumps(w.oos_metrics if w else {}),
                all_results_json=json.dumps([c.to_dict() for c in result.candidates[:20]]),
                no_edge=result.no_edge,
                is_current=True,
                evaluated_at=result.evaluated_at.replace(tzinfo=None),
            )
        )


def current_assignment(symbol: str) -> StrategyAssignment | None:
    with session_scope() as s:
        return (
            s.query(StrategyAssignment)
            .filter(StrategyAssignment.symbol == symbol,
                    StrategyAssignment.is_current.is_(True))
            .order_by(StrategyAssignment.evaluated_at.desc())
            .first()
        )


def select_for_symbols(
    symbols: list[str], lookback_years: int | None = None, persist: bool = True
) -> dict[str, SelectionResult]:
    """Run selection across a list of symbols."""
    from app.data.store import get_bars

    years = lookback_years or settings.selector_lookback_years
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    # Pull extra history beyond the scoring window so indicators warm up.
    start = end - timedelta(days=int(365.25 * years) + 300)

    out: dict[str, SelectionResult] = {}
    for sym in symbols:
        try:
            bars = get_bars(sym, start, end)
            res = evaluate_symbol(sym, bars)
        except Exception as exc:
            log.exception("selection failed for %s", sym)
            res = SelectionResult(symbol=sym, winner=None, no_edge=True,
                                  note=f"Selection failed: {exc}")
        out[sym] = res
        if persist:
            persist_selection(res)
        log.info("%s -> %s", sym, res.note)
    return out
