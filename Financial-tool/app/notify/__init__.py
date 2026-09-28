"""Outbound notifications.

The one rule this package exists to enforce: **a notification failure must
never affect a trade.** Telegram being unreachable, rate-limiting, or
returning nonsense is not a reason for an order to fail or a scan to abort, so
everything the trading path calls goes through `fire()`, which swallows.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)


def fire(fn: Callable[..., Any], *args, **kwargs) -> Any:
    """Call a notifier, absorbing anything it throws."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        log.warning("notification failed (%s): %s", getattr(fn, "__name__", fn), exc)
        return None
