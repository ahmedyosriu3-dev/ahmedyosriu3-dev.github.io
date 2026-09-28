"""Saving broker / bot / AI credentials from the Settings page.

Hand-editing `.env` and restarting is a poor way to paste a key you just
generated, so the desk accepts them in the browser instead. Three rules keep
that from becoming the weak point of an app that can spend money:

* A stored secret is **never sent back to the browser** -- the page shows only
  its length and last four characters, so you can tell which key is loaded
  without the page, your scrollback, or a screenshot carrying the value.
* Submitting a blank field changes nothing. Clearing is a separate, explicit
  tick, so an accidental empty save cannot silently disconnect your broker.
* Nothing here can arm live trading. `ALPACA_LIVE` is deliberately not a field
  on this form -- that lock stays a manual edit, which is the whole point of
  it being a separate lock.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app import envfile
from app.config import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Field:
    key: str          # name in .env
    attr: str         # matching attribute on Settings
    label: str
    secret: bool      # render as a password box and mask it back
    hint: str
    group: str        # which subsystem to reload after a change


FIELDS: tuple[Field, ...] = (
    Field(
        "ALPACA_API_KEY", "alpaca_api_key", "Alpaca API key", True,
        "app.alpaca.markets → Paper Trading → API Keys → Generate", "broker",
    ),
    Field(
        "ALPACA_API_SECRET", "alpaca_api_secret", "Alpaca API secret", True,
        "Shown once, at generation time. Regenerate the pair if you lost it.",
        "broker",
    ),
    Field(
        "TELEGRAM_BOT_TOKEN", "telegram_bot_token", "Telegram bot token", True,
        "@BotFather → /newbot. A bearer credential: anyone holding it can "
        "reach your bot.", "telegram",
    ),
    Field(
        "TELEGRAM_CHAT_ID", "telegram_chat_id", "Telegram chat id", False,
        "Save the token first, then press “Find my chat id” below.", "telegram",
    ),
    Field(
        "GEMINI_API_KEY", "gemini_api_key", "Gemini API key", True,
        "aistudio.google.com/apikey. Only used when AI_PROVIDER=gemini.", "ai",
    ),
)

_BY_KEY = {f.key: f for f in FIELDS}


def current_state() -> list[dict]:
    """What the Settings page renders. Carries masks, never values."""
    stored = envfile.read_values()
    out = []
    for f in FIELDS:
        live = str(getattr(settings, f.attr, "") or "")
        out.append(
            {
                "key": f.key,
                "label": f.label,
                "secret": f.secret,
                "hint": f.hint,
                "set": bool(live),
                # A non-secret (the chat id) is worth showing in full; it is an
                # identifier, not a credential.
                "shown": (envfile.mask(live) if f.secret else live) if live else "",
                # A shell-exported variable outranks .env in pydantic-settings,
                # so saving here would appear to work and then not survive a
                # restart. Say so rather than letting it surprise you later.
                "shadowed": envfile.shadowed(f.key)
                and stored.get(f.key, "") != live,
            }
        )
    return out


def save(values: dict[str, str], clears: set[str]) -> tuple[list[str], list[str]]:
    """Persist changed credentials and apply them to the running app.

    Returns (changed labels, warnings). Values are written to `.env` so they
    survive a restart, and mirrored onto the live settings object so the
    broker, bot and analyst pick them up now.
    """
    updates: dict[str, str] = {}

    for key, raw in values.items():
        f = _BY_KEY.get(key)
        if f is None:
            continue
        if key in clears:
            updates[key] = ""
            continue
        v = (raw or "").strip()
        if not v:
            continue  # blank means "leave alone", never "erase"
        updates[key] = v

    if not updates:
        return [], []

    envfile.write_values(updates)

    warnings: list[str] = []
    groups: set[str] = set()
    for key, value in updates.items():
        f = _BY_KEY[key]
        setattr(settings, f.attr, value)
        groups.add(f.group)
        if envfile.shadowed(key):
            warnings.append(
                f"{key} is also set as a system environment variable, which "
                "wins over .env — it is active now, but a restart will go back "
                "to the system value until you remove it."
            )

    _reload(groups)

    from app.trading.risk import audit

    audit(
        "credentials_saved",
        "Credentials updated from Settings: " + ", ".join(sorted(updates)),
        # Deliberately records which keys changed and never what they became.
        keys=sorted(updates),
        cleared=sorted(k for k in updates if not updates[k]),
    )
    return [_BY_KEY[k].label for k in sorted(updates)], warnings


def _reload(groups: set[str]) -> None:
    """Re-resolve whichever subsystems the changed keys feed."""
    if "broker" in groups:
        from app.trading.factory import get_broker

        try:
            log.info("broker reloaded: %s", get_broker(force_reload=True).describe())
        except Exception as exc:  # a bad key must not take the page down
            log.warning("broker reload failed: %s", exc)

    if "telegram" in groups:
        from app.notify import bot

        try:
            bot.stop()
            if bot.start():
                log.info("telegram bot restarted")
        except Exception as exc:
            log.warning("telegram reload failed: %s", exc)

    # The AI analyst builds its provider per call from `settings`, so a changed
    # key is already live -- nothing to reload.
