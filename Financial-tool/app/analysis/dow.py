"""Dogs of the Dow.

The oldest mechanical strategy that still has a following, and the simplest:

  1. On the last trading day of the year, rank the 30 Dow components by
     dividend yield.
  2. Buy the top ten, equally weighted.
  3. Hold for the whole of the next calendar year. Do nothing else.
  4. Repeat.

The five cheapest of those ten by share price are the **Small Dogs** (also
sold as the "Puppies" or the "Flying Five") -- a higher-variance version of
the same idea with no separate theory behind it.

The case for it is that a high yield on a Dow component usually means the
price has fallen rather than the dividend has risen, so the screen buys large,
established companies when they are out of favour and sells them when they are
not. The case against it is that this is a value tilt with a good story, it has
had long stretches of lagging the index (most of the 2010s), and a yield can
also be high because the dividend is about to be cut. The app shows the list
and the comparison; it does not tell you the strategy works.

Nothing here places or proposes an order. It is a list.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.data.dividends import DividendProfile, profiles
from app.data.store import get_bars
from app.db import session_scope
from app.models import DogsEntry

log = logging.getLogger(__name__)

N_DOGS = 10
N_SMALL_DOGS = 5

# --------------------------------------------------------------------------
# Membership
#
# Curated and dated, for the same reason `universe.py` is: there is no free,
# reliable index-constituent API, and a list you can read is better than a
# scrape that can silently go wrong. S&P Dow Jones Indices changes this a
# handful of times a decade, so an annual glance at the changes below is the
# whole maintenance burden.
# --------------------------------------------------------------------------
AS_OF = date(2026, 9, 13)

DOW30: dict[str, str] = {
    "AAPL": "Apple",
    "AMGN": "Amgen",
    "AMZN": "Amazon",
    "AXP": "American Express",
    "BA": "Boeing",
    "CAT": "Caterpillar",
    "CRM": "Salesforce",
    "CSCO": "Cisco Systems",
    "CVX": "Chevron",
    "DIS": "Walt Disney",
    "GS": "Goldman Sachs",
    "HD": "Home Depot",
    "HON": "Honeywell",
    "IBM": "IBM",
    "JNJ": "Johnson & Johnson",
    "JPM": "JPMorgan Chase",
    "KO": "Coca-Cola",
    "MCD": "McDonald's",
    "MMM": "3M",
    "MRK": "Merck",
    "MSFT": "Microsoft",
    "NKE": "Nike",
    "NVDA": "NVIDIA",
    "PG": "Procter & Gamble",
    "SHW": "Sherwin-Williams",
    "TRV": "Travelers",
    "UNH": "UnitedHealth",
    "V": "Visa",
    "VZ": "Verizon",
    "WMT": "Walmart",
}

# Index changes, newest first, used to reconstruct the membership on any past
# ranking date. `added` joined the index on that date and `removed` left it.
MEMBERSHIP_CHANGES: list[tuple[date, dict[str, str], dict[str, str]]] = [
    (
        date(2024, 11, 8),
        {"NVDA": "NVIDIA", "SHW": "Sherwin-Williams"},
        {"INTC": "Intel", "DOW": "Dow Inc"},
    ),
    (
        date(2024, 2, 26),
        {"AMZN": "Amazon"},
        {"WBA": "Walgreens Boots Alliance"},
    ),
    (
        date(2020, 8, 31),
        {"CRM": "Salesforce", "AMGN": "Amgen", "HON": "Honeywell"},
        {"XOM": "Exxon Mobil", "PFE": "Pfizer", "RTX": "Raytheon Technologies"},
    ),
]

# The oldest membership this module can reconstruct. Before this, the changes
# above run out and the honest answer is "I do not know who was in the index",
# not a list that looks authoritative and is wrong.
EARLIEST_KNOWN = date(2020, 8, 31)


class MembershipUnknown(Exception):
    """Raised for a ranking date older than the recorded index changes."""


def members_on(day: date) -> dict[str, str]:
    """Who was in the Dow on `day`, as {symbol: name}."""
    if day < EARLIEST_KNOWN:
        raise MembershipUnknown(
            f"Dow membership before {EARLIEST_KNOWN} is not recorded in this app; "
            f"add the index change to MEMBERSHIP_CHANGES to go further back."
        )
    members = dict(DOW30)
    # Walk backwards, undoing each change that had not happened yet.
    for effective, added, removed in MEMBERSHIP_CHANGES:
        if day < effective:
            for sym in added:
                members.pop(sym, None)
            members.update(removed)
    return members


# --------------------------------------------------------------------------
# The ranking
# --------------------------------------------------------------------------
@dataclass
class DogRow:
    symbol: str
    name: str
    price: float
    annual_dividend: float
    dividend_yield: float          # 0.032 == 3.2%
    trailing_12m: float
    payments_per_year: int
    last_ex_date: date | None
    rank: int = 0
    is_dog: bool = False
    is_small_dog: bool = False
    note: str = ""


@dataclass
class DogsList:
    year: int
    ranking_date: date
    rows: list[DogRow] = field(default_factory=list)
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def dogs(self) -> list[DogRow]:
        return [r for r in self.rows if r.is_dog]

    @property
    def small_dogs(self) -> list[DogRow]:
        return [r for r in self.rows if r.is_small_dog]


def ranking_date_for(year: int, today: date | None = None) -> date:
    """The last trading day before `year` starts.

    The Dow's own calendar is taken from a Dow component's bars rather than a
    holiday table, so it cannot drift out of date. For the current year before
    its first list exists, this still points at last December, which is the
    date the list in force today was built from.
    """
    today = today or datetime.now(timezone.utc).date()
    target = date(year - 1, 12, 31)
    start, end = datetime(year - 1, 12, 1), datetime(year - 1, 12, 31, 23, 59)

    bars = get_bars("MSFT", start, end)
    if bars.empty:
        # Same cache-hole retry as `_price_on`; see the note there.
        bars = get_bars("MSFT", start, end, refresh=True)
    if bars.empty:
        # No bars (a cold cache with no network, or a year in the future).
        # Fall back to the last weekday of December.
        while target.weekday() >= 5:
            target -= timedelta(days=1)
        return target
    return bars.index.max().date()


def _price_on(symbol: str, day: date) -> float:
    """Closing price on `day`, or the last close before it.

    The retry matters more than it looks. Asking the bar cache for a short
    window around one date in the past leaves an island of bars behind, and the
    cache decides whether to go back to the network by looking at the oldest
    and newest rows it holds -- it cannot see a hole in the middle. Build a
    list for 2026 and then one for 2022 and every price comes back empty, which
    reads as "the Dow paid no dividends in 2021" rather than as a cache miss.
    A forced refresh refetches the window and fills the hole.
    """
    start = datetime.combine(day - timedelta(days=45), datetime.min.time())
    end = datetime.combine(day, datetime.max.time())

    bars = get_bars(symbol, start, end)
    if bars.empty:
        bars = get_bars(symbol, start, end, refresh=True)
    if bars.empty:
        return 0.0
    # Actual close, not adjusted: a yield and a share price are both nominal,
    # and the Small Dogs screen ranks on the price actually printed that day.
    return float(bars["close"].iloc[-1])


def build_list(year: int, prefer_source: str = "auto") -> DogsList:
    """Compute the Dogs of the Dow list that applies to `year`."""
    rank_day = ranking_date_for(year)
    members = members_on(rank_day)
    warnings: list[str] = []

    divs: dict[str, DividendProfile] = profiles(
        list(members), as_of=rank_day, prefer=prefer_source
    )
    source = next((p.source for p in divs.values() if p.source), "")

    rows: list[DogRow] = []
    for symbol, name in members.items():
        price = _price_on(symbol, rank_day)
        prof = divs.get(symbol)
        if price <= 0:
            warnings.append(f"{symbol}: no price on {rank_day}, left out of the ranking")
            continue
        if prof is None or not prof.has_dividend:
            # A Dow member that pays nothing is not an error -- it simply
            # cannot be a Dog. It stays in the table at a 0% yield so the
            # ranking is visibly over all 30 names.
            rows.append(
                DogRow(symbol, name, price, 0.0, 0.0, 0.0, 0, None,
                       note="pays no dividend")
            )
            continue

        note = ""
        if prof.schedule_broken:
            note = (
                f"payment overdue — last went ex {prof.days_since_last} days "
                f"before the ranking date, so the yield shown is what was paid, "
                f"not a rate you can expect"
            )
        elif abs(prof.divergence) >= 0.10:
            direction = "raised" if prof.divergence > 0 else "cut"
            note = (
                f"dividend {direction} recently — the indicated rate is "
                f"{prof.divergence * 100:+.0f}% against the last 12 months paid"
            )

        rows.append(
            DogRow(
                symbol=symbol,
                name=name,
                price=price,
                annual_dividend=prof.indicated_annual,
                dividend_yield=prof.indicated_annual / price,
                trailing_12m=prof.trailing_12m,
                payments_per_year=prof.payments_per_year,
                last_ex_date=prof.last_ex_date,
                note=note,
            )
        )

    # Highest yield first; ties broken by the cheaper share, which is the same
    # tiebreak the Small Dogs screen uses.
    rows.sort(key=lambda r: (-r.dividend_yield, r.price))
    for i, row in enumerate(rows, start=1):
        row.rank = i
        row.is_dog = i <= N_DOGS and row.dividend_yield > 0

    dogs = [r for r in rows if r.is_dog]
    for row in sorted(dogs, key=lambda r: r.price)[:N_SMALL_DOGS]:
        row.is_small_dog = True

    if len(dogs) < N_DOGS:
        warnings.append(
            f"only {len(dogs)} of {N_DOGS} dogs could be ranked — "
            f"dividend data is missing for some members"
        )

    return DogsList(year=year, ranking_date=rank_day, rows=rows,
                    source=source, warnings=warnings)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def save_list(result: DogsList) -> int:
    """Replace the stored list for that year. Returns rows written."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        s.execute(delete(DogsEntry).where(DogsEntry.year == result.year))
        s.add_all(
            [
                DogsEntry(
                    year=result.year,
                    ranking_date=result.ranking_date,
                    symbol=r.symbol,
                    name=r.name,
                    rank=r.rank,
                    price=r.price,
                    annual_dividend=r.annual_dividend,
                    dividend_yield=r.dividend_yield,
                    trailing_12m=r.trailing_12m,
                    payments_per_year=r.payments_per_year,
                    last_ex_date=r.last_ex_date,
                    is_dog=r.is_dog,
                    is_small_dog=r.is_small_dog,
                    note=r.note,
                    source=result.source,
                    created_at=now,
                )
                for r in result.rows
            ]
        )
    return len(result.rows)


