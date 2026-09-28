"""Position sizing and the risk gate."""
from __future__ import annotations

import pytest

from app.trading.risk import size_position


def test_shares_are_capped_by_the_risk_budget():
    # $10,000 allocation, 1% risk = $100 at risk. Stop is $5 below entry,
    # so 20 shares. The allocation alone would have allowed 100.
    qty, why = size_position("X", price=100.0, stop_price=95.0,
                             allocation_usd=10_000, risk_pct=1.0,
                             cash_available=1_000_000)
    assert qty == 20
    assert "risk budget" in why


def test_a_wider_stop_buys_fewer_shares_for_the_same_risk():
    tight, _ = size_position("X", 100.0, 98.0, 10_000, 1.0, 1_000_000)
    wide, _ = size_position("X", 100.0, 90.0, 10_000, 1.0, 1_000_000)
    assert tight > wide
    # Dollars at risk stay the same; only the share count moves.
    assert tight * 2 == pytest.approx(wide * 10, rel=0.05)


def test_allocation_caps_the_position_even_with_a_tiny_stop_distance():
    # A 1-cent stop would allow a huge share count on risk alone.
    qty, why = size_position("X", 100.0, 99.99, 1_000, 1.0, 1_000_000)
    assert qty == 10          # $1,000 allocation / $100
    assert "allocation" in why


def test_cash_caps_the_position():
    qty, why = size_position("X", 100.0, 95.0, 10_000, 1.0, 500.0)
    assert qty == 5
    assert "cash" in why


def test_stop_at_or_above_entry_is_refused():
    for bad_stop in (100.0, 105.0):
        qty, why = size_position("X", 100.0, bad_stop, 10_000, 1.0, 1_000_000)
        assert qty == 0
        assert "not below the entry" in why


def test_zero_or_negative_stop_is_refused():
    qty, why = size_position("X", 100.0, 0.0, 10_000, 1.0, 1_000_000)
    assert qty == 0


def test_invalid_price_is_refused():
    qty, why = size_position("X", 0.0, 0.0, 10_000, 1.0, 1_000_000)
    assert qty == 0
    assert "no valid price" in why


def test_share_price_above_available_cash_yields_no_trade():
    qty, why = size_position("X", 5_000.0, 4_500.0, 10_000, 1.0, 100.0)
    assert qty == 0
    assert "not enough cash" in why


def test_risk_budget_too_small_for_one_share_is_explained():
    # $1,000 allocation at 0.1% = $1 of risk, but the stop is $50 away.
    qty, why = size_position("X", 100.0, 50.0, 1_000, 0.1, 1_000_000)
    assert qty == 0
    assert "would not buy a single share" in why


def test_sizing_never_returns_fractional_shares():
    qty, _ = size_position("X", 33.33, 30.0, 10_000, 1.0, 1_000_000)
    assert isinstance(qty, int)
