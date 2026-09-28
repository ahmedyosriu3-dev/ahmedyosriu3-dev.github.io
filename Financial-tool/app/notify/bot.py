"""The Telegram side of the desk: long-polls for taps and commands.

Runs as a daemon thread rather than a webhook, because a webhook would need
this machine to be reachable from the internet, and it should not be.

Security model, such as it is:

  * Every update is checked against `TELEGRAM_CHAT_ID`. Messages from any other
    chat are dropped and logged -- a bot token is a bearer credential, so
    anyone who learns it can message the bot, but they cannot make it act.
  * Approvals only reach the broker when `TELEGRAM_ALLOW_APPROVALS` is on.
  * Live approvals need `TELEGRAM_ALLOW_LIVE_APPROVALS` *and* a second tap.
  * Approval runs the identical `engine.approve_proposal()` path as the
    dashboard button, so every risk check still applies. There is no shortcut
    from a phone to a broker.

If the bot token leaks, treat it like a leaked API key: revoke it in BotFather.
"""
from __future__ import annotations

import logging
import threading
import time

from app.config import settings
from app.notify import events, telegram as tg
from app.notify.telegram import Button, esc

log = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop = threading.Event()

HELP = """<b>Trading Desk</b> — what I answer to

/status — equity, exposure, kill switch
/brief — the full morning brief
/pending — orders waiting for your approval
/positions — your open book
/scan — re-run signals across the watchlist now
/scout — hunt for new ideas (slow, ~1 min)
/ideas — the last scout report
/watch SYM — add a symbol to the watchlist
/sym SYM — where one symbol stands
/kill — trip the kill switch (blocks all new orders)
/unkill — clear it

Approve buttons appear under each proposal when
approvals are enabled in Settings."""


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------
def _authorised(chat_id) -> bool:
    return str(chat_id) == str(settings.telegram_chat_id)


def _ack(cb_id: str, text: str = "", alert: bool = False) -> None:
    """Answer a button tap, if there was one."""
    if cb_id:
        tg.answer_callback(cb_id, text, alert=alert)


# --------------------------------------------------------------------------
# Button taps
# --------------------------------------------------------------------------
def _handle_callback(cb: dict) -> None:
    cb_id = cb.get("id", "")
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    if not _authorised(chat_id):
        log.warning("dropped callback from unauthorised chat %s", chat_id)
        tg.answer_callback(cb_id, "Not authorised.", alert=True)
        return

    action, _, arg = data.partition(":")

    try:
        if action == "ap":
            _approve(cb_id, message_id, arg, live=False)
        elif action == "apc":
            _arm_live(cb_id, message_id, arg)
        elif action == "apl":
            _approve(cb_id, message_id, arg, live=True)
        elif action == "rj":
            _reject(cb_id, message_id, arg)
        elif action == "why":
            _why(cb_id, arg)
        elif action == "add":
            _add_symbol(cb_id, arg)
        elif action == "cmd" and arg == "scan":
            tg.answer_callback(cb_id, "Scanning…")
            _cmd_scan()
        elif action == "cmd" and arg == "scout":
            tg.answer_callback(cb_id, "Hunting for ideas — this takes a minute.")
            _cmd_scout()
        elif action == "cancel":
            tg.answer_callback(cb_id, "Cancelled.")
            events.retire_proposal_message(int(arg), "Cancelled — nothing was sent.")
        else:
            tg.answer_callback(cb_id, "")
    except Exception as exc:
        log.exception("callback %s failed", data)
        tg.answer_callback(cb_id, f"Failed: {exc}"[:190], alert=True)


def _arm_live(cb_id: str, message_id: int, arg: str) -> None:
    """First tap on a live order: arm the confirm, do not submit."""
    pid = int(arg)
    tg.answer_callback(cb_id, "Real money. Confirm below.", alert=True)
    tg.edit(
        message_id,
        f"⚠️ <b>Confirm a LIVE order</b>\n\n"
        f"This spends real money. Press <b>Yes, submit</b> to send it to the "
        f"broker, or Cancel.\n\n<i>Proposal #{pid}</i>",
        buttons=[[Button("🔴 Yes, submit", f"apl:{pid}"),
                  Button("Cancel", f"cancel:{pid}")]],
    )


