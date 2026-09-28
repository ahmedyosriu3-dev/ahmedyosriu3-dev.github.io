"""What the app says on Telegram, and when.

Every composer here is best-effort and deduplicated: a restart, a manual
rescan and the scheduled scan all try to announce the same proposal, and you
should hear about it once.

Messages are built to be read on a phone at a glance -- the decision first,
the numbers second, the reasoning last.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.models import NotifiedEvent, OrderProposal, ScoutIdea
from app.notify import telegram as tg
from app.notify.telegram import Button, esc

log = logging.getLogger(__name__)

SPARK_CHARS = "▁▂▃▄▅▆▇█"


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def money(v: float, dp: int = 2) -> str:
    return f"${v:,.{dp}f}"


def signed_pct(v: float, dp: int = 2) -> str:
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{dp}f}%"


def arrow(v: float) -> str:
    return "🟢" if v > 0 else ("🔴" if v < 0 else "⚪")


def spark(values, width: int = 24) -> str:
    """A one-line price chart out of block characters.

    Telegram has no images without an upload round-trip; this costs nothing
    and answers "what shape is this?" well enough to earn its 24 bytes.
    """
    vals = [float(v) for v in values if v == v]
    if len(vals) < 2:
        return ""
    if len(vals) > width:
        step = len(vals) / width
        vals = [vals[int(i * step)] for i in range(width)]
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return SPARK_CHARS[0] * len(vals)
    span = hi - lo
    top = len(SPARK_CHARS) - 1
    return "".join(
        SPARK_CHARS[min(int((v - lo) / span * top), top)] for v in vals
    )


def bar(pct: float, width: int = 10) -> str:
    """A 0-100 score as a filled bar."""
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


# --------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------
def _already_sent(kind: str, key: str) -> bool:
    with session_scope() as s:
        row = s.execute(
            select(NotifiedEvent.id).where(
                NotifiedEvent.kind == kind, NotifiedEvent.key == key
            )
        ).first()
    return row is not None


def _mark_sent(kind: str, key: str, message_id: int | None) -> None:
    with session_scope() as s:
        s.add(NotifiedEvent(kind=kind, key=str(key), message_id=message_id))


def message_id_for(kind: str, key: str) -> int | None:
    with session_scope() as s:
        return s.execute(
            select(NotifiedEvent.message_id).where(
                NotifiedEvent.kind == kind, NotifiedEvent.key == str(key)
            )
        ).scalar_one_or_none()


def _send_once(kind: str, key: str, text: str, buttons=None,
               silent: bool = False) -> bool:
    if not tg.configured() or _already_sent(kind, str(key)):
        return False
    mid = tg.send(text, buttons=buttons, silent=silent)
    if mid is None:
        return False
    _mark_sent(kind, str(key), mid)
    return True


# --------------------------------------------------------------------------
# Approval-critical: a proposal waiting for you
# --------------------------------------------------------------------------
def approvals_allowed() -> tuple[bool, str]:
    """Whether a Telegram tap may reach the broker, and why not if it may not."""
    from app.trading.risk import live_trading_permitted

    if not settings.telegram_allow_approvals:
        return False, "Approvals from Telegram are switched off in Settings."
    if live_trading_permitted() and not settings.telegram_allow_live_approvals:
        return False, "Live trading is armed; approve this one on the dashboard."
    return True, ""


def proposal_message(p: OrderProposal, live: bool) -> str:
    rr = ""
    if p.stop_price and p.target_price and p.est_price:
        risk = p.est_price - p.stop_price
        reward = p.target_price - p.est_price
        if risk > 0:
            rr = f"  ·  {reward / risk:.1f}R to target"

    head = "🔴 <b>LIVE ORDER</b>" if live else "📋 <b>Paper order</b>"
    return (
        f"{head} — needs your approval\n"
        f"\n"
        f"<b>BUY {p.qty:.0f} × {esc(p.symbol)}</b> @ ~{money(p.est_price)}\n"
        f"<code>Cost    {money(p.notional, 0):>12}</code>\n"
        f"<code>Stop    {money(p.stop_price or 0):>12}</code>\n"
        f"<code>Target  {money(p.target_price or 0):>12}</code>\n"
        f"<code>At risk {money(p.risk_usd, 0):>12}</code>{rr}\n"
        f"\n"
        f"<i>{esc(p.rationale)}</i>\n"
        f"\n"
        f"⏳ Lapses in {settings.proposal_ttl_minutes} min if untouched."
    )


def notify_proposal(p: OrderProposal) -> bool:
    """Announce a pending proposal, with buttons if approvals are allowed."""
    from app.trading.risk import live_trading_permitted

    live = live_trading_permitted()
    allowed, why = approvals_allowed()
    text = proposal_message(p, live)

    if allowed:
        # Live orders take two taps: the first only arms the second. A pocket
        # tap should not be able to spend real money.
        approve = (
            Button("🔴 Approve LIVE", f"apc:{p.id}")
            if live
            else Button("✅ Approve & submit", f"ap:{p.id}")
        )
        buttons = [
            [approve, Button("✖ Dismiss", f"rj:{p.id}")],
            [Button("📊 Why this?", f"why:{p.id}")],
        ]
    else:
        text += f"\n\n🔒 {esc(why)}"
        buttons = [[Button("📊 Why this?", f"why:{p.id}")]]

    return _send_once("proposal", p.id, text, buttons=buttons)


def retire_proposal_message(proposal_id: int, outcome: str) -> None:
    """Strip the buttons off a proposal message once it has been decided."""
    mid = message_id_for("proposal", str(proposal_id))
    if mid is None:
        return
    with session_scope() as s:
        p = s.get(OrderProposal, proposal_id)
        if p is None:
            return
        summary = (
            f"<b>{esc(p.symbol)}</b> — BUY {p.qty:.0f} @ ~{money(p.est_price)}\n"
            f"{esc(outcome)}"
        )
    tg.edit(mid, summary, buttons=None)


# --------------------------------------------------------------------------
# Signals, fills, and the things that should reach your pocket
# --------------------------------------------------------------------------
def notify_signal(view) -> bool:
    """A BUY/SELL on a symbol that will never produce a proposal by itself."""
    emoji = "▲" if view.signal.value == "BUY" else "▼"
    key = f"{view.symbol}:{view.bar_ts:%Y-%m-%d}:{view.signal.value}"
    text = (
        f"{emoji} <b>{esc(view.signal.value)} {esc(view.symbol)}</b> "
        f"@ {money(view.price)}\n"
        f"<i>{esc(view.reason)}</i>\n\n"
        f"{esc(view.strategy_label)} · {view.agreement}/{view.agreement_total} "
        f"strategies long\n"
        f"<i>Signal only — no order was created.</i>"
    )
    return _send_once("signal", key, text, silent=True)


def notify_order_submitted(symbol: str, qty: float, price: float,
                           stop: float | None, live: bool, order_id: str) -> bool:
    tag = "🔴 LIVE" if live else "📄 Paper"
    text = (
        f"✅ <b>Order submitted</b> — {tag}\n\n"
        f"BUY {qty:.0f} × <b>{esc(symbol)}</b> @ ~{money(price)}\n"
        f"Protective stop at {money(stop or 0)}.\n\n"
        f"<code>{esc(order_id)}</code>"
    )
    return _send_once("order", order_id, text)


def notify_kill_switch(active: bool, reason: str) -> bool:
    key = f"{active}:{datetime.now(timezone.utc):%Y-%m-%d %H:%M}"
    if active:
        text = (
            f"🛑 <b>KILL SWITCH TRIPPED</b>\n\n{esc(reason)}\n\n"
            f"All new orders are blocked until you clear it on the dashboard."
        )
    else:
        text = f"🟢 <b>Kill switch cleared.</b> {esc(reason)}"
    return _send_once("kill", key, text)


# --------------------------------------------------------------------------
# The morning brief
# --------------------------------------------------------------------------
def build_digest() -> str:
    """One message that answers: do I need to do anything today?"""
    from app.trading import engine
    from app.trading.factory import get_broker
    from app.trading.risk import kill_switch_active

    broker = get_broker()
    try:
        account = broker.get_account()
        positions = broker.get_positions()
    except Exception as exc:
        return f"⚠️ Could not read the account: {esc(exc)}"

    pending = engine.pending_proposals()
    total_pl = sum(p.unrealized_pl for p in positions)
    deployed = sum(abs(p.market_value) for p in positions)
    dep_pct = (deployed / account.equity * 100) if account.equity else 0.0
    pl_pct = (total_pl / account.equity * 100) if account.equity else 0.0

    lines = [
        f"☀️ <b>Morning brief</b> · {datetime.now():%a %d %b}",
        "",
        f"<b>Equity</b> {money(account.equity)}   "
        f"{arrow(total_pl)} {signed_pct(pl_pct)} open",
        f"<b>Cash</b> {money(account.cash)} · {dep_pct:.0f}% deployed · "
        f"{len(positions)} position{'' if len(positions) == 1 else 's'}",
    ]

    if kill_switch_active():
        lines += ["", "🛑 <b>Kill switch is ON</b> — no orders can be placed."]

    if positions:
        lines += ["", "<b>Your book</b>"]
        for p in sorted(positions, key=lambda x: -abs(x.unrealized_pl))[:8]:
            lines.append(
                f"{arrow(p.unrealized_pl)} <code>{esc(p.symbol):<6}</code>"
                f"{money(p.market_value, 0):>9}  "
                f"{signed_pct(p.unrealized_plpc * 100, 1):>8}"
            )

    if pending:
        lines += ["", f"⚡ <b>{len(pending)} order(s) waiting for you</b>"]
        for p in pending[:5]:
            lines.append(
                f"   BUY {p.qty:.0f} {esc(p.symbol)} · {money(p.notional, 0)} "
                f"· risk {money(p.risk_usd, 0)}"
            )
    else:
        lines += ["", "✓ Nothing waiting for approval."]

    # Anything the watchlist is shouting about today.
    hot = []
    for w in engine.get_watchlist():
        try:
            v = engine.evaluate_symbol(w)
        except Exception:
            continue
        if v and v.signal.value in ("BUY", "SELL"):
            mark = "▲" if v.signal.value == "BUY" else "▼"
            hot.append(
                f"{mark} <b>{esc(v.symbol)}</b> {esc(v.signal.value)} — "
                f"{esc(v.reason[:70])}"
            )
    if hot:
        lines += ["", "<b>Fresh signals</b>"] + hot[:6]

    return "\n".join(lines)


def notify_digest() -> bool:
    key = f"{datetime.now():%Y-%m-%d}"
    return _send_once(
        "digest", key, build_digest(),
        buttons=[[Button("🔄 Rescan now", "cmd:scan"),
                  Button("💡 Find ideas", "cmd:scout")]],
    )


# --------------------------------------------------------------------------
# Scout report
# --------------------------------------------------------------------------
def scout_message(ideas: list[ScoutIdea], limit: int = 6) -> str:
    if not ideas:
        return (
            "🔍 <b>Scout report</b>\n\nNothing cleared the bar this week. "
            "That is a real answer, not a failure — most weeks there is no "
            "obvious new thing worth your money."
        )

    lines = [
        f"💡 <b>Scout report</b> · {datetime.now():%d %b}",
        f"<i>{len(ideas)} ideas passed screening. Things to research, "
        f"not orders.</i>",
        "",
    ]
    for i, idea in enumerate(ideas[:limit], 1):
        reasons = json.loads(idea.reasons_json or "[]")
        lines += [
            f"<b>{i}. {esc(idea.symbol)}</b> — {esc(idea.name[:38])}",
            f"<code>{bar(idea.score)}</code> {idea.score:.0f}/100 · "
            f"{esc(idea.asset_class)} · {money(idea.price)}",
            f"<code>{spark(json.loads(idea.spark_json or '[]'))}</code> "
            f"{signed_pct(idea.ret_6m * 100, 0)} 6m",
            f"<i>{esc(idea.headline)}</i>",
        ]
        if reasons:
            lines.append("   " + esc(" · ".join(reasons[:2])))
        lines.append("")

    lines.append("Tap a symbol below to add it to your watchlist.")
    return "\n".join(lines)


def notify_scout(ideas: list[ScoutIdea], run_id: str) -> bool:
    buttons: list[list[Button]] = []
    row: list[Button] = []
    for idea in ideas[:6]:
        row.append(Button(f"+ {idea.symbol}", f"add:{idea.symbol}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return _send_once("scout", run_id, scout_message(ideas), buttons=buttons)


# --------------------------------------------------------------------------
# Ad-hoc
# --------------------------------------------------------------------------
def send_now(text: str, buttons=None) -> int | None:
    """Bypass dedupe -- for /commands and the Settings test button."""
    return tg.send(text, buttons=buttons)
