"""The pool of things the scout is allowed to suggest.

A fixed, curated list rather than a live screener. Three reasons:

  * It is auditable. You can read exactly what the app may ever propose.
  * Free screener APIs are unreliable and inconsistent; a hard-coded list of
    liquid instruments cannot silently start returning penny stocks.
  * It keeps the scan bounded. ~230 symbols of daily bars is a minute of work,
    not an afternoon.

Edit it freely -- it is data, not logic. `(symbol, name, tags)` where tags feed
the plain-English reasoning and the sector grouping on the scout page.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    universe: str
    asset_class: str       # equity | etf | bond | commodity
    sector: str = ""


def _eq(sym: str, name: str, sector: str) -> Instrument:
    return Instrument(sym, name, "stocks", "equity", sector)


def _etf(sym: str, name: str, sector: str = "") -> Instrument:
    return Instrument(sym, name, "sector_etf", "etf", sector)


def _bond(sym: str, name: str, sector: str = "Fixed income") -> Instrument:
    return Instrument(sym, name, "bonds", "bond", sector)


def _cmdty(sym: str, name: str, sector: str = "Commodity") -> Instrument:
    return Instrument(sym, name, "commodities", "commodity", sector)


# --------------------------------------------------------------------------
# US large / mid caps, spread across sectors so the scout cannot end up
# suggesting eight versions of the same bet.
# --------------------------------------------------------------------------
STOCKS: list[Instrument] = [
    # Technology
    _eq("AAPL", "Apple", "Technology"),
    _eq("MSFT", "Microsoft", "Technology"),
    _eq("NVDA", "NVIDIA", "Technology"),
    _eq("AVGO", "Broadcom", "Technology"),
    _eq("AMD", "Advanced Micro Devices", "Technology"),
    _eq("CRM", "Salesforce", "Technology"),
    _eq("ORCL", "Oracle", "Technology"),
    _eq("ADBE", "Adobe", "Technology"),
    _eq("CSCO", "Cisco Systems", "Technology"),
    _eq("ACN", "Accenture", "Technology"),
    _eq("INTU", "Intuit", "Technology"),
    _eq("IBM", "IBM", "Technology"),
    _eq("QCOM", "Qualcomm", "Technology"),
    _eq("TXN", "Texas Instruments", "Technology"),
    _eq("AMAT", "Applied Materials", "Technology"),
    _eq("LRCX", "Lam Research", "Technology"),
    _eq("KLAC", "KLA Corp", "Technology"),
    _eq("MU", "Micron Technology", "Technology"),
    _eq("ANET", "Arista Networks", "Technology"),
    _eq("PANW", "Palo Alto Networks", "Technology"),
    _eq("SNPS", "Synopsys", "Technology"),
    _eq("CDNS", "Cadence Design", "Technology"),
    _eq("NOW", "ServiceNow", "Technology"),
    _eq("MSI", "Motorola Solutions", "Technology"),
    _eq("APH", "Amphenol", "Technology"),
    # Communication services
    _eq("GOOGL", "Alphabet", "Communication"),
    _eq("META", "Meta Platforms", "Communication"),
    _eq("NFLX", "Netflix", "Communication"),
    _eq("DIS", "Walt Disney", "Communication"),
    _eq("CMCSA", "Comcast", "Communication"),
    _eq("T", "AT&T", "Communication"),
    _eq("VZ", "Verizon", "Communication"),
    _eq("TMUS", "T-Mobile US", "Communication"),
    _eq("EA", "Electronic Arts", "Communication"),
    # Consumer discretionary
    _eq("AMZN", "Amazon", "Consumer disc."),
    _eq("TSLA", "Tesla", "Consumer disc."),
    _eq("HD", "Home Depot", "Consumer disc."),
    _eq("MCD", "McDonald's", "Consumer disc."),
    _eq("NKE", "Nike", "Consumer disc."),
    _eq("SBUX", "Starbucks", "Consumer disc."),
    _eq("LOW", "Lowe's", "Consumer disc."),
    _eq("TJX", "TJX Companies", "Consumer disc."),
    _eq("BKNG", "Booking Holdings", "Consumer disc."),
    _eq("ORLY", "O'Reilly Automotive", "Consumer disc."),
    _eq("CMG", "Chipotle", "Consumer disc."),
    _eq("MAR", "Marriott", "Consumer disc."),
    _eq("GM", "General Motors", "Consumer disc."),
    _eq("F", "Ford Motor", "Consumer disc."),
    # Consumer staples
    _eq("PG", "Procter & Gamble", "Staples"),
    _eq("KO", "Coca-Cola", "Staples"),
    _eq("PEP", "PepsiCo", "Staples"),
    _eq("COST", "Costco", "Staples"),
    _eq("WMT", "Walmart", "Staples"),
    _eq("PM", "Philip Morris", "Staples"),
    _eq("MO", "Altria", "Staples"),
    _eq("MDLZ", "Mondelez", "Staples"),
    _eq("CL", "Colgate-Palmolive", "Staples"),
    _eq("KMB", "Kimberly-Clark", "Staples"),
    _eq("GIS", "General Mills", "Staples"),
    _eq("SYY", "Sysco", "Staples"),
    _eq("KHC", "Kraft Heinz", "Staples"),
    # Health care
    _eq("UNH", "UnitedHealth", "Health care"),
    _eq("JNJ", "Johnson & Johnson", "Health care"),
    _eq("LLY", "Eli Lilly", "Health care"),
    _eq("ABBV", "AbbVie", "Health care"),
    _eq("MRK", "Merck", "Health care"),
    _eq("PFE", "Pfizer", "Health care"),
    _eq("TMO", "Thermo Fisher", "Health care"),
    _eq("ABT", "Abbott Laboratories", "Health care"),
    _eq("DHR", "Danaher", "Health care"),
    _eq("AMGN", "Amgen", "Health care"),
    _eq("BMY", "Bristol-Myers Squibb", "Health care"),
    _eq("GILD", "Gilead Sciences", "Health care"),
    _eq("CVS", "CVS Health", "Health care"),
    _eq("ISRG", "Intuitive Surgical", "Health care"),
    _eq("VRTX", "Vertex Pharmaceuticals", "Health care"),
    _eq("REGN", "Regeneron", "Health care"),
    _eq("SYK", "Stryker", "Health care"),
    _eq("BDX", "Becton Dickinson", "Health care"),
    _eq("ZTS", "Zoetis", "Health care"),
    _eq("MDT", "Medtronic", "Health care"),
    # Financials
    _eq("BRK-B", "Berkshire Hathaway B", "Financials"),
    _eq("JPM", "JPMorgan Chase", "Financials"),
    _eq("BAC", "Bank of America", "Financials"),
    _eq("WFC", "Wells Fargo", "Financials"),
    _eq("GS", "Goldman Sachs", "Financials"),
    _eq("MS", "Morgan Stanley", "Financials"),
    _eq("C", "Citigroup", "Financials"),
    _eq("SCHW", "Charles Schwab", "Financials"),
    _eq("BLK", "BlackRock", "Financials"),
    _eq("SPGI", "S&P Global", "Financials"),
    _eq("V", "Visa", "Financials"),
    _eq("MA", "Mastercard", "Financials"),
    _eq("AXP", "American Express", "Financials"),
    _eq("PGR", "Progressive", "Financials"),
    _eq("CB", "Chubb", "Financials"),
    _eq("MMC", "Marsh & McLennan", "Financials"),
    _eq("AON", "Aon", "Financials"),
    _eq("ICE", "Intercontinental Exchange", "Financials"),
    _eq("CME", "CME Group", "Financials"),
    # Industrials
    _eq("CAT", "Caterpillar", "Industrials"),
    _eq("DE", "Deere & Co", "Industrials"),
    _eq("HON", "Honeywell", "Industrials"),
    _eq("GE", "GE Aerospace", "Industrials"),
    _eq("BA", "Boeing", "Industrials"),
    _eq("LMT", "Lockheed Martin", "Industrials"),
    _eq("RTX", "RTX Corp", "Industrials"),
    _eq("NOC", "Northrop Grumman", "Industrials"),
    _eq("GD", "General Dynamics", "Industrials"),
    _eq("UNP", "Union Pacific", "Industrials"),
    _eq("UPS", "United Parcel Service", "Industrials"),
    _eq("FDX", "FedEx", "Industrials"),
    _eq("ETN", "Eaton", "Industrials"),
    _eq("EMR", "Emerson Electric", "Industrials"),
    _eq("ITW", "Illinois Tool Works", "Industrials"),
    _eq("PH", "Parker-Hannifin", "Industrials"),
    _eq("CSX", "CSX Corp", "Industrials"),
    _eq("WM", "Waste Management", "Industrials"),
    _eq("MMM", "3M", "Industrials"),
    # Energy
    _eq("XOM", "Exxon Mobil", "Energy"),
    _eq("CVX", "Chevron", "Energy"),
    _eq("COP", "ConocoPhillips", "Energy"),
    _eq("EOG", "EOG Resources", "Energy"),
    _eq("SLB", "SLB", "Energy"),
    _eq("PSX", "Phillips 66", "Energy"),
    _eq("MPC", "Marathon Petroleum", "Energy"),
    _eq("VLO", "Valero Energy", "Energy"),
    _eq("OXY", "Occidental Petroleum", "Energy"),
    _eq("WMB", "Williams Companies", "Energy"),
    _eq("KMI", "Kinder Morgan", "Energy"),
    # Utilities
    _eq("NEE", "NextEra Energy", "Utilities"),
    _eq("DUK", "Duke Energy", "Utilities"),
    _eq("SO", "Southern Company", "Utilities"),
    _eq("D", "Dominion Energy", "Utilities"),
    _eq("AEP", "American Electric Power", "Utilities"),
    _eq("SRE", "Sempra", "Utilities"),
    _eq("EXC", "Exelon", "Utilities"),
    _eq("XEL", "Xcel Energy", "Utilities"),
    # Real estate
    _eq("PLD", "Prologis", "Real estate"),
    _eq("AMT", "American Tower", "Real estate"),
    _eq("EQIX", "Equinix", "Real estate"),
    _eq("SPG", "Simon Property", "Real estate"),
    _eq("O", "Realty Income", "Real estate"),
    _eq("PSA", "Public Storage", "Real estate"),
    _eq("CCI", "Crown Castle", "Real estate"),
    # Materials
    _eq("LIN", "Linde", "Materials"),
    _eq("SHW", "Sherwin-Williams", "Materials"),
    _eq("APD", "Air Products", "Materials"),
    _eq("ECL", "Ecolab", "Materials"),
    _eq("FCX", "Freeport-McMoRan", "Materials"),
    _eq("NUE", "Nucor", "Materials"),
    _eq("NEM", "Newmont", "Materials"),
    _eq("DOW", "Dow Inc", "Materials"),
]


# --------------------------------------------------------------------------
# Index, sector and factor ETFs. The cheapest way to own "the part of the
# market that is working" without picking a single name.
# --------------------------------------------------------------------------
SECTOR_ETFS: list[Instrument] = [
    _etf("SPY", "S&P 500", "Broad index"),
    _etf("VOO", "Vanguard S&P 500", "Broad index"),
    _etf("QQQ", "Nasdaq 100", "Broad index"),
    _etf("IWM", "Russell 2000 small caps", "Broad index"),
    _etf("DIA", "Dow Jones Industrial", "Broad index"),
    _etf("VTI", "Total US market", "Broad index"),
    _etf("RSP", "S&P 500 equal weight", "Broad index"),
    _etf("XLK", "Technology sector", "Sector"),
    _etf("XLF", "Financials sector", "Sector"),
    _etf("XLV", "Health care sector", "Sector"),
    _etf("XLE", "Energy sector", "Sector"),
    _etf("XLI", "Industrials sector", "Sector"),
    _etf("XLY", "Consumer discretionary", "Sector"),
    _etf("XLP", "Consumer staples", "Sector"),
    _etf("XLU", "Utilities sector", "Sector"),
    _etf("XLB", "Materials sector", "Sector"),
    _etf("XLRE", "Real estate sector", "Sector"),
    _etf("XLC", "Communication services", "Sector"),
    _etf("SMH", "Semiconductors", "Industry"),
    _etf("XBI", "Biotech", "Industry"),
    _etf("ITA", "Aerospace & defence", "Industry"),
    _etf("KRE", "Regional banks", "Industry"),
    _etf("IYT", "Transportation", "Industry"),
    _etf("XHB", "Homebuilders", "Industry"),
    _etf("MTUM", "US momentum factor", "Factor"),
    _etf("QUAL", "US quality factor", "Factor"),
    _etf("USMV", "US minimum volatility", "Factor"),
    _etf("VTV", "US large value", "Factor"),
    _etf("VUG", "US large growth", "Factor"),
    _etf("SCHD", "US dividend quality", "Factor"),
    _etf("VYM", "High dividend yield", "Factor"),
    _etf("NOBL", "Dividend aristocrats", "Factor"),
    _etf("EFA", "Developed markets ex-US", "International"),
    _etf("VEA", "Developed markets", "International"),
    _etf("VWO", "Emerging markets", "International"),
    _etf("EWJ", "Japan", "International"),
    _etf("INDA", "India", "International"),
    _etf("EWG", "Germany", "International"),
    _etf("EWU", "United Kingdom", "International"),
    _etf("VNQ", "US real estate", "Sector"),
]


# --------------------------------------------------------------------------
# Bonds and income. Different job from the equity sleeve: these are here to
# be uncorrelated and to pay you while you wait, not to go up the most.
# --------------------------------------------------------------------------
BONDS: list[Instrument] = [
    _bond("BND", "Total US bond market", "Aggregate"),
    _bond("AGG", "US aggregate bond", "Aggregate"),
    _bond("BNDX", "International bonds hedged", "Aggregate"),
    _bond("SHY", "1-3 year Treasuries", "Treasury short"),
    _bond("SHV", "Under 1 year Treasuries", "Treasury short"),
    _bond("BIL", "1-3 month T-bills", "Treasury short"),
    _bond("VGSH", "Short Treasuries", "Treasury short"),
    _bond("IEF", "7-10 year Treasuries", "Treasury mid"),
    _bond("VGIT", "Intermediate Treasuries", "Treasury mid"),
    _bond("TLT", "20+ year Treasuries", "Treasury long"),
    _bond("EDV", "Extended duration Treasuries", "Treasury long"),
    _bond("VGLT", "Long Treasuries", "Treasury long"),
    _bond("TIP", "Inflation-protected Treasuries", "Inflation linked"),
    _bond("VTIP", "Short TIPS", "Inflation linked"),
    _bond("SCHP", "TIPS", "Inflation linked"),
    _bond("LQD", "Investment-grade corporates", "Corporate"),
    _bond("VCIT", "Intermediate corporates", "Corporate"),
    _bond("VCSH", "Short corporates", "Corporate"),
    _bond("IGSB", "Short investment grade", "Corporate"),
    _bond("HYG", "High-yield corporates", "High yield"),
    _bond("JNK", "High-yield bonds", "High yield"),
    _bond("SJNK", "Short-term high yield", "High yield"),
    _bond("MUB", "Municipal bonds", "Municipal"),
    _bond("VTEB", "Tax-exempt municipals", "Municipal"),
    _bond("EMB", "Emerging market bonds", "Emerging"),
    _bond("PFF", "Preferred shares", "Preferred"),
    _bond("JEPI", "S&P 500 covered-call income", "Income equity"),
    _bond("JEPQ", "Nasdaq covered-call income", "Income equity"),
    _bond("SGOV", "0-3 month Treasuries", "Treasury short"),
]


# --------------------------------------------------------------------------
# Commodities and gold. Mostly here for the weeks when stocks and bonds fall
# together and nothing in the first three lists helps.
# --------------------------------------------------------------------------
COMMODITIES: list[Instrument] = [
    _cmdty("GLD", "Gold bullion", "Precious metals"),
    _cmdty("IAU", "Gold trust", "Precious metals"),
    _cmdty("SLV", "Silver bullion", "Precious metals"),
    _cmdty("GDX", "Gold miners", "Precious metals"),
    _cmdty("PPLT", "Platinum", "Precious metals"),
    _cmdty("DBC", "Broad commodities", "Broad"),
    _cmdty("PDBC", "Commodities no K-1", "Broad"),
    _cmdty("GSG", "GSCI commodity index", "Broad"),
    _cmdty("USO", "Crude oil", "Energy"),
    _cmdty("UNG", "Natural gas", "Energy"),
    _cmdty("XOP", "Oil & gas exploration", "Energy"),
    _cmdty("URA", "Uranium & nuclear", "Energy"),
    _cmdty("ICLN", "Clean energy", "Energy"),
    _cmdty("DBA", "Agriculture", "Agriculture"),
    _cmdty("WOOD", "Timber & forestry", "Agriculture"),
    _cmdty("COPX", "Copper miners", "Industrial metals"),
    _cmdty("LIT", "Lithium & battery tech", "Industrial metals"),
    _cmdty("REMX", "Rare earth metals", "Industrial metals"),
]


UNIVERSES: dict[str, list[Instrument]] = {
    "stocks": STOCKS,
    "sector_etf": SECTOR_ETFS,
    "bonds": BONDS,
    "commodities": COMMODITIES,
}

UNIVERSE_LABELS = {
    "stocks": "US stocks",
    "sector_etf": "Index & sector ETFs",
    "bonds": "Bonds & income",
    "commodities": "Commodities & gold",
}

# The benchmark every regime read is taken against.
BENCHMARK = "SPY"
# A defensive counterweight, used to judge whether bonds are actually working.
DEFENSIVE = "IEF"


def instruments(keys: list[str] | None = None) -> list[Instrument]:
    """Flatten the requested universes into one list."""
    keys = keys or list(UNIVERSES)
    out: list[Instrument] = []
    seen: set[str] = set()
    for key in keys:
        for inst in UNIVERSES.get(key, []):
            if inst.symbol not in seen:
                seen.add(inst.symbol)
                out.append(inst)
    return out


def lookup(symbol: str) -> Instrument | None:
    symbol = symbol.upper()
    for group in UNIVERSES.values():
        for inst in group:
            if inst.symbol == symbol:
                return inst
    return None
