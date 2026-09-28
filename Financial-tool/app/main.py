"""FastAPI application: pages and the small JSON/HTMX API behind them."""
from __future__ import annotations

from contextlib import asynccontextmanager

import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.ai.analyst import analyse_symbol
from app.ai.providers import LLMError, provider_status
from app.analysis import indicators as ind
from app.analysis.backtest import buy_and_hold, run_backtest
from app.analysis.selector import (
    current_assignment,
    evaluate_symbol as select_evaluate,
    persist_selection,
    select_for_symbols,
)
from app.analysis.strategies import get_strategy, list_strategies
from app.config import BASE_DIR, settings
from app.credentials import (
    current_state as credential_state,
    save as save_credentials,
)
from app.data import logos
from app.data.store import get_bars
from app.db import init_db, session_scope
from app.models import (
    AuditLog,
    Mode,
    Order,
    OrderProposal,
    ProposalStatus,
    Signal,
    StrategyAssignment,
    WatchItem,
)
from app.trading import engine
from app.trading.factory import broker_status, get_broker
from app.trading.risk import (
    kill_switch_active,
    live_enabled,
    set_kill_switch,
    set_live_enabled,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    from app.jobs import shutdown_scheduler, start_scheduler
    from app.notify import bot

    start_scheduler()
    if bot.start():
        log.info("telegram bot listening")
    log.info("Trading Desk ready on http://127.0.0.1:8000")
    yield
    bot.stop()
    shutdown_scheduler()


app = FastAPI(title="Trading Desk", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


# --------------------------------------------------------------------------
# Shared template context
# --------------------------------------------------------------------------
def _asset_version() -> str:
    """Mtime of the stylesheet, used to bust the browser cache on edit."""
    try:
        return str(int((BASE_DIR / "app" / "static" / "app.css").stat().st_mtime))
    except OSError:
        return "0"


def base_ctx(request: Request, active: str) -> dict:
    broker = get_broker()
    status = broker_status()
    try:
        account = broker.get_account()
    except Exception as exc:
        log.warning("could not read account: %s", exc)
        account = None
    try:
        market_open = broker.is_market_open()
    except Exception:
        market_open = False

    return {
        "request": request,
        "active": active,
        "broker": status,
        "account": account,
        "market_open": market_open,
        "pending_count": len(engine.pending_proposals()),
        "cfg": settings,
        "ai": provider_status(),
        "telegram": telegram_status(),
        "asset_v": _asset_version(),
        "palette_symbols": _palette_symbols(),
    }


def _palette_symbols() -> str:
    """Watchlist symbols as JSON, so the command palette can jump to them."""
    try:
        with session_scope() as s:
            rows = s.execute(
                select(WatchItem.symbol, WatchItem.name).order_by(WatchItem.symbol)
            ).all()
        return json.dumps([{"s": r[0], "n": r[1] or r[0]} for r in rows])
    except Exception:
        return "[]"


def telegram_status() -> dict:
    from app.notify import bot

    return {
        "configured": settings.telegram_configured,
        "running": bot.running(),
        "approvals": settings.telegram_allow_approvals,
        "live_approvals": settings.telegram_allow_live_approvals,
    }


# HTTP header values are latin-1, so a typographic dash or curly quote in a
# toast would raise on the way out and turn a working button into a 500. These
# are the characters we actually write; anything else degrades to "?" rather
# than taking the request down.
_HEADER_SAFE = str.maketrans({
    "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", " ": " ",
})


def _toast(message: str, kind: str = "ok", body: str = "") -> Response:
    safe = message.translate(_HEADER_SAFE).encode("latin-1", "replace").decode("latin-1")
    return Response(
        content=body,
        media_type="text/html",
        headers={"HX-Toast": safe, "HX-Toast-Kind": kind},
    )


def _change_pct(bars: pd.DataFrame) -> float:
    if len(bars) < 2:
        return 0.0
    return float(bars["close"].iloc[-1] / bars["close"].iloc[-2] - 1.0) * 100.0


def spark_path(values: list[float], width: int = 96, height: int = 26) -> str:
    """An SVG polyline for a row-height price chart.

    Server-rendered because these are static once drawn, and shipping a
    charting library to draw sixty pixels of line would be silly.
    """
    vals = [float(v) for v in values if v == v]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = width / (len(vals) - 1)
    pad = 2
    inner = height - pad * 2
    pts = [
        f"{i * step:.1f},{pad + inner - ((v - lo) / span) * inner:.1f}"
        for i, v in enumerate(vals)
    ]
    return " ".join(pts)


def build_brief(views: list[dict], proposals: list[dict], positions: list,
                account, kill: bool) -> dict:
    """One paragraph answering: what, if anything, needs me right now?

    The dashboard used to open with five numbers and leave the reading to you.
    Most days the honest answer is "nothing", and saying so plainly is more
    useful than a wall of green.
    """
    buys = [v for v in views if v["signal"].value == "BUY"]
    sells = [v for v in views if v["signal"].value == "SELL"]
    losers = [p for p in positions if p.unrealized_plpc < -0.05]

    if kill:
        tone, headline = "danger", "The kill switch is on — nothing can trade."
        detail = ("Work out why it tripped before clearing it in Settings. "
                  "It exists to stop a bad day becoming a worse one.")
    elif proposals:
        n = len(proposals)
        tone, headline = "action", (
            f"{n} order{'' if n == 1 else 's'} waiting for your approval."
        )
        detail = ", ".join(
            f"buy {p['qty']:.0f} {p['symbol']} (${p['notional']:,.0f}, "
            f"${p['risk_usd']:,.0f} at risk)" for p in proposals[:3]
        ) + ". Nothing is sent until you press Approve."
    elif sells:
        tone, headline = "warn", (
            f"{len(sells)} exit signal{'' if len(sells) == 1 else 's'} fired."
        )
        detail = ("Exits are not auto-executed — if you hold "
                  + ", ".join(v["symbol"] for v in sells[:4])
                  + ", the assigned strategy no longer wants to be in it.")
    elif buys:
        tone, headline = "info", (
            f"{len(buys)} entry signal{'' if len(buys) == 1 else 's'} today, "
            f"none of them actionable."
        )
        detail = (", ".join(v["symbol"] for v in buys[:4])
                  + " fired on signal-only symbols, so no order was sized. "
                  "Switch one to semi-auto in Settings if you want proposals.")
    elif losers:
        tone, headline = "warn", "Nothing needs a decision, but check your losers."
        detail = ", ".join(
            f"{p.symbol} is {p.unrealized_plpc * 100:.0f}% down" for p in losers[:3]
        ) + ". Each one has a stop attached; this is information, not an alarm."
    else:
        tone, headline = "calm", "Nothing needs you today."
        detail = (
            f"{len(views)} symbols watched, {len(positions)} position"
            f"{'' if len(positions) == 1 else 's'} open, every entry carrying a "
            f"stop. Doing nothing is the position."
        )

    return {"tone": tone, "headline": headline, "detail": detail}


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    ctx = base_ctx(request, "overview")
    broker = get_broker()

    positions = broker.get_positions() if ctx["account"] else []
    held_symbols = {p.symbol for p in positions}
    watch_items = engine.get_watchlist(enabled_only=False)

    views = []
    for w in watch_items:
        try:
            v = engine.evaluate_symbol(w)
        except Exception as exc:
            log.warning("evaluate %s failed: %s", w.symbol, exc)
            continue
        if v is None:
            continue
        end = datetime.now(timezone.utc).replace(tzinfo=None)
        bars = get_bars(w.symbol, end - timedelta(days=90), end)
        closes = bars["close"].tolist()
        month = closes[-22:] if len(closes) > 22 else closes
        views.append(
            {
                "symbol": v.symbol, "price": v.price, "signal": v.signal,
                "strategy": v.strategy, "strategy_label": v.strategy_label,
                "reason": v.reason, "note": v.note, "no_edge": v.no_edge,
                "agreement": v.agreement, "agreement_total": v.agreement_total,
                "position_now": v.position_now,
                "is_held": v.symbol in held_symbols,
                "mode": w.mode.value,
                "allocation_usd": w.allocation_usd,
                "risk_pct": w.risk_pct,
                "change_pct": _change_pct(bars[-2:]) if len(bars) >= 2 else 0.0,
                "month_pct": ((month[-1] / month[0] - 1) * 100) if len(month) > 1 else 0.0,
                "spark": spark_path(closes[-60:]),
                "spark_up": len(closes) > 1 and closes[-1] >= closes[-60:][0],
                "stop_price": v.stop_price,
                "target_price": v.target_price,
                "item_id": w.id,
            }
        )

    props = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for p in engine.pending_proposals():
        mins = max(int((p.expires_at - now).total_seconds() // 60), 0)
        props.append(
            {
                "id": p.id, "symbol": p.symbol, "qty": p.qty,
                "est_price": p.est_price, "notional": p.notional,
                "stop_price": p.stop_price, "target_price": p.target_price,
                "risk_usd": p.risk_usd, "rationale": p.rationale,
                "expires_in": f"in {mins} min" if mins else "very soon",
            }
        )

    account = ctx["account"]
    deployed = sum(abs(p.market_value) for p in positions)

    try:
        from app.analysis.scout import market_regime

        regime = market_regime()
    except Exception as exc:
        log.warning("regime read failed: %s", exc)
        regime = None

    ctx.update(
        {
            "views": views,
            "positions": positions,
            "proposals": props,
            "total_pl": sum(p.unrealized_pl for p in positions),
            "deployed_pct": (deployed / account.equity * 100) if account and account.equity else 0,
            "max_positions": settings.max_open_positions,
            "last_scan": _last_event_time("SIGNAL") or _last_event_time("SCOUT_RUN"),
            "regime": regime,
            "brief": build_brief(views, props, positions, account,
                                 ctx["broker"]["kill_switch"]),
            "equity_spark": _equity_spark(),
        }
    )
    return templates.TemplateResponse(request, "overview.html", ctx)


def _last_event_time(event: str) -> str:
    with session_scope() as s:
        ts = s.execute(
            select(AuditLog.ts).where(AuditLog.event == event)
            .order_by(AuditLog.ts.desc()).limit(1)
        ).scalar_one_or_none()
    return f"{ts:%d %b %H:%M}" if ts else ""


def _equity_spark() -> str:
    """Your own equity curve, from the daily snapshots the risk module keeps."""
    from app.models import DailyPnL

    with session_scope() as s:
        rows = list(
            s.execute(
                select(DailyPnL).order_by(DailyPnL.day.desc()).limit(60)
            ).scalars()
        )
    values = [r.last_equity for r in reversed(rows) if r.last_equity > 0]
    return spark_path(values, width=120, height=30)


@app.get("/symbol/{symbol}", response_class=HTMLResponse)
def symbol_page(request: Request, symbol: str):
    symbol = symbol.upper()
    ctx = base_ctx(request, "overview")

    watch = engine.get_watch(symbol)
    if watch is None:
        watch = WatchItem(symbol=symbol, mode=Mode.OFF, allocation_usd=0, risk_pct=1.0)

    end = datetime.now(timezone.utc).replace(tzinfo=None)
    bars = get_bars(symbol, end - timedelta(days=365 * 4), end)
    if bars.empty:
        ctx.update({"symbol": symbol, "error": "No price data available."})
        return templates.TemplateResponse(request, "symbol.html", ctx)

    view = engine.evaluate_symbol(watch)
    assignment = current_assignment(symbol)
    try:
        is_held = any(p.symbol == symbol for p in get_broker().get_positions())
    except Exception:
        is_held = False

    chart = _build_chart(symbol, bars, view)

    candidates = []
    if assignment and assignment.all_results_json:
        raw = json.loads(assignment.all_results_json)
        best = max((c["score"] for c in raw if not c["rejected"]), default=1.0) or 1.0
        for c in raw[:12]:
            c["bar_pct"] = max(0, min(100, (c["score"] / best) * 100)) if best else 0
            candidates.append(c)

    with session_scope() as s:
        recent_signals = list(
            s.execute(
                select(Signal).where(Signal.symbol == symbol)
                .order_by(Signal.bar_ts.desc()).limit(12)
            ).scalars()
        )

    ctx.update(
        {
            "symbol": symbol, "view": view, "watch": watch, "is_held": is_held,
            "assignment": assignment, "candidates": candidates,
            "recent_signals": recent_signals,
            "bars_count": len(bars),
            "last_close": float(bars["close"].iloc[-1]),
            "change_pct": _change_pct(bars),
            "chart_json": json.dumps(chart),
            "params": json.dumps(json.loads(assignment.params_json), indent=2)
            if assignment and assignment.params_json else "",
        }
    )
    return templates.TemplateResponse(request, "symbol.html", ctx)


def _build_chart(symbol: str, bars: pd.DataFrame, view) -> dict:
    """Shape bars, overlays, markers and an oscillator for Lightweight Charts."""
    def ts(i) -> str:
        return pd.Timestamp(i).strftime("%Y-%m-%d")

    candles = [
        {"time": ts(i), "open": float(r.open), "high": float(r.high),
         "low": float(r.low), "close": float(r.close)}
        for i, r in bars.iterrows()
    ]
    volume = [
        {"time": ts(i), "value": float(r.volume),
         "color": "rgba(38,208,124,.25)" if r.close >= r.open else "rgba(255,92,92,.25)"}
        for i, r in bars.iterrows()
    ]

    overlays, oscillator, markers = [], None, []

    key = view.strategy if view and view.strategy else ""
    params = {}
    assignment = current_assignment(symbol)
    if assignment and assignment.params_json and key:
        params = json.loads(assignment.params_json)

    def line(series: pd.Series, name: str) -> None:
        data = [
            {"time": ts(i), "value": float(v)}
            for i, v in series.items() if pd.notna(v)
        ]
        if data:
            overlays.append({"name": name, "data": data})

    close, high, low = bars["close"], bars["high"], bars["low"]

    if key == "ma_cross":
        f, s_ = params.get("fast", 20), params.get("slow", 50)
        line(ind.ema(close, f), f"EMA {f}")
        line(ind.ema(close, s_), f"EMA {s_}")
        adx = ind.adx(high, low, close, params.get("adx_period", 14))["adx"]
        oscillator = {
            "name": "ADX", "levels": [params.get("adx_min", 20.0)],
            "data": [{"time": ts(i), "value": float(v)} for i, v in adx.items() if pd.notna(v)],
        }
    elif key == "rsi_reversion":
        if params.get("trend_ma", 200):
            line(ind.sma(close, params["trend_ma"]), f"SMA {params['trend_ma']}")
        rsi = ind.rsi(close, params.get("rsi_period", 14))
        oscillator = {
            "name": "RSI",
            "levels": [params.get("oversold", 30.0), params.get("exit_level", 55.0)],
            "data": [{"time": ts(i), "value": float(v)} for i, v in rsi.items() if pd.notna(v)],
        }
    elif key == "macd_trend":
        if params.get("trend_ma", 100):
            line(ind.sma(close, params["trend_ma"]), f"SMA {params['trend_ma']}")
        m = ind.macd(close, params.get("fast", 12), params.get("slow", 26),
                     params.get("signal", 9))
        oscillator = {
            "name": "MACD histogram", "levels": [0],
            "data": [{"time": ts(i), "value": float(v)} for i, v in m["hist"].items() if pd.notna(v)],
        }
    elif key == "bollinger":
        bb = ind.bollinger(close, params.get("period", 20), params.get("std", 2.0))
        line(bb["upper"], "BB upper")
        line(bb["mid"], "BB mid")
        line(bb["lower"], "BB lower")
    elif key == "momentum":
        if params.get("trend_ma", 100):
            line(ind.sma(close, params["trend_ma"]), f"SMA {params['trend_ma']}")
        ret = ind.rolling_return(close, params.get("lookback", 126))
        oscillator = {
            "name": f"{params.get('lookback', 126)}-day return", "levels": [0],
            "data": [{"time": ts(i), "value": float(v) * 100}
                     for i, v in ret.items() if pd.notna(v)],
        }
    else:
        line(ind.sma(close, 50), "SMA 50")
        line(ind.sma(close, 200), "SMA 200")

    # Entry/exit markers from the assigned strategy.
    if key:
        try:
            res = get_strategy(key, **params).run(bars)
            changes = res.position.diff().fillna(res.position)
            for i, delta in changes.items():
                if delta == 1:
                    markers.append({"time": ts(i), "position": "belowBar",
                                    "color": "#26d07c", "shape": "arrowUp", "text": "BUY"})
                elif delta == -1:
                    markers.append({"time": ts(i), "position": "aboveBar",
                                    "color": "#ff5c5c", "shape": "arrowDown", "text": "SELL"})
        except Exception as exc:
            log.debug("marker generation failed for %s: %s", symbol, exc)

    return {"candles": candles, "volume": volume, "overlays": overlays,
            "oscillator": oscillator, "markers": markers}


@app.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request):
    ctx = base_ctx(request, "positions")
    broker = get_broker()
    positions = broker.get_positions() if ctx["account"] else []
    with session_scope() as s:
        orders = list(
            s.execute(select(Order).order_by(Order.submitted_at.desc()).limit(60)).scalars()
        )
    ctx.update(
        {
            "positions": positions, "orders": orders,
            "market_value": sum(p.market_value for p in positions),
            "total_pl": sum(p.unrealized_pl for p in positions),
        }
    )
    return templates.TemplateResponse(request, "positions.html", ctx)


@app.get("/strategies", response_class=HTMLResponse)
def strategies_page(request: Request):
    ctx = base_ctx(request, "strategies")
    with session_scope() as s:
        rows = list(
            s.execute(
                select(StrategyAssignment).where(StrategyAssignment.is_current.is_(True))
            ).scalars()
        )

    by_key: dict[str, list] = {}
    no_edge = []
    for r in rows:
        if r.no_edge or not r.strategy:
            no_edge.append(r.symbol)
        else:
            by_key.setdefault(r.strategy, []).append({"symbol": r.symbol, "score": r.score})

    strategies = [
        {
            "key": c.key, "label": c.label, "description": c.description,
            "grid": c.grid, "grid_json": json.dumps(c.grid, indent=2),
            "assigned_to": sorted(by_key.get(c.key, []),
                                  key=lambda a: a["score"], reverse=True),
        }
        for c in list_strategies()
    ]
    ctx.update({"strategies": strategies, "no_edge_symbols": sorted(no_edge)})
    return templates.TemplateResponse(request, "strategies.html", ctx)


@app.get("/backtest", response_class=HTMLResponse)
def backtest_page(
    request: Request, symbol: str = "", strategy: str = "ma_cross",
    years: int = 3, cash: float = 10_000.0,
):
    ctx = base_ctx(request, "backtest")
    ctx.update(
        {
            "strategies": list_strategies(), "symbol": symbol.upper(),
            "strategy_key": strategy, "years": years, "cash": cash,
            "result": None, "error": None,
        }
    )

    if symbol:
        end = datetime.now(timezone.utc).replace(tzinfo=None)
        bars = get_bars(symbol.upper(), end - timedelta(days=int(365.25 * years)), end)
        if bars.empty:
            ctx["error"] = f"No price data for {symbol.upper()}."
        else:
            try:
                res = run_backtest(get_strategy(strategy), bars, symbol.upper(),
                                   initial_cash=cash)
            except KeyError:
                ctx["error"] = f"Unknown strategy {strategy!r}."
                return templates.TemplateResponse(request, "backtest.html", ctx)

            bh_metrics = buy_and_hold(bars, cash)
            qty = int(cash // bars["open"].iloc[0])
            bh_equity = (cash - qty * bars["open"].iloc[0]) + qty * bars["close"]

            ctx.update(
                {
                    "result": res, "m": res.metrics, "bh": bh_metrics,
                    "trades": list(reversed(res.trades))[:80],
                    "equity_json": json.dumps(
                        {
                            "strategy": [
                                {"time": pd.Timestamp(i).strftime("%Y-%m-%d"), "value": float(v)}
                                for i, v in res.equity.items()
                            ],
                            "buy_hold": [
                                {"time": pd.Timestamp(i).strftime("%Y-%m-%d"), "value": float(v)}
                                for i, v in bh_equity.items()
                            ],
                        }
                    ),
                }
            )
    return templates.TemplateResponse(request, "backtest.html", ctx)


@app.get("/log", response_class=HTMLResponse)
def log_page(request: Request, event: str = ""):
    ctx = base_ctx(request, "log")
    with session_scope() as s:
        q = select(AuditLog).order_by(AuditLog.ts.desc()).limit(250)
        if event:
            q = select(AuditLog).where(AuditLog.event == event).order_by(
                AuditLog.ts.desc()).limit(250)
        entries = list(s.execute(q).scalars())
        types = sorted(
            {e for e in s.execute(select(AuditLog.event).distinct()).scalars()}
        )
    ctx.update({"entries": entries, "event_types": types, "current_event": event})
    return templates.TemplateResponse(request, "log.html", ctx)


@app.get("/scout", response_class=HTMLResponse)
def scout_page(request: Request, universe: str = "", sort: str = "score"):
    """Ideas the scout surfaced, with the evidence behind each one."""
    from app.analysis.scout import latest_ideas, market_regime
    from app.analysis.universe import UNIVERSE_LABELS, lookup

    ctx = base_ctx(request, "scout")
    ideas_raw = latest_ideas()

    watched = {w.symbol for w in engine.get_watchlist(enabled_only=False)}
    cards = []
    for row in ideas_raw:
        if universe and row.universe != universe:
            continue
        inst = lookup(row.symbol)
        spark = json.loads(row.spark_json or "[]")
        cards.append(
            {
                "row": row,
                "sector": inst.sector if inst else "",
                "reasons": json.loads(row.reasons_json or "[]"),
                "cautions": json.loads(row.cautions_json or "[]"),
                "spark": spark_path(spark, width=200, height=48),
                "spark_up": len(spark) > 1 and spark[-1] >= spark[0],
                "watched": row.symbol in watched,
                "components": [
                    ("Trend", row.trend_score),
                    ("Momentum", row.momentum_score),
                    ("Steadiness", row.quality_score),
                    ("Diversifies you", row.diversify_score),
                    ("Pullback", row.value_score),
                ],
            }
        )

    if sort == "diversify":
        cards.sort(key=lambda c: c["row"].diversify_score, reverse=True)
    elif sort == "momentum":
        cards.sort(key=lambda c: c["row"].momentum_score, reverse=True)
    elif sort == "steady":
        cards.sort(key=lambda c: c["row"].quality_score, reverse=True)

    try:
        regime = market_regime()
    except Exception as exc:
        log.warning("regime read failed: %s", exc)
        regime = None

    counts: dict[str, int] = {}
    for row in ideas_raw:
        counts[row.universe] = counts.get(row.universe, 0) + 1

    ctx.update(
        {
            "cards": cards,
            "regime": regime,
            "universe_labels": UNIVERSE_LABELS,
            "counts": counts,
            "current_universe": universe,
            "sort": sort,
            "last_run": ideas_raw[0].created_at if ideas_raw else None,
            "total": len(ideas_raw),
        }
    )
    return templates.TemplateResponse(request, "scout.html", ctx)


@app.get("/suggestions", response_class=HTMLResponse)
def suggestions_page(request: Request, tab: str = "buy"):
    """Today's live signals, ranked by the evidence behind them."""
    from app.analysis.conviction import rank

    ctx = base_ctx(request, "suggestions")
    broker = get_broker()

    try:
        buys, sells = rank(broker)
    except Exception:
        log.exception("suggestion ranking failed")
        buys, sells = [], []

    try:
        holds_anything = bool(broker.get_positions())
    except Exception:
        holds_anything = False

    ctx.update(
        {
            "tab": "sell" if tab == "sell" else "buy",
            "buys": buys,
            "sells": sells,
            "holds_anything": holds_anything,
            "scanned_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }
    )
    return templates.TemplateResponse(request, "suggestions.html", ctx)


@app.get("/dogs", response_class=HTMLResponse)
def dogs_page(request: Request, year: int = 0, all: str = ""):
    """The Dogs of the Dow list in force for a given year."""
    from app.analysis import dow

    ctx = base_ctx(request, "dogs")
    current_year = datetime.now(timezone.utc).year
    years = dow.stored_years()
    year = year or (years[0] if years else current_year)

    entries = dow.stored_list(year)
    show_all = bool(all)
    rows = entries if show_all else [e for e in entries if e.is_dog]

    perf = None
    returns: dict = {}
    if entries:
        try:
            perf = dow.performance(year, entries)
            returns = {r.symbol: r for r in perf["rows"]}
        except Exception as exc:
            log.warning("dogs performance failed for %d: %s", year, exc)

    ctx.update(
        {
            "year": year,
            "current_year": current_year,
            "years": years or [current_year],
            "entries": entries,
            "rows": rows,
            "show_all": show_all,
            "perf": perf,
            "returns": returns,
            "ranking_date": entries[0].ranking_date if entries else None,
            "source": entries[0].source if entries else "",
            "membership_as_of": dow.AS_OF,
            "warnings": [],
        }
    )
    return templates.TemplateResponse(request, "dogs.html", ctx)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    ctx = base_ctx(request, "settings")
    items = engine.get_watchlist(enabled_only=False)
    with session_scope() as s:
        rows = list(
            s.execute(
                select(StrategyAssignment).where(StrategyAssignment.is_current.is_(True))
            ).scalars()
        )
    ctx.update(
        {
            "items": items,
            "assignments": {r.symbol: r for r in rows},
            "modes": [m.value for m in Mode],
            "strategies": list_strategies(),
            "kill_switch": kill_switch_active(),
            "live_enabled": live_enabled(),
            "logo_cache": logos.cache_stats(),
            "credentials": credential_state(),
        }
    )
    return templates.TemplateResponse(request, "settings.html", ctx)


# --------------------------------------------------------------------------
# Logos
# --------------------------------------------------------------------------
@app.get("/logo/{symbol}")
def symbol_logo(symbol: str) -> Response:
    """Serve a cached instrument logo, or a locally drawn monogram tile.

    Sync on purpose: FastAPI runs it in the threadpool, so the one-off fetch
    behind a cold cache cannot block the event loop.
    """
    data, media, real = logos.logo(symbol)
    return Response(
        content=data,
        media_type=media,
        headers={
            # A real logo never changes; a monogram is cheap but re-checking it
            # every day is enough to pick up a logo that later becomes available.
            "Cache-Control": "public, max-age=%d" % (604800 if real else 86400),
        },
    )


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.post("/api/watchlist/add")
def api_watch_add(
    symbol: str = Form(...), mode: str = Form("SIGNAL_ONLY"),
    allocation_usd: float = Form(1000.0), risk_pct: float = Form(1.0),
):
    sym = symbol.upper().strip()
    if not sym:
        return _toast("Enter a symbol", "err")

    end = datetime.now(timezone.utc).replace(tzinfo=None)
    bars = get_bars(sym, end - timedelta(days=400), end)
    if bars.empty:
        return _toast(f"No market data found for {sym}", "err")

    with session_scope() as s:
        existing = s.execute(
            select(WatchItem).where(WatchItem.symbol == sym)
        ).scalar_one_or_none()
        if existing:
            return _toast(f"{sym} is already on the watchlist", "err")
        s.add(
            WatchItem(symbol=sym, name=sym, mode=Mode(mode),
                      allocation_usd=allocation_usd, risk_pct=risk_pct)
        )
    return _toast(f"Added {sym}")


@app.post("/api/watchlist/{item_id}/update")
def api_watch_update(
    item_id: int, mode: str = Form(None), allocation_usd: float = Form(None),
    risk_pct: float = Form(None), pinned_strategy: str = Form(None),
):
    with session_scope() as s:
        it = s.get(WatchItem, item_id)
        if it is None:
            return _toast("Not found", "err")
        if mode:
            it.mode = Mode(mode)
        if allocation_usd is not None:
            it.allocation_usd = allocation_usd
        if risk_pct is not None:
            it.risk_pct = risk_pct
        if pinned_strategy is not None:
            it.pinned_strategy = pinned_strategy or None
        sym = it.symbol
    return _toast(f"{sym} updated")


@app.post("/api/watchlist/{item_id}/delete")
def api_watch_delete(item_id: int):
    with session_scope() as s:
        it = s.get(WatchItem, item_id)
        if it is None:
            return _toast("Not found", "err")
        sym = it.symbol
        s.delete(it)
    return _toast(f"Removed {sym}")


@app.post("/api/scan")
def api_scan():
    broker = get_broker()
    views = engine.run_signal_scan(broker, require_market_open=False)
    return _toast(f"Scanned {len(views)} symbols")


@app.post("/api/select/{symbol}")
def api_select_one(symbol: str):
    res = select_for_symbols([symbol.upper()])
    r = res[symbol.upper()]
    return _toast(r.note[:120], "ok" if not r.no_edge else "err")


@app.post("/api/select-all")
def api_select_all():
    symbols = [w.symbol for w in engine.get_watchlist(enabled_only=False)]
    if not symbols:
        return _toast("Watchlist is empty", "err")
    res = select_for_symbols(symbols)
    n_edge = sum(1 for r in res.values() if not r.no_edge)
    return _toast(f"Evaluated {len(res)} symbols; {n_edge} have a usable strategy")


@app.post("/api/proposals/{proposal_id}/approve", response_class=HTMLResponse)
def api_approve(proposal_id: int):
    broker = get_broker()
    ok, msg = engine.approve_proposal(proposal_id, broker, require_market_open=False)
    html = (
        f'<div class="banner banner-info" style="margin:0 0 12px">'
        f'<span>{"✓" if ok else "⚠"}</span><span>{msg}</span></div>'
    )
    return Response(
        content=html, media_type="text/html",
        headers={"HX-Toast": msg, "HX-Toast-Kind": "ok" if ok else "err"},
    )


@app.post("/api/proposals/{proposal_id}/reject", response_class=HTMLResponse)
def api_reject(proposal_id: int):
    ok = engine.reject_proposal(proposal_id)
    msg = "Proposal dismissed" if ok else "Could not dismiss"
    html = (
        f'<div class="banner banner-info" style="margin:0 0 12px">'
        f'<span>·</span><span>{msg}</span></div>'
    )
    return Response(
        content=html, media_type="text/html",
        headers={"HX-Toast": msg, "HX-Toast-Kind": "ok" if ok else "err"},
    )


@app.post("/api/positions/{symbol}/close")
def api_close(symbol: str):
    broker = get_broker()
    try:
        broker.close_position(symbol.upper())
    except Exception as exc:
        return _toast(str(exc), "err")
    return _toast(f"Closing {symbol.upper()}")


@app.post("/api/kill-switch")
def api_kill_switch():
    now = not kill_switch_active()
    set_kill_switch(now, "toggled from the dashboard")
    return _toast("Kill switch ON - orders blocked" if now else "Kill switch cleared")


@app.post("/api/live-toggle")
def api_live_toggle():
    if not settings.alpaca_live:
        return _toast("ALPACA_LIVE is not enabled in .env", "err")
    now = not live_enabled()
    set_live_enabled(now)
    get_broker(force_reload=True)
    return _toast("LIVE TRADING ARMED" if now else "Live trading disarmed",
                  "err" if now else "ok")


@app.post("/api/sim/reset")
def api_sim_reset():
    broker = get_broker()
    if broker.name != "simulator":
        return _toast("Not running the simulator", "err")
    broker.reset()
    return _toast("Simulated account reset to $100,000")


@app.post("/api/logos/refresh")
def api_logos_refresh():
    """Drop every cached logo and fetch the watchlist's again.

    Worth doing after a company rebrands, or once when a provider that was
    unreachable (offline, blocked) comes back -- misses are cached for a week
    otherwise.
    """
    if not settings.logos_enabled:
        return _toast("Logos are switched off (LOGOS_ENABLED=false)", "err")
    logos.clear_cache()
    symbols = [w.symbol for w in engine.get_watchlist(enabled_only=False)]
    found = logos.prefetch(symbols)
    missing = len(symbols) - found
    msg = f"Fetched {found} logo{'' if found == 1 else 's'}"
    if missing:
        msg += f" · {missing} fell back to a monogram"
    return _toast(msg, "ok" if found else "err")


@app.post("/api/analyse/{symbol}", response_class=HTMLResponse)
def api_analyse(symbol: str, question: str = Form("")):
    """Ask the configured LLM to comment on one symbol.

    Commentary only: this endpoint cannot create a proposal or place an order,
    and nothing downstream reads its output.
    """
    symbol = symbol.upper()
    watch = engine.get_watch(symbol) or WatchItem(
        symbol=symbol, mode=Mode.OFF, allocation_usd=0, risk_pct=1.0
    )
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    bars = get_bars(symbol, end - timedelta(days=365 * 3), end)
    if bars.empty:
        return HTMLResponse(_ai_error(f"No price data for {symbol}."))

    view = engine.evaluate_symbol(watch)
    if view is None:
        return HTMLResponse(_ai_error(f"Could not evaluate {symbol}."))

    try:
        result = analyse_symbol(symbol, bars, view, current_assignment(symbol),
                                question=question)
    except LLMError as exc:
        return HTMLResponse(_ai_error(str(exc)))
    except Exception as exc:
        log.exception("analyst failed for %s", symbol)
        return HTMLResponse(_ai_error(f"Analyst failed: {exc}"))

    body = _markdownish(result.text)
    return HTMLResponse(
        f'''<div class="ai-reply">
             <div class="ai-meta">
               <span class="badge badge-accent">{result.provider} &middot; {result.model}</span>
               <span class="dim">generated {result.generated_at:%H:%M}</span>
             </div>
             <div class="ai-body">{body}</div>
             <details class="raw"><summary>Exact data the model was given</summary>
               <pre>{_esc(result.briefing)}</pre></details>
           </div>'''
    )


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _ai_error(msg: str) -> str:
    return (f'<div class="banner banner-warn" style="margin:0">'
            f'<span>⚠</span><span>{_esc(msg)}</span></div>')


def _markdownish(text: str) -> str:
    """Minimal, safe rendering: escape everything, then allow **bold**, lists.

    Deliberately not a full markdown parser -- this is untrusted model output
    going straight into the page, so it is escaped first and only a tiny,
    known-safe set of patterns is re-introduced.
    """
    import re

    bullet = r"^\s*([-*•]|\d+\.)\s+"
    out = []
    for block in _esc(text).split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if lines and all(re.match(bullet, ln) for ln in lines):
            items = "".join(f"<li>{re.sub(bullet, '', ln)}</li>" for ln in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append(f"<p>{'<br>'.join(lines)}</p>")
    html = "".join(out)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", html)


# --------------------------------------------------------------------------
# Scout API
# --------------------------------------------------------------------------
@app.post("/api/scout/run")
def api_scout_run(universe: str = Form("")):
    """Screen the universe now. Takes a while on a cold cache."""
    from app.analysis.scout import run_scout

    keys = [universe] if universe else None
    try:
        run_id, ideas = run_scout(universe_keys=keys)
    except Exception as exc:
        log.exception("scout run failed")
        return _toast(f"Scout failed: {exc}", "err")

    if not ideas:
        return _toast("Nothing cleared the bar — that is a real answer", "ok")
    return _toast(f"{len(ideas)} ideas found; top pick {ideas[0].symbol}")


# --------------------------------------------------------------------------
# Dogs of the Dow
# --------------------------------------------------------------------------
@app.post("/api/dogs/rebuild")
def api_dogs_rebuild(year: int = Form(0)):
    """Rebuild one year's list, replacing whatever is stored for it."""
    from app.analysis import dow

    target = year or datetime.now(timezone.utc).year
    try:
        result = dow.refresh(target, force=True)
    except dow.MembershipUnknown as exc:
        return _toast(str(exc), "err")
    except Exception as exc:
        log.exception("dogs rebuild failed")
        return _toast(f"Rebuild failed: {exc}", "err")

    if result is None or not result.dogs:
        return _toast(f"No {target} list could be built", "err")
    return _toast(
        f"{target} list rebuilt — top yielder {result.dogs[0].symbol} "
        f"at {result.dogs[0].dividend_yield * 100:.2f}%"
    )


@app.post("/api/scout/{symbol}/watch")
def api_scout_watch(symbol: str, mode: str = Form("SIGNAL_ONLY"),
                    allocation_usd: float = Form(1000.0)):
    """Promote an idea to the watchlist. Signal-only by default, deliberately.

    An instrument that has never been through strategy selection has no
    demonstrated edge on this app's own terms, so it starts out only able to
    talk, not to trade.
    """
    sym = symbol.upper().strip()
    with session_scope() as s:
        exists = s.execute(
            select(WatchItem).where(WatchItem.symbol == sym)
        ).scalar_one_or_none()
        if exists:
            return _toast(f"{sym} is already watched", "err")
        s.add(WatchItem(symbol=sym, name=sym, mode=Mode(mode),
                        allocation_usd=allocation_usd, risk_pct=settings.default_risk_pct))
    return _toast(f"{sym} added — run strategy selection to test it for edge")


@app.post("/api/scout/{symbol}/edge-test", response_class=HTMLResponse)
def api_scout_edge_test(symbol: str):
    """Walk-forward one candidate before you commit to watching it."""
    sym = symbol.upper().strip()
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    bars = get_bars(sym, end - timedelta(days=365 * settings.selector_lookback_years), end)
    if bars.empty:
        return HTMLResponse('<div class="edge-result err">No price data.</div>')

    try:
        res = select_evaluate(sym, bars)
    except Exception as exc:
        log.exception("edge test failed for %s", sym)
        return HTMLResponse(f'<div class="edge-result err">{_esc(str(exc))}</div>')

    if res.no_edge or res.winner is None:
        return HTMLResponse(
            f'<div class="edge-result err"><strong>No edge.</strong> '
            f'{_esc(res.note)}<br><span class="dim">Nothing beat the bar '
            f'out-of-sample. Owning it may still be sensible — but this app '
            f'has no rule it can trade on.</span></div>'
        )

    w = res.winner
    m = w.oos_metrics
    return HTMLResponse(
        f'<div class="edge-result ok"><strong>{_esc(w.label)}</strong> '
        f'scored {w.score:.2f} out-of-sample across {w.folds} folds.<br>'
        f'<span class="dim">Sharpe {m.get("sharpe", 0):.2f} · max drawdown '
        f'{abs(m.get("max_drawdown", 0)) * 100:.0f}% · {w.n_trades} trades. '
        f'Add it to the watchlist, then run selection to assign it.</span></div>'
    )


# --------------------------------------------------------------------------
# Manual orders
# --------------------------------------------------------------------------
@app.post("/api/symbol/{symbol}/propose")
def api_propose_manual(symbol: str):
    """Queue a buy you decided on, sized and risk-checked like any other."""
    prop, message = engine.propose_manual(symbol, get_broker(),
                                          require_market_open=False)
    return _toast(message, "ok" if prop else "err")


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
@app.post("/api/credentials", response_class=HTMLResponse)
async def api_credentials(request: Request):
    """Save credentials typed into Settings to .env, and apply them now.

    Reads the raw form rather than declaring each field, so adding a key to
    `credentials.FIELDS` is the only edit a new credential needs.
    """
    form = await request.form()
    clears = {
        k[len("clear_"):] for k, v in form.items()
        if k.startswith("clear_") and v
    }
    values = {k: str(v) for k, v in form.items() if not k.startswith("clear_")}

    try:
        changed, warnings = save_credentials(values, clears)
    except OSError as exc:
        log.warning("could not write .env: %s", exc)
        return _toast(f"Could not write .env — {exc}", "err")

    if not changed:
        return _toast("Nothing to save — fill a field, or tick Clear", "err")
    if warnings:
        return _toast(warnings[0], "err")
    return _toast("Saved to .env: " + ", ".join(changed))


@app.post("/api/telegram/test")
def api_telegram_test():
    from app.notify import telegram as tg
    from app.notify.events import send_now

    ok, message = tg.check()
    if not ok:
        return _toast(message, "err")
    sent = send_now(
        "✅ <b>Trading Desk is connected.</b>\n\n"
        "You will get the morning brief, every signal, and any order that "
        "needs approving. Send /help to see what I answer to."
    )
    if sent is None:
        return _toast("Token works, but the message did not send — check the chat id", "err")
    return _toast(message)


@app.post("/api/telegram/whoami")
def api_telegram_whoami():
    """Read the chat id off the last message sent to the bot."""
    from app.notify import telegram as tg

    chat_id = tg.discover_chat_id()
    if not chat_id:
        return _toast("Send your bot any message first, then press this again", "err")
    return _toast(f"Your chat id is {chat_id} — put it in .env as TELEGRAM_CHAT_ID")


@app.post("/api/telegram/digest")
def api_telegram_digest():
    from app.notify.events import build_digest, send_now

    if not settings.telegram_configured:
        return _toast("Telegram is not configured", "err")
    send_now(build_digest())
    return _toast("Brief sent to Telegram")


@app.get("/healthz")
def healthz():
    return {"ok": True, "broker": broker_status()}
