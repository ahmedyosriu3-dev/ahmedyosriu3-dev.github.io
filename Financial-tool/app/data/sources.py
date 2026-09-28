"""Market-data sources.

Two implementations behind one interface so the rest of the app never cares
where prices came from:

  * YFinanceSource -- free, no key, deep history. Default and fallback.
  * AlpacaSource   -- free IEX feed bundled with the (paper) trading account.

Both return the same normalised DataFrame:
    index: tz-naive UTC DatetimeIndex named "ts"
    cols : open, high, low, close, adj_close, volume
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd

from app.config import settings

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]


def _empty() -> pd.DataFrame:
    df = pd.DataFrame(columns=COLUMNS, dtype="float64")
    df.index = pd.DatetimeIndex([], name="ts")
    return df


class DataSource(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1D"
    ) -> pd.DataFrame:
        ...


class YFinanceSource(DataSource):
    name = "yfinance"

    def fetch(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1D"
    ) -> pd.DataFrame:
        import yfinance as yf

        interval = {"1D": "1d", "1H": "1h"}.get(timeframe, "1d")
        try:
            raw = yf.download(
                symbol,
                start=start.date(),
                end=end.date(),
                interval=interval,
                auto_adjust=False,
                progress=False,
                threads=False,
            )
        except Exception as exc:  # network / symbol errors shouldn't kill a run
            log.warning("yfinance fetch failed for %s: %s", symbol, exc)
            return _empty()

        if raw is None or raw.empty:
            return _empty()

        # yfinance returns a MultiIndex (field, ticker) even for one symbol.
        if isinstance(raw.columns, pd.MultiIndex):
            raw = raw.droplevel(1, axis=1)

        raw = raw.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Adj Close": "adj_close",
                "Volume": "volume",
            }
        )
        if "adj_close" not in raw.columns:
            raw["adj_close"] = raw["close"]

        df = raw[COLUMNS].astype("float64")
        df.index = pd.DatetimeIndex(df.index).tz_localize(None)
        df.index.name = "ts"
        return df.dropna(subset=["close"]).sort_index()


class AlpacaSource(DataSource):
    name = "alpaca"

    def __init__(self) -> None:
        from alpaca.data.historical import StockHistoricalDataClient

        self._client = StockHistoricalDataClient(
            settings.alpaca_api_key, settings.alpaca_api_secret
        )

    def fetch(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1D"
    ) -> pd.DataFrame:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        tf = TimeFrame.Hour if timeframe == "1H" else TimeFrame.Day
        try:
            resp = self._client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol, timeframe=tf, start=start, end=end
                )
            )
            raw = resp.df
        except Exception as exc:
            log.warning("alpaca fetch failed for %s: %s", symbol, exc)
            return _empty()

        if raw is None or raw.empty:
            return _empty()

        if isinstance(raw.index, pd.MultiIndex):
            raw = raw.droplevel("symbol")

        df = raw.rename(columns={"trade_count": "trades", "vwap": "vwap"})
        # Alpaca bars are already split/dividend adjusted on the free feed.
        df["adj_close"] = df["close"]
        df = df[COLUMNS].astype("float64")
        df.index = pd.DatetimeIndex(df.index).tz_localize(None)
        df.index.name = "ts"
        return df.dropna(subset=["close"]).sort_index()


def get_source(prefer: str = "auto") -> DataSource:
    """Pick a source. Alpaca when keys exist, yfinance otherwise."""
    if prefer == "yfinance":
        return YFinanceSource()
    if prefer == "alpaca" or (prefer == "auto" and settings.alpaca_configured):
        try:
            return AlpacaSource()
        except Exception as exc:
            log.warning("Alpaca source unavailable (%s); using yfinance", exc)
    return YFinanceSource()