def _approve(cb_id: str, message_id: int, arg: str, live: bool) -> None:
    from app.trading import engine
    from app.trading.factory import get_broker
    from app.trading.risk import live_trading_permitted

    pid = int(arg)
    allowed, why = events.approvals_allowed()
    if not allowed:
        tg.answer_callback(cb_id, why, alert=True)
        return

    # Re-check at tap time: the live flag may have been armed since the
    # message was sent, which would turn a paper button into a live one.
    if live_trading_permitted() and not live:
        tg.answer_callback(
            cb_id, "Live trading was armed since this was sent. Use the "
                   "dashboard.", alert=True)
        return

    tg.answer_callback(cb_id, "Submitting…")
    ok, message = engine.approve_proposal(
        pid, get_broker(), require_market_open=False
    )
    icon = "✅" if ok else "⚠️"
    tg.edit(message_id, f"{icon} {esc(message)}", buttons=None)


def _reject(cb_id: str, message_id: int, arg: str) -> None:
    from app.trading import engine

    pid = int(arg)
    ok = engine.reject_proposal(pid, "dismissed from Telegram")
    tg.answer_callback(cb_id, "Dismissed." if ok else "Already decided.")
    tg.edit(
        message_id,
        "✖ <b>Dismissed.</b> Nothing was sent to the broker."
        if ok else "This proposal was already decided.",
        buttons=None,
    )


def _why(cb_id: str, arg: str) -> None:
    """The evidence behind a proposal, on demand rather than in every alert."""
    from app.analysis.selector import current_assignment
    from app.db import session_scope
    from app.models import OrderProposal
    from app.trading import engine

    tg.answer_callback(cb_id, "")
    pid = int(arg)
    with session_scope() as s:
        p = s.get(OrderProposal, pid)
        if p is None:
            events.send_now("That proposal no longer exists.")
            return
        symbol, rationale = p.symbol, p.rationale

    assignment = current_assignment(symbol)
    watch = engine.get_watch(symbol)
    lines = [f"📊 <b>Why {esc(symbol)}</b>", "", f"<i>{esc(rationale)}</i>", ""]

    if assignment and assignment.strategy:
        import json

        m = json.loads(assignment.metrics_json or "{}")
        lines += [
            f"<b>Strategy</b> {esc(assignment.strategy)} "
            f"(score {assignment.score:.2f})",
            f"Out-of-sample: Sharpe {m.get('sharpe', 0):.2f} · "
            f"max DD {abs(m.get('max_drawdown', 0)) * 100:.0f}% · "
            f"{m.get('n_trades', 0)} trades",
            "",
            "<i>Those are walk-forward numbers on data the parameters were "
            "not fitted to. They are evidence, not a forecast.</i>",
        ]
    if watch:
        lines.append(f"\nAllocation cap {events.money(watch.allocation_usd, 0)} "
                     f"· {watch.risk_pct:.1f}% risked per trade")
    events.send_now("\n".join(lines))


