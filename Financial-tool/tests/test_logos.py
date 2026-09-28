"""Symbol logos.

Logos are cosmetic, which is exactly why they must never be able to break a
page, leak a watchlist, or touch a file outside their own cache directory.
What is worth holding onto:

  * a ticker from the URL is never used as a path until it has been validated
  * every symbol renders something, network or no network
  * a provider that fails is asked once, not once per page view
  * turning fetching off means no outbound request at all
"""
from __future__ import annotations

import time

import pytest

from app.data import logos


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Never let a test read or write the real data/logos directory."""
    monkeypatch.setattr(logos, "LOGO_DIR", tmp_path / "logos")
    yield


# --------------------------------------------------------------------------
# A ticker from a URL is untrusted input
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("aapl", "AAPL"),
        (" msft ", "MSFT"),
        ("BRK.B", "BRK.B"),
        ("RDS-A", "RDS-A"),
        ("AAPL.png", "AAPL"),
    ],
)
def test_normalise_accepts_tickers(raw, expected):
    assert logos.normalise(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "../../etc/passwd",
        "..",
        "A/B",
        "A\\B",
        "AAPL;rm -rf",
        "WAYTOOLONGTICKER",
        "<script>",
    ],
)
def test_normalise_rejects_everything_else(raw):
    assert logos.normalise(raw) is None


def test_bad_symbol_still_renders_and_never_hits_the_network(monkeypatch):
    monkeypatch.setattr(
        logos, "_download", lambda s: pytest.fail("traversal attempt reached the network")
    )
    data, media, real = logos.logo("../../etc/passwd")
    assert media == "image/svg+xml" and not real and data


# --------------------------------------------------------------------------
# The monogram fallback
# --------------------------------------------------------------------------
def test_monogram_is_deterministic_and_symbol_specific():
    assert logos.monogram("AAPL") == logos.monogram("aapl")
    assert logos.monogram("AAPL") != logos.monogram("MSFT")


def test_monogram_is_full_bleed():
    """It sits on the light tile real logos need, so it has to cover it."""
    svg = logos.monogram("AAPL").decode()
    assert "<rect width='100' height='100'" in svg
    assert ">AA<" in svg


# --------------------------------------------------------------------------
# Fetch, cache, and failure
# --------------------------------------------------------------------------
def test_a_fetched_logo_is_cached_and_the_provider_asked_once(monkeypatch):
    calls = []

    def fake(symbol):
        calls.append(symbol)
        return b"\x89PNG-pretend", "image/png"

    monkeypatch.setattr(logos, "_download", fake)

    first = logos.logo("AAPL")
    second = logos.logo("aapl")

    assert first == (b"\x89PNG-pretend", "image/png", True)
    assert second == first
    assert calls == ["AAPL"], "second render must come from disk"
    assert (logos.LOGO_DIR / "AAPL.png").exists()


def test_a_failed_lookup_falls_back_and_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(logos, "_download", lambda s: calls.append(s))

    data, media, real = logos.logo("NOPE")
    assert media == "image/svg+xml" and not real and data

    logos.logo("NOPE")
    assert calls == ["NOPE"], "a miss is remembered, not re-asked"


def test_a_stale_miss_is_retried(monkeypatch):
    monkeypatch.setattr(logos, "_download", lambda s: None)
    logos.logo("LATER")
    marker = logos.LOGO_DIR / "LATER.miss"
    old = time.time() - logos._MISS_TTL - 60
    import os

    os.utime(marker, (old, old))

    monkeypatch.setattr(logos, "_download", lambda s: (b"png", "image/png"))
    assert logos.logo("LATER")[2] is True


def test_disabling_logos_makes_no_request(monkeypatch):
    monkeypatch.setattr(logos.settings, "logos_enabled", False)
    monkeypatch.setattr(
        logos, "_download", lambda s: pytest.fail("fetched with logos disabled")
    )
    data, media, real = logos.logo("AAPL")
    assert media == "image/svg+xml" and not real and data


def test_a_provider_exception_cannot_break_a_page(monkeypatch):
    def boom(symbol):
        raise RuntimeError("provider on fire")

    monkeypatch.setattr(logos, "_download", boom)
    data, media, real = logos.logo("AAPL")
    assert media == "image/svg+xml" and not real and data


def test_download_swallows_network_errors(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise OSError("no route to host")

    import httpx

    monkeypatch.setattr(httpx, "Client", Boom)
    assert logos._download("AAPL") is None


def test_clear_cache_removes_hits_and_misses(monkeypatch):
    monkeypatch.setattr(logos, "_download", lambda s: (b"png", "image/png"))
    logos.logo("AAPL")
    monkeypatch.setattr(logos, "_download", lambda s: None)
    logos.logo("NOPE")

    assert logos.cache_stats()["real"] == 1
    assert logos.cache_stats()["miss"] == 1
    assert logos.clear_cache() == 2
    assert logos.cache_stats() == {
        "real": 0,
        "miss": 0,
        "dir": str(logos.LOGO_DIR),
    }
