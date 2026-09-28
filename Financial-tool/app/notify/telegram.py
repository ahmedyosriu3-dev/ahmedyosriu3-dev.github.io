"""Thin Telegram Bot API client.

Deliberately small: send a message, edit one, answer a button tap, and poll
for updates. No third-party SDK -- the Bot API is plain HTTPS and JSON, and
one fewer dependency in the order path is worth the fifty lines.

Every call is best-effort. A notification channel that can take down a trading
app by being unreachable is a worse channel than no channel at all, so failures
are logged and swallowed rather than raised.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
_TIMEOUT = httpx.Timeout(15.0, read=40.0)


class TelegramError(RuntimeError):
    pass


def _verify() -> Any:
    """Use the merged CA bundle app.certs built, if there is one.

    This machine may sit behind a TLS-intercepting proxy; httpx builds its own
    SSL context from certifi and would otherwise ignore the merged bundle.
    """
    bundle = os.environ.get("SSL_CERT_FILE")
    return bundle if bundle and os.path.exists(bundle) else True


def configured() -> bool:
    return settings.telegram_configured


def _call(method: str, payload: dict | None = None, timeout: Any = _TIMEOUT) -> dict | None:
    """POST to one Bot API method. Returns the `result` field, or None."""
    token = settings.telegram_bot_token
    if not token:
        return None
    url = f"{API_ROOT}/bot{token}/{method}"
    try:
        with httpx.Client(timeout=timeout, verify=_verify()) as client:
            resp = client.post(url, json=payload or {})
    except Exception as exc:
        log.warning("telegram %s failed: %s", method, exc)
        return None

    if resp.status_code != 200:
        log.warning("telegram %s -> HTTP %s: %s", method, resp.status_code, resp.text[:300])
        return None
    body = resp.json()
    if not body.get("ok"):
        log.warning("telegram %s -> %s", method, body.get("description"))
        return None
    return body.get("result")


# --------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------
def esc(text: Any) -> str:
    """Escape for Telegram's HTML parse mode.

    Symbols and strategy reasons are app-generated, but AI commentary and
    company names are not, so everything interpolated into a message goes
    through here.
    """
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------
@dataclass
class Button:
    text: str
    data: str          # callback_data, <= 64 bytes
    url: str = ""

    def to_dict(self) -> dict:
        if self.url:
            return {"text": self.text, "url": self.url}
        return {"text": self.text, "callback_data": self.data}


def keyboard(rows: list[list[Button]]) -> dict:
    return {"inline_keyboard": [[b.to_dict() for b in row] for row in rows]}


def send(
    text: str,
    buttons: list[list[Button]] | None = None,
    silent: bool = False,
    chat_id: str | None = None,
) -> int | None:
    """Send a message. Returns the Telegram message id, or None."""
    if not configured():
        return None
    payload: dict = {
        "chat_id": chat_id or settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    if buttons:
        payload["reply_markup"] = keyboard(buttons)
    res = _call("sendMessage", payload)
    return res.get("message_id") if res else None


def edit(
    message_id: int,
    text: str,
    buttons: list[list[Button]] | None = None,
    chat_id: str | None = None,
) -> bool:
    """Rewrite a message in place -- used to retire spent Approve buttons."""
    if not configured():
        return False
    payload: dict = {
        "chat_id": chat_id or settings.telegram_chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    payload["reply_markup"] = keyboard(buttons) if buttons else {"inline_keyboard": []}
    return _call("editMessageText", payload) is not None


def answer_callback(callback_id: str, text: str = "", alert: bool = False) -> None:
    """Stop the button's spinner and optionally show a toast in Telegram."""
    _call(
        "answerCallbackQuery",
        {"callback_query_id": callback_id, "text": text[:200], "show_alert": alert},
    )


def get_updates(offset: int | None, timeout: int = 25) -> list[dict]:
    """Long-poll for updates. Blocks up to `timeout` seconds server-side."""
    payload: dict = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    res = _call(
        "getUpdates",
        payload,
        timeout=httpx.Timeout(15.0, read=timeout + 15),
    )
    return res or []


def check() -> tuple[bool, str]:
    """Validate the token and chat id. Used by the Settings 'Test' button."""
    if not settings.telegram_bot_token:
        return False, "TELEGRAM_BOT_TOKEN is not set in .env"
    me = _call("getMe")
    if not me:
        return False, "Bot token rejected by Telegram (or no network)."
    if not settings.telegram_chat_id:
        return False, (
            f"Bot @{me.get('username')} is reachable, but TELEGRAM_CHAT_ID is "
            "not set. Message your bot, then read the chat id from "
            "/api/telegram/whoami."
        )
    return True, f"Connected as @{me.get('username')}"


def discover_chat_id() -> str | None:
    """Read the chat id of whoever last messaged the bot.

    Saves you hunting for it: message the bot once, press the button, done.
    """
    for upd in get_updates(None, timeout=0):
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            return str(chat["id"])
    return None
