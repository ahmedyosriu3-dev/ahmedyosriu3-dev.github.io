"""Screen well-known instruments for repeatable behaviour.

    .venv\\Scripts\\python.exe scripts\\pattern_scan.py
    .venv\\Scripts\\python.exe scripts\\pattern_scan.py SPY GLD USO

Prints one row per instrument: whether moves reverse or persist, whether that
holds in both halves of its history, how its falls have resolved, and whether
buying it weak has paid. Sorted so that the instruments with a pattern that
survives the split-sample check come first.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Run from anywhere: the project root is this file's parent's parent.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402, F401  -- bootstraps the TLS trust store before any fetch
from app.analysis.patterns import PatternReport, scan  # noqa: E402
from app.db import init_db  # noqa: E402

# A deliberately recognisable list: the names someone would actually think of
# when asking "does this thing move in a pattern?", across the asset classes
# where the answer differs most.
WELL_KNOWN: list[tuple[str, str]] = [
    # Index and factor
    ("SPY", "S&P 500"),
    ("QQQ", "Nasdaq 100"),
    ("DIA", "Dow 30"),
    ("IWM", "Russell 2000"),
    ("RSP", "S&P 500 equal weight"),
    # Mega-cap equities
    ("AAPL", "Apple"),
    ("MSFT", "Microsoft"),
    ("NVDA", "NVIDIA"),
    ("AMZN", "Amazon"),
    ("GOOGL", "Alphabet"),
    ("META", "Meta Platforms"),
    ("TSLA", "Tesla"),
    # Defensives and old-economy names
    ("KO", "Coca-Cola"),
    ("PG", "Procter & Gamble"),
    ("JNJ", "Johnson & Johnson"),
    ("WMT", "Walmart"),
    ("MCD", "McDonald's"),
    ("VZ", "Verizon"),
    ("BA", "Boeing"),
    ("DIS", "Walt Disney"),
    ("INTC", "Intel"),
    ("JPM", "JPMorgan Chase"),
    ("XOM", "Exxon Mobil"),
    ("CVX", "Chevron"),
    # Sectors
    ("XLE", "Energy sector"),
    ("XLF", "Financials sector"),
    ("XLK", "Technology sector"),
    ("XLU", "Utilities sector"),
    ("XLP", "Consumer staples"),
    ("SMH", "Semiconductors"),
    ("XBI", "Biotech"),
    ("KRE", "Regional banks"),
    # Commodities, metals and energy -- where real mean reversion tends to live
    ("GLD", "Gold"),
    ("SLV", "Silver"),
    ("GDX", "Gold miners"),
    ("USO", "Crude oil"),
    ("UNG", "Natural gas"),
    ("XOP", "Oil & gas exploration"),
    ("COPX", "Copper miners"),
    ("URA", "Uranium"),
    ("DBA", "Agriculture"),
    # Bonds
    ("TLT", "20+ year Treasuries"),
    ("IEF", "7-10 year Treasuries"),
    ("HYG", "High-yield corporates"),
]


def row(r: PatternReport) -> str:
    month = r.ratio_at(21)
    dip10 = next((d for d in r.dips if d.threshold == -0.10), None)
    edge = r.edge

    vr_text = f"{month.ratio:>5.2f} z{month.z:>+6.1f}" if month else "     -       "
    verdict = month.verdict if month else "-"
    split = "yes" if r.split_agrees else "no"
    dip_text = (
        f"{dip10.episodes:>3} {dip10.recovery_rate * 100:>4.0f}% "
        f"{dip10.median_days_to_recover / 21:>5.1f}m"
        if dip10 and dip10.episodes else "  -    -      -"
    )
    edge_text = f"{edge.edge * 100:>+6.1f}%" if edge and edge.samples_after_dip else "     -"

    return (
        f"{r.symbol:<6} {r.name[:22]:<22} {vr_text}  {verdict:<14} {split:<5} "
        f"{dip_text}  {edge_text}"
    )


def main() -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    init_db()

    args = [a.upper() for a in sys.argv[1:]]
    universe = (
        [(s, s) for s in args] if args
        else WELL_KNOWN
    )

    print(f"Analysing {len(universe)} instruments — this fetches years of "
          f"history on a cold cache.\n")
    reports = scan(universe)

    # Anything whose pattern survives the split-sample check first, then by how
    # far its variance ratio sits from 1.
    def sort_key(r: PatternReport) -> tuple:
        month = r.ratio_at(21)
        return (
            not r.split_agrees,
            -abs(month.z) if month else 0.0,
        )

    reports.sort(key=sort_key)

    header = (
        f"{'sym':<6} {'name':<22} {'VR(1m)':>6} {'z':>6}  {'reads as':<14} "
        f"{'both':<5} {'n':>3} {'rec':>4} {'time':>6}  {'edge':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in reports:
        print(row(r))

    print(
        "\nVR(1m)  variance ratio at a one-month horizon; <1 reverses, >1 persists"
        "\nz       Lo-MacKinlay robust z-score; |z| < 2 is indistinguishable from noise"
        "\nboth    does the same reading hold in BOTH halves of the history?"
        "\nn/rec   falls of 10%+ from a high / share that got back to the old peak"
        "\ntime    median months from crossing -10% back to the previous peak"
        "\nedge    forward 6m return when 10% off the high, minus at other times"
        "\n\nOverlapping windows: 'edge' is descriptive, not a significance test."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
