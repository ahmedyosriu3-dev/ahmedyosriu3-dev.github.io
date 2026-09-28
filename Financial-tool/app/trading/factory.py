"""Broker selection.

Falls back to the local simulator when Alpaca keys are absent, so the app is
fully usable before any account exists. The UI always shows which broker is
actually in play -- silently pretending to trade would be the worst outcome.
"""
from __future__ import annotations

import logging

from app.config import settings
from app.trading.broker import Broker

log = logging.getLogger(__name__)

_cached: Broker | None = None


def get_broker(force_reload: bool = False) -> Broker:
    global _cached
    if _cached is not None and not force_reload:
        return _cached

    if settings.alpaca_configured:
        try:
            from app.trading.alpaca_broker import AlpacaBroker

            _cached = AlpacaBroker()
            log.info("using %s", _cached.describe())
            return _cached
        except Exception as exc:
            log.warning("Alpaca unavailable (%s); falling back to simulator", exc)

    from app.trading.sim_broker import SimBroker

    _cached = SimBroker()
    log.info("using %s", _cached.describe())
    return _cached


def broker_status() -> dict:
    b = get_broker()
    from app.trading.risk import kill_switch_active, live_trading_permitted

    return {
        "name": b.name,
        "is_paper": b.is_paper,
        "is_simulator": b.name == "simulator",
        "live_permitted": live_trading_permitted(),
        "kill_switch": kill_switch_active(),
        "alpaca_configured": settings.alpaca_configured,
        "describe": b.describe(),
    }
