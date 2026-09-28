"""Cash dividend history.

The Dogs of the Dow list is a yield ranking, and a yield needs a dividend.
This module is the one place that answers "what has this company actually
paid, and what is it currently indicating it will pay?"

Two sources behind one function, same shape as `app/data/sources.py`:

  * Alpaca's corporate-actions endpoint -- comes with the API key already in
    `.env`, returns every cash dividend with its ex-date, and covers all 30
    Dow names in a single request.
  * yfinance -- no key, used when Alpaca is not configured or returns nothing.

Everything here keys off the **ex-date**, not the pay date. Whoever holds the
share on the ex-date is the one who gets the money, so that is the date the
market prices against and the date a yield should be built from.

That distinction is not cosmetic. Alpaca's `start`/`end` parameters filter on
the *process* date, so a dividend that goes ex in mid-December and pays in
January is absent from a query ending on the 31st of December. Asking for a
year of dividends and ranking on what came back would have dropped the fourth
quarter for roughly a third of the Dow and reported those companies as having
just raised their dividend by a third. `_PAYMENT_LAG_DAYS` below is the fix:
fetch wide, then filter on the ex-date here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from app.config import settings

log = logging.getLogger(__name__)

# Median spacing between ex-dates -> payments per year. A company paying every
# 91 days is quarterly; the bands are wide because boards move dates around by
# a fortnight and nobody is tidy about December.
_FREQUENCY_BANDS: list[tuple[int, int, int]] = [
    # (min_days, max_days, payments_per_year)
    (20, 45, 12),     # monthly
    (46, 120, 4),     # quarterly
    (121, 250, 2),    # semi-annual
    (251, 450, 1),    # annual
]

# How long after an ex-date the cash can still be in transit. Every query is
# widened by this at both ends so that filtering on the ex-date afterwards has
# the complete set of rows to filter. A quarter is generous -- the usual lag is
# two to five weeks -- and over-fetching costs nothing here.
_PAYMENT_LAG_DAYS = 120


@dataclass(frozen=True)
class Dividend:
    symbol: str
    ex_date: date
    amount: float
    special: bool = False


@dataclass(frozen=True)
class DividendProfile:
    """What one symbol pays, as of a point in time."""

    symbol: str
    indicated_annual: float   # latest regular payment x payments per year
    trailing_12m: float       # what was actually paid over the last year
    payments_per_year: int
    last_ex_date: date | None
    last_amount: float
    source: str
    days_since_last: int = 0
    schedule_broken: bool = False   # a scheduled payment did not arrive

    @property
    def has_dividend(self) -> bool:
        return self.indicated_annual > 0

    @property
    def divergence(self) -> float:
        """How far the indicated rate sits from what was actually paid.

        Clearly positive means a recent raise, clearly negative a cut. Either
        way the headline yield describes a dividend that did not exist for the
        whole of the last year, which is worth saying out loud.
        """
        if self.trailing_12m <= 0:
            return 0.0
        return self.indicated_annual / self.trailing_12m - 1.0


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
def _from_alpaca(symbols: list[str], start: date, end: date) -> dict[str, list[Dividend]]:
    from alpaca.data.historical.corporate_actions import CorporateActionsClient
    from alpaca.data.requests import CorporateActionsRequest, CorporateActionsType

    client = CorporateActionsClient(settings.alpaca_api_key, settings.alpaca_api_secret)
    out: dict[str, list[Dividend]] = {s: [] for s in symbols}

    # The endpoint takes the whole basket at once, so the Dow costs one call.
    # Chunked anyway: 30 today, but this module should not be the reason a
    # longer list stops working.
    for i in range(0, len(symbols), 50):
        batch = symbols[i : i + 50]
        resp = client.get_corporate_actions(
            CorporateActionsRequest(
                symbols=batch,
                types=[CorporateActionsType.CASH_DIVIDEND],
                start=start,
                end=end,
                limit=1000,
            )
        )
        for row in resp.data.get("cash_dividends", []):
            sym = str(row.symbol).upper()
            if sym not in out:
                continue
            out[sym].append(
                Dividend(
                    symbol=sym,
                    ex_date=row.ex_date,
                    amount=float(row.rate or 0.0),
                    special=bool(getattr(row, "special", False)),
                )
            )
    return out


def _from_yfinance(symbols: list[str], start: date, end: date) -> dict[str, list[Dividend]]:
    import yfinance as yf

    out: dict[str, list[Dividend]] = {s: [] for s in symbols}
    for sym in symbols:
        try:
            series = yf.Ticker(sym).dividends
        except Exception as exc:
            log.warning("yfinance dividends failed for %s: %s", sym, exc)
            continue
        if series is None or series.empty:
            continue
        for ts, amount in series.items():
            ex = ts.date()
            if start <= ex <= end:
                # yfinance does not flag specials, so every payment is treated
                # as regular. The frequency inference below absorbs most of the
                # damage; it is one more reason Alpaca is preferred.
                out[sym].append(Dividend(sym, ex, float(amount), special=False))
    return out


def fetch_dividends(
    symbols: list[str], start: date, end: date, prefer: str = "auto"
) -> tuple[dict[str, list[Dividend]], str]:
    """Every cash dividend with an ex-date in [start, end], by symbol.

    The window is the caller's, in ex-date terms. What goes to the provider is
    deliberately wider -- see `_PAYMENT_LAG_DAYS` -- and the result is trimmed
    back here, so no caller has to know which date a given API filters on.
    """
    symbols = [s.upper().strip() for s in symbols]
    lag = timedelta(days=_PAYMENT_LAG_DAYS)
    wide_start, wide_end = start - lag, end + lag

    source = "yfinance"
    data: dict[str, list[Dividend]] = {}
    if prefer in ("auto", "alpaca") and settings.alpaca_configured:
        try:
            data = _from_alpaca(symbols, wide_start, wide_end)
            if any(data.values()):
                source = "alpaca"
            else:
                log.warning("alpaca returned no dividends; falling back to yfinance")
                data = {}
        except Exception as exc:
            log.warning("alpaca dividend fetch failed (%s); using yfinance", exc)
            data = {}

    if not data:
        data = _from_yfinance(symbols, wide_start, wide_end)

    return (
        {
            sym: sorted(
                (d for d in rows if start <= d.ex_date <= end),
                key=lambda d: d.ex_date,
            )
            for sym, rows in data.items()
        },
        source,
    )


# --------------------------------------------------------------------------
# Turning payments into a rate
# --------------------------------------------------------------------------
def _payments_per_year(ex_dates: list[date]) -> int:
    """Infer the payment schedule from the spacing of recent ex-dates."""
    if len(ex_dates) < 2:
        return 0
    gaps = sorted(
        (b - a).days for a, b in zip(ex_dates, ex_dates[1:]) if (b - a).days > 0
    )
    if not gaps:
        return 0
    median = gaps[len(gaps) // 2]
    for low, high, freq in _FREQUENCY_BANDS:
        if low <= median <= high:
            return freq
    return 0


def profile(
    symbol: str, dividends: list[Dividend], as_of: date, source: str = ""
) -> DividendProfile:
    """Summarise one symbol's payments as of a date.

    The headline number is the **indicated annual dividend** -- the latest
    regular payment multiplied by how often it is paid -- because that is the
    convention Dogs of the Dow has always used, and it is what someone buying
    today would actually collect over the next year. When the schedule cannot
    be inferred (one payment on record, an erratic payer) it falls back to what
    was actually paid over the trailing year, which is never wrong, only late.

    Special dividends are excluded. A one-off payout is not a rate, and letting
    one in would put a company at the top of a yield ranking for something it
    has no intention of repeating.

    A payment that never arrived breaks the multiplication. Boeing's last
    quarterly cheque went ex in February 2020 and the dividend was suspended
    the following month; multiplying that payment by four would have carried a
    dead 3.8% yield into the end-of-2020 ranking and put a company that paid
    nothing among the top ten highest yielders. So when the schedule has
    visibly lapsed, the indicated rate is abandoned in favour of what was
    actually paid -- which decays to zero over the year, as it should.
    """
    regular = sorted(
        (d for d in dividends if not d.special and d.amount > 0 and d.ex_date <= as_of),
        key=lambda d: d.ex_date,
    )
    if not regular:
        return DividendProfile(symbol, 0.0, 0.0, 0, None, 0.0, source)

    year_ago = as_of - timedelta(days=365)
    trailing = sum(d.amount for d in regular if d.ex_date > year_ago)

    # Two years of spacing: enough to see the schedule, recent enough that a
    # change of schedule long ago does not decide today's answer.
    recent = [d for d in regular if d.ex_date > as_of - timedelta(days=760)]
    freq = _payments_per_year([d.ex_date for d in recent])
    last = regular[-1]
    days_since = (as_of - last.ex_date).days

    # Allow three quarters of an interval of slack before calling a payment
    # missed: boards shift ex-dates by a fortnight routinely, and this must not
    # fire on a company that is merely a little late.
    broken = bool(freq) and days_since > (365 / freq) * 1.75

    if freq and not broken:
        indicated = last.amount * freq
    else:
        indicated = trailing
        if not freq:
            freq = len([d for d in regular if d.ex_date > year_ago])

    return DividendProfile(
        symbol=symbol,
        indicated_annual=round(indicated, 6),
        trailing_12m=round(trailing, 6),
        payments_per_year=freq,
        last_ex_date=last.ex_date,
        last_amount=last.amount,
        source=source,
        days_since_last=days_since,
        schedule_broken=broken,
    )


def profiles(
    symbols: list[str], as_of: date, prefer: str = "auto"
) -> dict[str, DividendProfile]:
    """Dividend profile for every symbol, as of `as_of`."""
    # Three years back: two to infer the schedule, one of slack so a symbol
    # that pays once a year in February still has two ex-dates to reason from.
    start = as_of - timedelta(days=3 * 365 + 30)
    raw, source = fetch_dividends(symbols, start, as_of, prefer=prefer)
    return {
        sym: profile(sym, raw.get(sym, []), as_of, source)
        for sym in (s.upper().strip() for s in symbols)
    }
