"""Telegram notifications and the approve-from-phone path.

The Telegram buttons are the only part of this app where a tap on a device
that is not this machine can move money. The guarantees that matter:

  * an update from any chat other than the configured one changes nothing
  * approvals are refused unless explicitly switched on
  * live approvals need their own switch *and* a second tap
  * approving from Telegram runs the identical engine path as the dashboard,
    so every risk check still applies
  * a Telegram outage can never break a trade

None of this makes a phone button safe. It makes it as safe as a phone button
can be, which is a different claim.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest

from app.models import OrderProposal, ProposalStatus
from app.notify import fire
from app.notify import bot, events
from app.notify import telegram as tg


# --------------------------------------------------------------------------
# Nothing the notifier does may break trading
# --------------------------------------------------------------------------
def test_fire_swallows_everything():
    def explode():
        raise RuntimeError("telegram is on fire")

    assert fire(explode) is None          # no exception escapes


def test_fire_returns_the_value_on_success():
    assert fire(lambda x: x * 2, 21) == 42


# --------------------------------------------------------------------------
# Authorisation
# --------------------------------------------------------------------------
def test_only_the_configured_chat_is_authorised(monkeypatch):
    monkeypatch.setattr(tg.settings, "telegram_chat_id", "123456", raising=False)
    assert bot._authorised("123456")
    assert bot._authorised(123456)        # Telegram sends ints
    assert not bot._authorised("999999")
    assert not bot._authorised("")
    assert not bot._authorised(None)


def test_unauthorised_callback_never_reaches_the_engine(monkeypatch):
    """A tap from a stranger's chat must not call approve_proposal at all."""
    monkeypatch.setattr(tg.settings, "telegram_chat_id", "123456", raising=False)

    answered, approved = [], []
    monkeypatch.setattr(bot.tg, "answer_callback",
                        lambda cb, text="", alert=False: answered.append(text))
    monkeypatch.setattr(bot, "_approve",
                        lambda *a, **k: approved.append(a))

    bot._handle_callback({
        "id": "cb1", "data": "ap:1",
        "message": {"message_id": 5, "chat": {"id": "999999"}},
    })

    assert approved == [], "an unauthorised chat reached the approval path"
    assert answered and "Not authorised" in answered[0]


def test_unauthorised_message_is_dropped(monkeypatch):
    monkeypatch.setattr(tg.settings, "telegram_chat_id", "123456", raising=False)
    sent = []
    monkeypatch.setattr(bot.events, "send_now", lambda *a, **k: sent.append(a))

    bot._handle_message({"chat": {"id": "999999"}, "text": "/status"})
    assert sent == []


# --------------------------------------------------------------------------
# Approval switches
# --------------------------------------------------------------------------
def test_approvals_refused_when_switched_off(monkeypatch):
    monkeypatch.setattr(events.settings, "telegram_allow_approvals", False,
                        raising=False)
    ok, why = events.approvals_allowed()
    assert not ok
    assert "switched off" in why


def test_paper_approvals_allowed_when_switched_on(monkeypatch):
    monkeypatch.setattr(events.settings, "telegram_allow_approvals", True,
                        raising=False)
    monkeypatch.setattr("app.trading.risk.live_trading_permitted", lambda: False)
    ok, _ = events.approvals_allowed()
    assert ok


def test_live_approvals_refused_without_their_own_switch(monkeypatch):
    monkeypatch.setattr(events.settings, "telegram_allow_approvals", True,
                        raising=False)
    monkeypatch.setattr(events.settings, "telegram_allow_live_approvals", False,
                        raising=False)
    monkeypatch.setattr("app.trading.risk.live_trading_permitted", lambda: True)

    ok, why = events.approvals_allowed()
    assert not ok
    assert "dashboard" in why


def test_live_approvals_allowed_with_both_switches(monkeypatch):
    monkeypatch.setattr(events.settings, "telegram_allow_approvals", True,
                        raising=False)
    monkeypatch.setattr(events.settings, "telegram_allow_live_approvals", True,
                        raising=False)
    monkeypatch.setattr("app.trading.risk.live_trading_permitted", lambda: True)
    ok, _ = events.approvals_allowed()
    assert ok


def test_live_order_needs_two_taps(monkeypatch):
    """The first tap on a live order must arm, never submit."""
    monkeypatch.setattr(events.settings, "telegram_allow_approvals", True,
                        raising=False)
    monkeypatch.setattr(events.settings, "telegram_allow_live_approvals", True,
                        raising=False)
    monkeypatch.setattr(tg.settings, "telegram_chat_id", "123456", raising=False)

    edits, approved = [], []
    monkeypatch.setattr(bot.tg, "answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(bot.tg, "edit",
                        lambda mid, text, buttons=None, chat_id=None: edits.append(text))
    monkeypatch.setattr(bot, "_approve", lambda *a, **k: approved.append(a))

    # "apc" is the arming callback the live message carries.
    bot._handle_callback({
        "id": "cb", "data": "apc:7",
        "message": {"message_id": 5, "chat": {"id": "123456"}},
    })

    assert approved == [], "the arming tap submitted an order"
    assert edits and "Confirm" in edits[0]