def refresh(year: int | None = None, force: bool = False) -> DogsList | None:
    """Build and store the list for `year`, skipping work already done.

    This is what the January job and the Rebuild button both call. With
    `force=False` a year that is already stored is left alone -- the list is
    fixed on its ranking date by definition, so recomputing it can only
    introduce drift from revised data.
    """
    year = year or datetime.now(timezone.utc).year
    if not force and stored_years() and year in stored_years():
        log.info("dogs of the dow %d already stored; skipping", year)
        return None

    result = build_list(year)
    save_list(result)

    from app.trading.risk import audit

    audit(
        "DOGS_REFRESH",
        message=(
            f"Dogs of the Dow {year}: "
            f"{', '.join(r.symbol for r in result.dogs)} "
            f"(ranked on {result.ranking_date})"
        ),
        year=year,
        ranking_date=str(result.ranking_date),
        source=result.source,
        warnings=result.warnings,
    )
    return result


def stored_years() -> list[int]:
    with session_scope() as s:
        return sorted(
            {y for y in s.execute(select(DogsEntry.year).distinct()).scalars()},
            reverse=True,
        )


def stored_list(year: int) -> list[DogsEntry]:
    with session_scope() as s:
        return list(
            s.execute(
                select(DogsEntry)
                .where(DogsEntry.year == year)
                .order_by(DogsEntry.rank)
            ).scalars()
        )


