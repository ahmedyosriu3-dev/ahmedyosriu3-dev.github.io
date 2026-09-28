"""The AI analyst.

Builds a factual, numeric briefing about one symbol from data the app has
already computed, hands it to whichever LLM you configured, and returns prose.

Two rules shape everything here:

  1. **The model never sees a decision to make.** It is asked to interpret and
     stress-test a reading the deterministic engine already produced. It cannot
     place, size, or approve anything -- the order path does not import this
     module.
  2. **The model is given numbers, not asked to recall them.** Everything in
     the briefing is computed locally, so the model has no reason to invent a
     price or an indicator value. Anything it still gets wrong is visible
     against the numbers printed right next to it in the UI.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.ai.providers import LLMError, LLMProvider, get_provider
from app.analysis import indicators as ind
from app.analysis.backtest import buy_and_hold, run_backtest
from app.analysis.strategies import get_strategy

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a markets analyst embedded in a personal trading dashboard. The user is
the sole operator of this tool and trades their own money.

You will be given a factual briefing about one stock: recent price action,
indicator values, the strategy the system selected for it, that strategy's
out-of-sample backtest results, and the current signal. Every number is
computed by the system -- treat them as ground truth and never invent others.

Your job:
  - Explain what the current setup actually means in plain language.
  - Point out what the data does NOT support, and where the reading is fragile.
  - Name the specific things that would invalidate this setup.
  - Flag when the backtest evidence is thin (few trades, short window, wide
    spread between folds) rather than treating it as settled.

Constraints:
  - Do not give a buy/sell/hold recommendation and do not tell the user what to
    do with their money. Interpret the evidence; the decision is theirs.
  - Do not predict prices or targets.
  - Be concise: about 200-300 words, short paragraphs or tight bullets.
  - If the evidence is weak, say so directly. Do not manufacture confidence.
  - No preamble, no sign-off, no disclaimers about being an AI.
"""


@dataclass
class Analysis:
    text: str
    provider: str
    model: str
    briefing: str
    generated_at: datetime


