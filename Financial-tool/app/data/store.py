"""Bar cache.

`get_bars()` is the single door through which the rest of the app gets prices.
It serves from SQLite, fetches only what is missing, and writes back.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import delete, select

from app.data.sources import COLUMNS, DataSource, YFinanceSource, get_source
from app.db import session_scope
from app.models import AppState, Bar

log = logging.getLogger(__name__)

# How stale the newest cached bar may be before we go back to the network.
_STALE_AFTER = {"1D": timedelta(hours=12), "1H": timedelta(minutes=45)}


def _read_cache(symbol: str, timeframe: str) -> pd.DataFrame:
    with session_scope() as s:
        rows = s.execute(
            select(Bar)
            .where(Bar.symbol == symbol, Bar.timeframe == timeframe)
            .order_by(Bar.ts)
        ).scalars().all()

    if not rows:
        df = pd.DataFrame(columns=COLUMNS, dtype="float64")
        df.index = pd.DatetimeIndex([], name="ts")
        return df

    df = pd.DataFrame(
        [
            {
                "ts": r.ts,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "adj_close": r.adj_close,
                "volume": r.volume,
            }
            for r in rows
        ]
    ).set_index("ts")
    df.index = pd.DatetimeIndex(df.index, name="ts")
    return df.astype("float64")


def _write_cache(symbol: str, timeframe: str, df: pd.DataFrame, source: str) -> int:
    if df.empty:
        return 0
    with session_scope() as s:
        # Replace the overlapping range wholesale -- simpler and safer than
        # upserting row by row, and revisions (late adjustments) get picked up.
        s.execute(
            delete(Bar).where(
                Bar.symbol == symbol,
                Bar.timeframe == timeframe,
                Bar.ts >= df.index.min().to_pydatetime(),
                Bar.ts <= df.index.max().to_pydatetime(),
            )
        )
        s.add_all(
            [
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    ts=ts.to_pydatetime(),
                    open=float(r.open),
                    high=float(r.high),
                    low=float(r.low),
                    close=float(r.close),
                    adj_close=float(r.adj_close),
                    volume=float(r.volume),
                    source=source,
                )
                for ts, r in df.iterrows()
            ]
        )
    return len(df)


def _backfill_marker(symbol: str, timeframe: str) -> str:
    return f"backfill:{symbol}:{timeframe}"


def _earliest_attempted(symbol: str, timeframe: str) -> datetime | None:
    """The oldest start date we have already tried to backfill for this symbol.

    Without this, a symbol whose real history is shorter than the requested
    window (a recent IPO, say) looks permanently "missing head" and hits the
    network on every single call.
    """
    with session_scope() as s:
        row = s.get(AppState, _backfill_marker(symbol, timeframe))
        if row is None or not row.value:
            return None
        try:
            return datetime.fromisoformat(row.value)
        except ValueError:
            return None


def _record_attempt(symbol: str, timeframe: str, start: datetime) -> None:
    key = _backfill_marker(symbol, timeframe)
    with session_scope() as s:
        row = s.get(AppState, key)
        if row is None:
            s.add(AppState(key=key, value=start.isoformat()))
        elif not row.value or datetime.fromisoformat(row.value) > start:
            row.value = start.isoformat()
            row.updated_at = datetime.now(timezone.utc)


def get_bars(
    symbol: str,
    start: datetime | None = None,
    end: datetime | None = None,
    timeframe: str = "1D",
    refresh: bool = False,
    source: DataSource | None = None,
) -> pd.DataFrame:
    """Return OHLCV bars for `symbol`, fetching only what the cache lacks."""
    symbol = symbol.upper().strip()
    end = end or datetime.now(timezone.utc).replace(tzinfo=None)
    start = start or (end - timedelta(days=365 * 6))

    cached = _read_cache(symbol, timeframe)
    missing_head = False
    need_fetch = refresh or cached.empty

    if not cached.empty and not refresh:
        stale_after = _STALE_AFTER.get(timeframe, timedelta(hours=12))
        # Tolerate a few days of slack at the head: the requested start may
        # fall on a weekend or predate the symbol's listing.
        missing_head = cached.index.min() > pd.Timestamp(start) + timedelta(days=5)
        if missing_head:
            # Don't keep chasing history that does not exist.
            attempted = _earliest_attempted(symbol, timeframe)
            if attempted is not None and attempted <= start:
                missing_head = False
        stale_tail = (pd.Timestamp(end) - cached.index.max()) > stale_after
        need_fetch = missing_head or stale_tail

    if need_fetch:
        src = source or get_source()
        if cached.empty or refresh or missing_head:
            # Backfilling older history: go all the way back, otherwise we'd
            # only ever top up the recent tail and never deepen the cache.
            fetch_start = start
        else:
            # Only the tail is stale. Refetch a small overlap so revised bars
            # get corrected, without re-downloading years of history.
            overlap = (cached.index.max() - timedelta(days=7)).to_pydatetime()
            fetch_start = max(overlap, start)

        fresh = src.fetch(symbol, fetch_start, end, timeframe)

        if fresh.empty and src.name != "yfinance":
            # Alpaca's free plan serves IEX equities but refuses SIP data, so
            # every ETF comes back empty with "subscription does not permit
            # querying recent SIP data". Without this fallback, adding a broker
            # key silently removes ETFs, bond funds and commodities from the
            # whole app -- the scout, the benchmark, every backtest -- and the
            # only symptom is a warning in the log.
            log.info("%s returned nothing for %s; trying yfinance", src.name, symbol)
            src = YFinanceSource()
            fresh = src.fetch(symbol, fetch_start, end, timeframe)

        if not fresh.empty:
            # Only a fetch that returned something counts as an attempt. A
            # failed provider call recorded here would cap the symbol's history
            # at whatever was already cached, permanently: the marker says "I
            # have already been back that far" and no later call goes deeper.
            _record_attempt(symbol, timeframe, fetch_start)
            _write_cache(symbol, timeframe, fresh, src.name)
            cached = _read_cache(symbol, timeframe)
        elif cached.empty:
            log.warning("no data available for %s", symbol)

    if cached.empty:
        return cached
    return cached.loc[pd.Timestamp(start) : pd.Timestamp(end)]


def latest_price(symbol: str, timeframe: str = "1D") -> float | None:
    df = get_bars(symbol, timeframe=timeframe)
    return None if df.empty else float(df["close"].iloc[-1])


def cached_symbols(timeframe: str = "1D") -> list[str]:
    with session_scope() as s:
        return list(
            s.execute(
                select(Bar.symbol).where(Bar.timeframe == timeframe).distinct()
            ).scalars()
        )
