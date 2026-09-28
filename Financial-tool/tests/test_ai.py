"""AI analyst layer.

The important guarantees here are not about prose quality -- they are that the
analyst is fed real numbers, that it stays entirely out of the order path, and
that its output is escaped before it reaches the page.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from app.ai.analyst import build_briefing
from app.ai.providers import LLMError, LLMProvider, LLMReply
from app.models import SignalType
from app.trading.engine import SignalView


class StubProvider(LLMProvider):
    """Records what it was asked, returns a canned reply."""

    name = "stub"
    model = "stub-1"

    def __init__(self, reply: str = "Looks fine.", usable: bool = True):
        self.reply = reply
        self.usable = usable
        self.last_system = ""
        self.last_user = ""

    def available(self):
        return (self.usable, "stub ready" if self.usable else "stub unavailable")

    def complete(self, system: str, user: str) -> LLMReply:
        self.last_system, self.last_user = system, user
        return LLMReply(text=self.reply, model=self.model, provider=self.name)


@pytest.fixture
def bars() -> pd.DataFrame:
    idx = pd.date_range(end=datetime(2026, 9, 11), periods=400, freq="B")
    close = pd.Series([100 + i * 0.4 for i in range(400)], index=idx, dtype="float64")
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "adj_close": close, "volume": 1e6},
        index=idx,
    )


@pytest.fixture
def view() -> SignalView:
    return SignalView(
        symbol="TEST", strategy="", strategy_label="Bollinger Bands",
        signal=SignalType.BUY, price=259.6, bar_ts=datetime(2026, 9, 11),
        stop_price=240.0, target_price=290.0,
        reason="Closed above the upper band", agreement=3, agreement_total=5,
        position_now=1,
    )


def test_briefing_contains_real_computed_numbers(bars, view):
    text = build_briefing("TEST", bars, view, assignment=None)
    for expected in ["SYMBOL: TEST", "RSI(14):", "MACD line", "Bollinger(20,2)",
                     "ATR(14):", "ADX(14):", "SMA50:", "Last close:"]:
        assert expected in text, f"briefing missing {expected!r}"
    # The signal the deterministic engine produced must be stated verbatim.
    assert "Current signal: BUY" in text
    assert "Closed above the upper band" in text


def test_briefing_never_contains_placeholder_nans(bars, view):
    text = build_briefing("TEST", bars, view, assignment=None)
    assert "nan" not in text.lower().replace("n/a", "")


def test_analyst_passes_the_briefing_to_the_model(bars, view):
    from app.ai.analyst import analyse_symbol

    stub = StubProvider("Commentary here.")
    result = analyse_symbol("TEST", bars, view, None, provider=stub)

    assert result.text == "Commentary here."
    assert result.provider == "stub"
    assert "SYMBOL: TEST" in stub.last_user
    # The system prompt must forbid recommendations.
    assert "recommendation" in stub.last_system.lower()


def test_a_specific_question_is_appended(bars, view):
    from app.ai.analyst import analyse_symbol

    stub = StubProvider()
    analyse_symbol("TEST", bars, view, None, provider=stub,
                   question="What would invalidate this?")
    assert "What would invalidate this?" in stub.last_user


def test_unavailable_provider_raises_rather_than_guessing(bars, view):
    from app.ai.analyst import analyse_symbol

    stub = StubProvider(usable=False)
    with pytest.raises(LLMError, match="unavailable"):
        analyse_symbol("TEST", bars, view, None, provider=stub)


def test_missing_provider_raises_a_helpful_error(bars, view, monkeypatch):
    from app.ai import analyst

    monkeypatch.setattr(analyst, "get_provider", lambda: None)
    with pytest.raises(LLMError, match="AI_PROVIDER"):
        analyst.analyse_symbol("TEST", bars, view, None)


def test_model_output_is_escaped_before_rendering():
    """Model output goes straight into the page, so it must be escaped."""
    from app.main import _markdownish

    html = _markdownish('<script>alert("xss")</script> and <img onerror=x>')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "onerror=x>" not in html


def test_markdownish_renders_bold_and_lists():
    from app.main import _markdownish

    assert "<strong>bold</strong>" in _markdownish("this is **bold** text")
    html = _markdownish("- alpha\n- beta")
    assert html.count("<li>") == 2
    assert "<ul>" in html


def test_ai_package_does_not_import_the_order_path():
    """Structural guarantee: the analyst cannot reach a broker.

    If someone later imports the broker or engine into app.ai, this fails --
    which is the point. The model must stay unable to place a trade.
    """
    import pathlib

    ai_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "ai"
    banned = ("app.trading.broker", "app.trading.engine", "app.trading.factory",
              "alpaca", "submit_order", "approve_proposal")
    for py in ai_dir.glob("*.py"):
        source = py.read_text(encoding="utf-8")
        for token in banned:
            assert token not in source, f"{py.name} must not reference {token!r}"


def test_order_path_does_not_import_the_ai_package():
    """The reverse guarantee: no trading decision may depend on the model."""
    import pathlib

    trading = pathlib.Path(__file__).resolve().parent.parent / "app" / "trading"
    for py in trading.glob("*.py"):
        source = py.read_text(encoding="utf-8")
        assert "app.ai" not in source, f"{py.name} must not import app.ai"