# --------------------------------------------------------------------------
# How the list has actually done
# --------------------------------------------------------------------------
@dataclass
class DogPerformance:
    symbol: str
    start_price: float
    last_price: float
    price_return: float
    dividends_paid: float          # per share, since the ranking date
    total_return: float


def performance(year: int, entries: list[DogsEntry] | None = None) -> dict:
    """Price and total return of a stored list since its ranking date.

    Total return counts the dividends actually paid since the ranking date,
    which is the entire point of the strategy and the number most write-ups
    quietly leave out.
    """
    entries = entries if entries is not None else stored_list(year)
    dogs = [e for e in entries if e.is_dog]
    if not dogs:
        return {"rows": [], "dogs": 0.0, "small_dogs": 0.0, "benchmark": 0.0}

    rank_day = dogs[0].ranking_date
    end = min(
        datetime.now(timezone.utc).replace(tzinfo=None),
        datetime(year, 12, 31, 23, 59),
    )
    start_dt = datetime.combine(rank_day, datetime.min.time())

    from app.data.dividends import fetch_dividends

    paid, _ = fetch_dividends([e.symbol for e in dogs], rank_day, end.date())

    rows: list[DogPerformance] = []
    for e in dogs:
        bars = get_bars(e.symbol, start_dt, end)
        if bars.empty or e.price <= 0:
            continue
        last = float(bars["close"].iloc[-1])
        cash = sum(
            d.amount for d in paid.get(e.symbol, []) if not d.special and d.ex_date > rank_day
        )
        rows.append(
            DogPerformance(
                symbol=e.symbol,
                start_price=e.price,
                last_price=last,
                price_return=last / e.price - 1.0,
                dividends_paid=cash,
                total_return=(last + cash) / e.price - 1.0,
            )
        )

    def _avg(subset: list[DogPerformance]) -> float:
        # Equal weight, which is how the strategy is actually bought.
        return sum(r.total_return for r in subset) / len(subset) if subset else 0.0

    small = {e.symbol for e in entries if e.is_small_dog}
    bench = get_bars("DIA", start_dt, end)
    bench_ret = (
        float(bench["adj_close"].iloc[-1]) / float(bench["adj_close"].iloc[0]) - 1.0
        if len(bench) > 1
        else 0.0
    )

    return {
        "rows": sorted(rows, key=lambda r: r.total_return, reverse=True),
        "dogs": _avg(rows),
        "small_dogs": _avg([r for r in rows if r.symbol in small]),
        "benchmark": bench_ret,
        "as_of": end.date(),
    }
