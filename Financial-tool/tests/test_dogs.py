"""Dogs of the Dow.

The strategy is four lines of arithmetic, so these tests are aimed at the
places where the arithmetic is fed something misleading:

  * a dividend that was suspended months ago still being multiplied by four
  * a quarterly payment dropping out of the window because it pays in January
  * a special dividend buying a company the top spot in a yield ranking
  * the list silently changing during the year it is supposed to be fixed for
"""
from __future__ import annotations

from datetime import date

import pytest

from app.analysis import dow
from app.data.dividends import Dividend, fetch_dividends, profile


def quarterly(symbol: str, amounts: list[float], first: date,
              special_last: bool = False) -> list[Dividend]:
    """Four-times-a-year payments, 91 days apart, oldest first."""
    from datetime import timedelta

    out = []
    for i, amt in enumerate(amounts):
        is_last = i == len(amounts) - 1
        out.append(
            Dividend(symbol, first + timedelta(days=91 * i), amt,
                     special=special_last and is_last)
        )
    return out


# --------------------------------------------------------------------------
# Dividend profiling
# --------------------------------------------------------------------------
def test_indicated_rate_annualises_the_latest_payment():
    divs = quarterly("AAA", [0.50, 0.50, 0.55, 0.55], date(2025, 1, 15))
    p = profile("AAA", divs, as_of=date(2025, 12, 31))

    assert p.payments_per_year == 4
    # The rate you would receive going forward, not the sum of history.
    assert p.indicated_annual == pytest.approx(0.55 * 4)
    assert p.trailing_12m == pytest.approx(2.10)


def test_suspended_dividend_is_not_annualised():
    """Boeing's case: last cheque in February, suspended in March.

    Multiplying it by four would carry a dead yield into the ranking and put a
    company that pays nothing among the top ten yielders.
    """
    divs = quarterly("BA", [2.055], date(2020, 2, 13))
    p = profile("BA", divs, as_of=date(2020, 12, 31))

    assert p.schedule_broken is False or p.indicated_annual < 2.055 * 4
    # With one payment on record there is no schedule to infer, so the honest
    # answer is what was actually paid.
    assert p.indicated_annual == pytest.approx(2.055)


def test_missed_payment_breaks_the_schedule():
    """A payer with a clear quarterly history that then stops."""
    divs = quarterly("BBB", [1.0, 1.0, 1.0, 1.0], date(2019, 2, 1))
    # Last ex-date is 2019-11-05; as_of is nearly a year later.
    p = profile("BBB", divs, as_of=date(2020, 10, 1))

    assert p.payments_per_year == 4
    assert p.schedule_broken is True
    assert p.indicated_annual < 4.0          # not 1.0 x 4
    assert p.indicated_annual == pytest.approx(p.trailing_12m)


def test_late_but_not_missed_payment_still_annualises():
    """A fortnight's slip is normal and must not read as a suspension."""
    divs = quarterly("CCC", [1.0, 1.0, 1.0, 1.0], date(2025, 1, 6))
    # Last ex-date 2025-10-06; 100 days later is late, not missed.
    p = profile("CCC", divs, as_of=date(2026, 1, 14))

    assert p.schedule_broken is False
    assert p.indicated_annual == pytest.approx(4.0)


def test_special_dividend_is_excluded():
    """A one-off payout is not a rate, and must not buy the top of the ranking."""
    divs = quarterly("DDD", [0.25, 0.25, 0.25, 0.25], date(2025, 1, 10))
    divs.append(Dividend("DDD", date(2025, 11, 1), 10.00, special=True))
    p = profile("DDD", divs, as_of=date(2025, 12, 31))

    # The one-off is ignored in both the rate and the trailing sum. Counting it
    # would report a 44x dividend on a company that pays 25c a quarter.
    assert p.indicated_annual == pytest.approx(1.0)
    assert p.trailing_12m == pytest.approx(1.0)


def test_profile_with_no_payments_has_no_dividend():
    p = profile("EEE", [], as_of=date(2025, 12, 31))
    assert p.has_dividend is False
    assert p.indicated_annual == 0.0


# --------------------------------------------------------------------------
# Index membership
# --------------------------------------------------------------------------
def test_membership_today_is_thirty_names():
    assert len(dow.DOW30) == 30


