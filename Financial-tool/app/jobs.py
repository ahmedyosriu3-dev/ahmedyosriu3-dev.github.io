"""Scheduled background jobs.

Runs in-process via APScheduler. Times are US/Eastern so they track the
exchange rather than wherever the machine happens to be.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None
TZ = "America/New_York"


def refresh_bars() -> None:
    from app.data.store import get_bars
    from app.trading.engine import get_watchlist

    for w in get_watchlist(enabled_only=False):
        try:
            get_bars(w.symbol, refresh=True)
        except Exception as exc:
            log.warning("refresh failed for %s: %s", w.symbol, exc)


def scan_signals() -> None:
    from app.trading.engine import run_signal_scan
    from app.trading.factory import get_broker

    try:
        views = run_signal_scan(get_broker())
        log.info("signal scan covered %d symbols", len(views))
    except Exception:
        log.exception("signal scan failed")


def reselect_strategies() -> None:
    from app.analysis.selector import select_for_symbols
    from app.trading.engine import get_watchlist

    symbols = [w.symbol for w in get_watchlist(enabled_only=False)]
    if not symbols:
        return
    try:
        select_for_symbols(symbols)
        log.info("re-evaluated strategies for %d symbols", len(symbols))
    except Exception:
        log.exception("strategy reselection failed")


def expire_proposals() -> None:
    from app.trading.engine import expire_stale

    try:
        expire_stale()
    except Exception:
        log.exception("proposal expiry failed")


def sync_account() -> None:
    from app.trading.factory import get_broker
    from app.trading.risk import record_equity

    try:
        broker = get_broker()
        if broker.name == "simulator":
            broker.mark_to_market()
        record_equity(broker.get_account().equity)
    except Exception as exc:
        log.warning("account sync failed: %s", exc)


def daily_digest() -> None:
    """One message before the open that answers 'anything to do today?'"""
    from app.notify import fire
    from app.notify.events import notify_digest

    fire(notify_digest)


def weekly_scout() -> None:
    """Hunt for new instruments worth a look. Suggestions only."""
    from app.analysis.scout import run_scout
    from app.notify import fire
    from app.notify.events import notify_scout

    try:
        run_id, ideas = run_scout()
        log.info("scout produced %d ideas", len(ideas))
        fire(notify_scout, ideas, run_id)
    except Exception:
        log.exception("scout run failed")


def annual_dogs() -> None:
    """Rebuild the Dogs of the Dow list for the new year.

    Scheduled every weekday morning in the first half of January rather than
    on one exact date: the list needs the previous year's final close, the
    first trading day moves around, and a machine that was switched off on the
    2nd should still pick it up on the 5th. `refresh()` is a no-op once the
    year is stored, so the repeats cost nothing.
    """
    from app.analysis.dow import refresh

    try:
        result = refresh()
        if result is not None:
            log.info(
                "dogs of the dow rebuilt: %s",
                ", ".join(r.symbol for r in result.dogs),
            )
    except Exception:
        log.exception("dogs of the dow refresh failed")


def reset_daily() -> None:
    from app.trading.risk import reset_daily_limits

    try:
        reset_daily_limits()
    except Exception:
        log.exception("daily reset failed")


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    sched = BackgroundScheduler(timezone=TZ)

    # Market hours are 09:30-16:00 ET.
    sched.add_job(refresh_bars, CronTrigger(day_of_week="mon-fri", hour="9-16",
                                            minute="*/30", timezone=TZ),
                  id="refresh_bars", replace_existing=True)

    # Morning read, then a pass just before the close when the day's bar is
    # nearly final -- that is when a daily-bar strategy has something to say.
    sched.add_job(scan_signals, CronTrigger(day_of_week="mon-fri", hour=9, minute=45,
                                            timezone=TZ),
                  id="scan_morning", replace_existing=True)
    sched.add_job(scan_signals, CronTrigger(day_of_week="mon-fri", hour=15, minute=45,
                                            timezone=TZ),
                  id="scan_close", replace_existing=True)

    sched.add_job(reselect_strategies, CronTrigger(day_of_week="sun", hour=18,
                                                   timezone=TZ),
                  id="reselect", replace_existing=True)

    sched.add_job(expire_proposals, CronTrigger(minute="*/5", timezone=TZ),
                  id="expire", replace_existing=True)

    sched.add_job(sync_account, CronTrigger(day_of_week="mon-fri", hour="9-16",
                                            minute="*/15", timezone=TZ),
                  id="sync_account", replace_existing=True)

    sched.add_job(reset_daily, CronTrigger(day_of_week="mon-fri", hour=9, minute=0,
                                           timezone=TZ),
                  id="reset_daily", replace_existing=True)

    # An hour before the open: enough time to actually act on it.
    sched.add_job(daily_digest, CronTrigger(day_of_week="mon-fri", hour=8, minute=30,
                                            timezone=TZ),
                  id="digest", replace_existing=True)

    # Saturday morning, when the week's bars are final and you have time to
    # read rather than react.
    sched.add_job(weekly_scout, CronTrigger(day_of_week="sat", hour=10, minute=0,
                                            timezone=TZ),
                  id="scout", replace_existing=True)

    # The Dogs list is a once-a-year event, so the job is a once-a-year event.
    sched.add_job(annual_dogs, CronTrigger(month=1, day="2-15", day_of_week="mon-fri",
                                           hour=8, minute=15, timezone=TZ),
                  id="annual_dogs", replace_existing=True)

    sched.start()
    _scheduler = sched
    log.info("scheduler started with %d jobs (%s)", len(sched.get_jobs()), TZ)
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