def test_a_live_message_carries_the_arming_callback_not_the_direct_one():
    """Wiring check: the live button must be 'apc', which only arms."""
    src = inspect.getsource(events.notify_proposal)
    assert 'f"apc:{p.id}"' in src
    assert "if live" in src


def test_telegram_approval_uses_the_same_engine_path():
    """No shortcut from a phone to a broker.

    If someone ever gives the bot its own order-submission code, the risk gate
    stops applying to half the app. This asserts the single path structurally.
    """
    src = inspect.getsource(bot._approve)
    assert "engine.approve_proposal" in src
    assert "submit_bracket_buy" not in src, "the bot must never call a broker directly"


def test_bot_module_never_imports_a_broker_directly():
    src = inspect.getsource(bot)
    assert "submit_bracket_buy" not in src
    assert "close_position" not in src


# --------------------------------------------------------------------------
# Message rendering
# --------------------------------------------------------------------------
def test_escaping_neutralises_markup():
    assert tg.esc("<script>alert(1)</script>") == (
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    )
    assert tg.esc("A & B") == "A &amp; B"


def make_proposal(**kw) -> OrderProposal:
    defaults = dict(
        id=1, signal_id=1, symbol="AAPL", side="buy", qty=10,
        est_price=200.0, stop_price=190.0, target_price=230.0,
        notional=2000.0, risk_usd=100.0, rationale="EMA cross with ADX 27",
        status=ProposalStatus.PENDING,
        expires_at=datetime(2026, 9, 11) + timedelta(hours=2),
    )
    defaults.update(kw)
    return OrderProposal(**defaults)


def test_proposal_message_carries_every_number_you_need():
    text = events.proposal_message(make_proposal(), live=False)
    for fragment in ("AAPL", "BUY 10", "$200.00", "$190.00", "$230.00", "$100"):
        assert fragment in text, f"missing {fragment}"
    assert "3.0R" in text, "risk/reward should be stated"


def test_live_proposal_is_visually_distinct():
    paper = events.proposal_message(make_proposal(), live=False)
    live = events.proposal_message(make_proposal(), live=True)
    assert "LIVE ORDER" in live and "LIVE ORDER" not in paper


def test_proposal_rationale_is_escaped():
    text = events.proposal_message(
        make_proposal(rationale="<b>bold</b> & risky"), live=False
    )
    assert "<b>bold</b>" not in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def test_spark_renders_a_shape():
    rising = events.spark([1, 2, 3, 4, 5])
    assert rising[0] == "▁" and rising[-1] == "█"
    falling = events.spark([5, 4, 3, 2, 1])
    assert falling[0] == "█" and falling[-1] == "▁"


def test_spark_handles_degenerate_input():
    assert events.spark([]) == ""
    assert events.spark([7]) == ""
    assert set(events.spark([3, 3, 3])) == {"▁"}     # flat line, no divide by zero


def test_spark_is_downsampled_to_the_width():
    assert len(events.spark(list(range(500)), width=24)) == 24


def test_bar_is_proportional():
    assert events.bar(0) == "░" * 10
    assert events.bar(100) == "█" * 10
    assert events.bar(50) == "█" * 5 + "░" * 5
    assert events.bar(150) == "█" * 10       # clamped, never overflows
    assert events.bar(-20) == "░" * 10


def test_money_and_pct_formatting():
    assert events.money(1234.5) == "$1,234.50"
    assert events.money(1234.6, 0) == "$1,235"
    assert events.signed_pct(1.234) == "+1.23%"
    assert events.signed_pct(-1.234) == "-1.23%"
    assert events.arrow(1) == "🟢" and events.arrow(-1) == "🔴"


# --------------------------------------------------------------------------
# Sending is inert without configuration
# --------------------------------------------------------------------------
def test_send_does_nothing_without_a_token(monkeypatch):
    monkeypatch.setattr(tg.settings, "telegram_bot_token", "", raising=False)
    monkeypatch.setattr(tg.settings, "telegram_chat_id", "", raising=False)
    assert tg.send("hello") is None
    assert tg.edit(1, "hello") is False


def test_bot_does_not_start_unconfigured(monkeypatch):
    monkeypatch.setattr(bot.settings, "telegram_bot_token", "", raising=False)
    monkeypatch.setattr(bot.settings, "telegram_chat_id", "", raising=False)
    assert bot.settings.telegram_configured is False
    assert bot.start() is False
    assert bot.running() is False