def build_briefing(symbol: str, bars: pd.DataFrame, view, assignment) -> str:
    """Assemble the factual context block handed to the model."""
    close, high, low = bars["close"], bars["high"], bars["low"]
    last = float(close.iloc[-1])

    def pct(days: int) -> str:
        if len(close) <= days:
            return "n/a"
        return f"{(last / float(close.iloc[-days - 1]) - 1) * 100:+.1f}%"

    rsi = ind.rsi(close, 14)
    macd = ind.macd(close)
    bb = ind.bollinger(close, 20, 2.0)
    atr = ind.atr(high, low, close, 14)
    adx = ind.adx(high, low, close, 14)
    sma50, sma200 = ind.sma(close, 50), ind.sma(close, 200)

    def val(s: pd.Series, fmt: str = "{:.2f}") -> str:
        v = s.iloc[-1]
        return "n/a" if pd.isna(v) else fmt.format(float(v))

    lines = [
        f"SYMBOL: {symbol}",
        f"As of: {bars.index[-1]:%Y-%m-%d} ({len(bars)} daily bars available)",
        "",
        "PRICE",
        f"  Last close: ${last:,.2f}",
        f"  Change 1d / 5d / 1m / 3m / 6m / 1y: "
        f"{pct(1)} / {pct(5)} / {pct(21)} / {pct(63)} / {pct(126)} / {pct(252)}",
        f"  52-week range: ${float(close.tail(252).min()):,.2f} - "
        f"${float(close.tail(252).max()):,.2f}",
        "",
        "INDICATORS (latest)",
        f"  RSI(14): {val(rsi)}",
        f"  MACD line / signal / histogram: {val(macd['macd'])} / "
        f"{val(macd['signal'])} / {val(macd['hist'])}",
        f"  Bollinger(20,2): lower ${val(bb['lower'])}, mid ${val(bb['mid'])}, "
        f"upper ${val(bb['upper'])}, %B {val(bb['pct_b'])}",
        f"  ATR(14): ${val(atr)} ({float(atr.iloc[-1]) / last * 100:.1f}% of price)"
        if not pd.isna(atr.iloc[-1]) else "  ATR(14): n/a",
        f"  ADX(14): {val(adx['adx'])} (trend strength; under 20 means no trend)",
        f"  SMA50: ${val(sma50)}   SMA200: ${val(sma200)}",
        f"  Price vs SMA200: "
        + ("above" if not pd.isna(sma200.iloc[-1]) and last > float(sma200.iloc[-1]) else "below"),
        "",
        "SYSTEM READING",
        f"  Assigned strategy: {view.strategy_label or 'none'}",
        f"  Current signal: {view.signal.value}",
        f"  Strategy position: {'long' if view.position_now else 'flat'}",
        f"  Stated reason: {view.reason}",
        f"  Suggested stop: "
        + (f"${view.stop_price:,.2f}" if view.stop_price else "n/a"),
        f"  Strategy agreement: {view.agreement} of {view.agreement_total} "
        "strategies would be long right now",
    ]

    if assignment and assignment.strategy and not assignment.no_edge:
        import json

        m = json.loads(assignment.metrics_json or "{}")
        lines += [
            "",
            "WALK-FORWARD BACKTEST OF THE ASSIGNED STRATEGY (out-of-sample only)",
            f"  Composite score: {assignment.score:.2f}",
            f"  Mean OOS return per fold: {m.get('total_return', 0) * 100:+.1f}%",
            f"  Sharpe: {m.get('sharpe', 0):.2f}",
            f"  Max drawdown: {m.get('max_drawdown', 0) * 100:.1f}%",
            f"  Win rate: {m.get('win_rate', 0) * 100:.0f}%",
            f"  Out-of-sample trades: {m.get('n_trades', 0)}",
            f"  Last evaluated: {assignment.evaluated_at:%Y-%m-%d}",
        ]
    elif assignment and assignment.no_edge:
        lines += [
            "",
            "BACKTEST: No strategy cleared the minimum bar for this symbol. The "
            "system marked it NO EDGE and it generates no signals.",
        ]

    # A full-window backtest plus the benchmark, so the model can see whether
    # all this machinery actually beat doing nothing.
    if view.strategy:
        try:
            import json

            params = json.loads(assignment.params_json) if assignment else {}
            res = run_backtest(get_strategy(view.strategy, **params), bars, symbol)
            bh = buy_and_hold(bars)
            lines += [
                "",
                "FULL-WINDOW BACKTEST vs BUY-AND-HOLD (same period, same cash)",
                f"  Strategy: {res.metrics['total_return'] * 100:+.1f}% return, "
                f"Sharpe {res.metrics['sharpe']:.2f}, "
                f"max DD {res.metrics['max_drawdown'] * 100:.1f}%, "
                f"{res.metrics['n_trades']} trades",
                f"  Buy & hold: {bh['total_return'] * 100:+.1f}% return, "
                f"Sharpe {bh['sharpe']:.2f}, max DD {bh['max_drawdown'] * 100:.1f}%",
            ]
        except Exception as exc:
            log.debug("briefing backtest failed for %s: %s", symbol, exc)

    return "\n".join(lines)


def analyse_symbol(
    symbol: str, bars: pd.DataFrame, view, assignment,
    question: str = "", provider: LLMProvider | None = None,
) -> Analysis:
    """Run the analyst over one symbol. Raises LLMError when unavailable."""
    prov = provider or get_provider()
    if prov is None:
        raise LLMError(
            "AI analyst is not configured. Set AI_PROVIDER=ollama or "
            "AI_PROVIDER=gemini in your .env file."
        )

    ok, status = prov.available()
    if not ok:
        raise LLMError(status)

    briefing = build_briefing(symbol, bars, view, assignment)
    user = briefing
    if question.strip():
        user += (
            "\n\n---\nThe operator asks specifically:\n"
            f"{question.strip()}\n\n"
            "Answer that question using only the data above."
        )

    reply = prov.complete(SYSTEM_PROMPT, user)
    return Analysis(
        text=reply.text, provider=reply.provider, model=reply.model,
        briefing=briefing,
        generated_at=datetime.now(timezone.utc),
    )