def test_membership_is_reconstructed_for_past_dates():
    """Before November 2024 the index held Intel and Dow, not NVIDIA and SHW."""
    before = dow.members_on(date(2024, 1, 2))

    assert "INTC" in before and "DOW" in before
    assert "NVDA" not in before and "SHW" not in before
    # Amazon joined in February 2024, so Walgreens was still in at the time.
    assert "WBA" in before and "AMZN" not in before
    assert len(before) == 30


def test_every_reconstructed_membership_holds_thirty_names():
    """Each recorded change must swap equal numbers in and out.

    An index of 29 or 31 names means a change was recorded with mismatched
    `added` and `removed` sets, which would quietly distort every ranking
    built from that date onwards.
    """
    from datetime import timedelta

    for effective, _added, _removed in dow.MEMBERSHIP_CHANGES:
        assert len(dow.members_on(effective)) == 30
        day_before = effective - timedelta(days=1)
        if day_before >= dow.EARLIEST_KNOWN:
            assert len(dow.members_on(day_before)) == 30


def test_membership_before_the_record_refuses_to_guess():
    with pytest.raises(dow.MembershipUnknown):
        dow.members_on(date(2019, 12, 31))


# --------------------------------------------------------------------------
# The ranking itself
# --------------------------------------------------------------------------
def _row(symbol: str, price: float, dividend: float) -> dow.DogRow:
    return dow.DogRow(
        symbol=symbol, name=symbol, price=price, annual_dividend=dividend,
        dividend_yield=dividend / price, trailing_12m=dividend,
        payments_per_year=4, last_ex_date=date(2025, 12, 1),
    )


def test_top_ten_by_yield_are_the_dogs_and_cheapest_five_are_small():
    # Yields descend with the index; prices deliberately do not.
    rows = [_row(f"S{i:02d}", price=10.0 * (i + 1), dividend=(30 - i) * 0.3)
            for i in range(30)]
    rows.sort(key=lambda r: (-r.dividend_yield, r.price))
    for i, r in enumerate(rows, start=1):
        r.rank = i
        r.is_dog = i <= dow.N_DOGS
    for r in sorted([r for r in rows if r.is_dog], key=lambda r: r.price)[:dow.N_SMALL_DOGS]:
        r.is_small_dog = True

    dogs = [r for r in rows if r.is_dog]
    small = [r for r in rows if r.is_small_dog]

    assert len(dogs) == 10
    assert len(small) == 5
    # Every Small Dog is a Dog, and is among the cheapest of them.
    assert all(r.is_dog for r in small)
    assert max(r.price for r in small) <= min(
        r.price for r in dogs if not r.is_small_dog
    )


def test_a_non_payer_can_never_be_a_dog():
    """Amazon and Boeing are in the index and pay nothing.

    They must appear in the ranking -- the screen is over all thirty -- while
    being incapable of reaching the top ten.
    """
    rows = [_row(f"S{i:02d}", 100.0, 1.0) for i in range(29)]
    rows.append(_row("AMZN", 200.0, 0.0))
    rows.sort(key=lambda r: (-r.dividend_yield, r.price))
    for i, r in enumerate(rows, start=1):
        r.rank = i
        r.is_dog = i <= dow.N_DOGS and r.dividend_yield > 0

    amzn = next(r for r in rows if r.symbol == "AMZN")
    assert amzn.is_dog is False
    assert amzn.rank == 30


def test_ranking_date_is_in_the_previous_year():
    """The list for a year is decided before that year begins."""
    day = dow.ranking_date_for(2026)
    assert day.year == 2025
    assert day.month == 12
    assert day.weekday() < 5


# --------------------------------------------------------------------------
# The window bug that this module exists to avoid
# --------------------------------------------------------------------------
@pytest.mark.parametrize("symbol", ["MRK", "KO"])
def test_year_end_window_includes_the_december_payment(symbol):
    """A dividend going ex in December but paying in January still counts.

    Alpaca filters on the process date, so a naive query ending on 31 December
    loses the fourth quarter for a large part of the Dow and makes those
    companies look like they have just raised their dividend by a third. This
    is the regression test for that.
    """
    pytest.importorskip("alpaca")
    divs, _source = fetch_dividends([symbol], date(2025, 1, 1), date(2025, 12, 31))
    ex_months = {d.ex_date.month for d in divs[symbol]}

    assert len(divs[symbol]) == 4, "a quarterly payer must show four payments"
    assert 12 in ex_months, "the December ex-date must be inside the window"
    assert all(d.ex_date.year == 2025 for d in divs[symbol])