def _add_symbol(cb_id: str, symbol: str) -> None:
    """Add a symbol to the watchlist.

    Reached both from a scout-report button and from `/watch SYM`, so the
    callback acknowledgement is optional -- a typed command has no button
    spinner to stop.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.data.store import get_bars
    from app.db import session_scope
    from app.models import Mode, WatchItem

    symbol = symbol.upper().strip()
    with session_scope() as s:
        exists = s.execute(
            select(WatchItem).where(WatchItem.symbol == symbol)
        ).scalar_one_or_none()
        if exists:
            _ack(cb_id, f"{symbol} is already watched.")
            return

    end = datetime.now(timezone.utc).replace(tzinfo=None)
    if get_bars(symbol, end - timedelta(days=400), end).empty:
        _ack(cb_id, f"No market data for {symbol}.", alert=True)
        return

    with session_scope() as s:
        s.add(WatchItem(symbol=symbol, name=symbol, mode=Mode.SIGNAL_ONLY,
                        allocation_usd=1000.0, risk_pct=1.0))
    _ack(cb_id, f"{symbol} added — signal only.")
    events.send_now(
        f"👀 <b>{esc(symbol)}</b> added to the watchlist in "
        f"<b>SIGNAL_ONLY</b> mode.\n\n"
        f"It will tell you what it sees but never create an order. Run "
        f"strategy selection on the desk to give it an edge test, and set an "
        f"allocation in Settings before switching it to semi-auto."
    )


# --------------------------------------------------------------------------
# Slash commands
# --------------------------------------------------------------------------
def _handle_message(msg: dict) -> None:
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not _authorised(chat_id):
        log.warning("dropped message from unauthorised chat %s", chat_id)
        return
    if not text.startswith("/"):
        return

    cmd, _, rest = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    rest = rest.strip()

    handlers = {
        "/start": lambda: events.send_now(HELP),
        "/help": lambda: events.send_now(HELP),
        "/status": _cmd_status,
        "/brief": lambda: events.send_now(events.build_digest()),
        "/pending": _cmd_pending,
        "/positions": _cmd_positions,
        "/scan": _cmd_scan,
        "/scout": _cmd_scout,
        "/ideas": _cmd_ideas,
        "/kill": lambda: _cmd_kill(True),
        "/unkill": lambda: _cmd_kill(False),
    }
    if cmd in ("/watch", "/add") and rest:
        _add_symbol("", rest)
        return
    if cmd in ("/sym", "/symbol") and rest:
        _cmd_symbol(rest)
        return

    handler = handlers.get(cmd)
    if handler is None:
        events.send_now("Unknown command. Try /help.")
        return
    try:
        handler()
    except Exception as exc:
        log.exception("command %s failed", cmd)
        events.send_now(f"⚠️ {esc(cmd)} failed: {esc(exc)}")


def _cmd_status() -> None:
    from app.trading.factory import broker_status, get_broker
    from app.trading.risk import kill_switch_active

    broker = get_broker()
    acct = broker.get_account()
    positions = broker.get_positions()
    st = broker_status()
    pl = sum(p.unrealized_pl for p in positions)
    deployed = sum(abs(p.market_value) for p in positions)

    events.send_now(
        f"<b>{esc(st['describe'])}</b>\n\n"
        f"Equity   {events.money(acct.equity)}\n"
        f"Cash     {events.money(acct.cash)}\n"
        f"Open P&amp;L {events.arrow(pl)} {events.money(pl)}\n"
        f"Deployed {deployed / acct.equity * 100 if acct.equity else 0:.0f}%"
        f" across {len(positions)} position(s)\n"
        f"Market   {'🟢 open' if broker.is_market_open() else '🔴 closed'}\n"
        f"Kill switch {'🛑 ON' if kill_switch_active() else '✓ off'}"
    )


def _cmd_pending() -> None:
    from app.trading import engine

    pending = engine.pending_proposals()
    if not pending:
        events.send_now("Nothing waiting for approval.")
        return
    for p in pending:
        # Clear the dedupe so the buttons come back on demand.
        from app.db import session_scope
        from app.models import NotifiedEvent
        from sqlalchemy import delete

        with session_scope() as s:
            s.execute(
                delete(NotifiedEvent).where(
                    NotifiedEvent.kind == "proposal",
                    NotifiedEvent.key == str(p.id),
                )
            )
        events.notify_proposal(p)


def _cmd_positions() -> None:
    from app.trading.factory import get_broker

    positions = get_broker().get_positions()
    if not positions:
        events.send_now("No open positions.")
        return
    lines = ["<b>Open positions</b>", ""]
    for p in positions:
        lines.append(
            f"{events.arrow(p.unrealized_pl)} <b>{esc(p.symbol)}</b> "
            f"{p.qty:.0f} @ {events.money(p.avg_entry_price)}\n"
            f"    now {events.money(p.current_price)} · "
            f"{events.money(p.market_value, 0)} · "
            f"{events.signed_pct(p.unrealized_plpc * 100, 1)}"
        )
    events.send_now("\n".join(lines))


def _cmd_scan() -> None:
    from app.trading import engine
    from app.trading.factory import get_broker

    views = engine.run_signal_scan(get_broker(), require_market_open=False)
    fired = [v for v in views if v.signal.value in ("BUY", "SELL")]
    if not fired:
        events.send_now(
            f"Scanned {len(views)} symbols. Nothing fired — everything is "
            f"holding its current state."
        )
        return
    lines = [f"<b>Scan complete</b> — {len(fired)} signal(s)", ""]
    for v in fired:
        mark = "▲" if v.signal.value == "BUY" else "▼"
        lines.append(
            f"{mark} <b>{esc(v.symbol)}</b> {esc(v.signal.value)} @ "
            f"{events.money(v.price)}\n    <i>{esc(v.reason[:90])}</i>"
        )
    events.send_now("\n".join(lines))


def _cmd_scout() -> None:
    from app.analysis.scout import run_scout

    events.send_now("🔍 Scanning the universe — back in a minute.")
    run_id, ideas = run_scout()
    events.send_now(events.scout_message(ideas))


def _cmd_ideas() -> None:
    from app.analysis.scout import latest_ideas

    ideas = latest_ideas()
    if not ideas:
        events.send_now("No scout report yet. Run /scout.")
        return
    events.send_now(events.scout_message(ideas))


def _cmd_symbol(symbol: str) -> None:
    from app.trading import engine

    symbol = symbol.upper().strip()
    watch = engine.get_watch(symbol)
    if watch is None:
        events.send_now(
            f"{esc(symbol)} is not on your watchlist. /watch {esc(symbol)} "
            f"adds it."
        )
        return
    view = engine.evaluate_symbol(watch)
    if view is None:
        events.send_now(f"No data for {esc(symbol)}.")
        return

    from datetime import datetime, timedelta, timezone

    from app.data.store import get_bars

    end = datetime.now(timezone.utc).replace(tzinfo=None)
    bars = get_bars(symbol, end - timedelta(days=120), end)
    sparkline = events.spark(bars["close"].tolist()) if not bars.empty else ""

    events.send_now(
        f"<b>{esc(symbol)}</b> {events.money(view.price)}\n"
        f"<code>{sparkline}</code>\n\n"
        f"Signal <b>{esc(view.signal.value)}</b> · {esc(view.strategy_label)}\n"
        f"<i>{esc(view.reason)}</i>\n\n"
        f"{view.agreement}/{view.agreement_total} strategies long · "
        f"mode {esc(watch.mode.value)}"
    )


def _cmd_kill(active: bool) -> None:
    from app.trading.risk import set_kill_switch

    set_kill_switch(active, "from Telegram")
    events.send_now(
        "🛑 <b>Kill switch ON.</b> All new orders are blocked."
        if active
        else "🟢 <b>Kill switch cleared.</b> Orders can be placed again."
    )


# --------------------------------------------------------------------------
# Poll loop
# --------------------------------------------------------------------------
def _loop() -> None:
    offset: int | None = None
    log.info("telegram bot polling started")

    # Drop anything queued while the app was down. Acting on a tap from
    # yesterday is worse than missing it.
    try:
        stale = tg.get_updates(None, timeout=0)
        if stale:
            offset = stale[-1]["update_id"] + 1
            log.info("skipped %d update(s) queued while offline", len(stale))
    except Exception:
        pass

    backoff = 1
    while not _stop.is_set():
        try:
            updates = tg.get_updates(offset, timeout=settings.telegram_poll_timeout)
            backoff = 1
            for upd in updates:
                offset = upd["update_id"] + 1
                if _stop.is_set():
                    break
                if "callback_query" in upd:
                    _handle_callback(upd["callback_query"])
                elif "message" in upd:
                    _handle_message(upd["message"])
        except Exception as exc:
            log.warning("telegram poll failed (%s); retrying in %ds", exc, backoff)
            _stop.wait(backoff)
            backoff = min(backoff * 2, 300)


def start() -> bool:
    """Start the poller if Telegram is configured. Idempotent."""
    global _thread
    if not settings.telegram_configured:
        log.info("telegram not configured; bot not started")
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="telegram-bot", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2)
        _thread = None


def running() -> bool:
    return _thread is not None and _thread.is_alive()
