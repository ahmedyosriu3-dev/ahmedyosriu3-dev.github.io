"""Does this instrument actually repeat itself?

Everyone can point at a chart and see a rhythm in it. The eye is very good at
finding one and very bad at telling a real rhythm from noise, which is why
"this stock always bounces back" is the most expensive sentence in investing.

This module tries to answer the question with statistics that can come back
negative, and most of the time they do:

  variance ratio     Do moves reverse or continue? A ratio below 1 means a
                     rise tends to be followed by a fall (mean reversion); a
                     ratio above 1 means moves persist (trending). Reported
                     with the Lo-MacKinlay heteroskedasticity-robust z-score,
                     so "1.03" and "meaningfully above 1" stay separable.

  dip and recovery   How often it has fallen 10% or 20% below its 52-week
                     high, how often it regained that level within a year, and
                     how long that took. This is the measurable version of
                     "it drops and surges back".

  dip edge           Forward return after a fall, against forward return at
                     any other time. If buying weakness works on an
                     instrument, this is where it shows up.

  seasonality        Calendar-month behaviour, with the hit rate next to the
                     average so that one enormous January cannot masquerade as
                     a reliable one.

Every measure is also computed on the first and second halves of the history
separately. A pattern that only exists in one half is a pattern you found by
looking, and `split_agrees` is False for it. That column is the point of the
module: it is the difference between a finding and a coincidence.

Two honest caveats that no amount of arithmetic removes:

  * The forward-return measures use overlapping windows, so their effective
    sample size is far smaller than the row count suggests. They are reported
    without p-values on purpose -- treat them as descriptive.
  * Everything here is in-sample by construction. Survivorship is real: these
    are today's well-known names, which is exactly the set that did well.

Nothing here places, sizes, or proposes an order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.data.store import get_bars

log = logging.getLogger(__name__)

# Ten years is long enough to hold a full cycle and short enough that the
# company at the end is recognisably the one at the start.
LOOKBACK_DAYS = 365 * 12
MIN_BARS = 756                    # three years; below this nothing is claimed

VR_HORIZONS = (5, 21, 63)         # a week, a month, a quarter
DIP_THRESHOLDS = (-0.10, -0.20)
FORWARD_WINDOW = 126              # six months, in trading days
DIP_TRIGGER = -0.10               # "on sale" for the conditional-return test

# Falls are measured against the trailing 52-week high, not the highest price
# ever seen. Against an all-time high, anything in a long structural decline --
# crude oil since 2014, long Treasuries since 2020 -- registers as a single
# twelve-year episode that never recovered, and every real dip and rebound
# inside that decline disappears. A rolling peak asks the question people
# actually mean: it fell away from where it recently was, did it come back?
PEAK_WINDOW = 252

# How long a fall is given to resolve before it is called unrecovered. Without
# a bound, "did it come back?" has no answer until the end of the data, and the
# answer for the last fall in the series is always no.
RECOVERY_HORIZON = 252

# Below this the z-score is noise dressed as a finding.
Z_SIGNIFICANT = 2.0


# --------------------------------------------------------------------------
# Variance ratio
# --------------------------------------------------------------------------
@dataclass
class VarianceRatio:
    horizon: int
    ratio: float
    z: float

    @property
    def verdict(self) -> str:
        if abs(self.z) < Z_SIGNIFICANT:
            return "random walk"
        return "mean reverting" if self.ratio < 1 else "trending"

    @property
    def significant(self) -> bool:
        return abs(self.z) >= Z_SIGNIFICANT


def variance_ratio(log_returns: pd.Series, horizon: int) -> VarianceRatio | None:
    """Lo-MacKinlay variance ratio with a heteroskedasticity-robust z-score.

    The robust version matters here. Under the simpler homoskedastic
    statistic, any asset with volatility clustering -- which is every asset --
    looks significantly mean reverting, and the whole module would print
    exciting findings for a random walk.
    """
    r = log_returns.dropna().to_numpy(dtype="float64")
    n = r.size
    if n < horizon * 10:
        return None

    mu = r.mean()
    dev = r - mu
    var_1 = float((dev ** 2).sum() / (n - 1))
    if var_1 <= 0:
        return None

    # Overlapping q-period sums, with the Lo-MacKinlay unbiased denominator.
    rolled = np.convolve(r, np.ones(horizon), mode="valid")
    m = horizon * (n - horizon + 1) * (1.0 - horizon / n)
    if m <= 0:
        return None
    var_q = float(((rolled - horizon * mu) ** 2).sum() / m)

    # `m` already carries the factor of q, so this is the whole ratio. Dividing
    # by the horizon again here scales every result by 1/q, which turns a
    # random walk into a screaming mean reverter at z = -16.
    ratio = var_q / var_1

    # Heteroskedasticity-robust variance of the statistic.
    dev_sq = dev ** 2
    denom = float(dev_sq.sum() ** 2)
    if denom <= 0:
        return None
    theta = 0.0
    for j in range(1, horizon):
        delta = float((dev_sq[j:] * dev_sq[:-j]).sum()) / denom
        theta += ((2.0 * (horizon - j) / horizon) ** 2) * delta
    if theta <= 0:
        return None

    return VarianceRatio(horizon, ratio, (ratio - 1.0) / float(np.sqrt(theta)))


# --------------------------------------------------------------------------
# Falls and recoveries
# --------------------------------------------------------------------------
@dataclass
class DipProfile:
    threshold: float
    episodes: int = 0
    recovered: int = 0
    median_days_to_recover: float = 0.0
    median_extra_fall: float = 0.0      # how much further it fell after crossing
    worst_days_to_recover: int = 0
    still_underwater: bool = False

    @property
    def recovery_rate(self) -> float:
        return self.recovered / self.episodes if self.episodes else 0.0


def dip_profile(close: pd.Series, threshold: float) -> DipProfile:
    """Every fall past `threshold` below the 52-week high, and what came next.

    An episode opens the first day price closes `threshold` below its trailing
    peak and runs until it climbs back above that threshold, so one decline is
    one episode however many times it wobbles across the line inside it.

    It counts as **recovered** only if, within a year of the crossing, price
    regained the level it fell away from -- the trailing peak fixed at the
    moment of the fall, not one that kept rising afterwards. An instrument that
    is permanently 30% below where it was is not recovering, and this must not
    report it as though it were.
    """
    prices = close.dropna()
    if prices.empty:
        return DipProfile(threshold)

    peak = prices.rolling(PEAK_WINDOW, min_periods=1).max()
    values = prices.to_numpy()
    peaks = peak.to_numpy()

    profile = DipProfile(threshold)
    days_to_recover: list[int] = []
    extra_falls: list[float] = []

    i, n = 0, values.size
    while i < n:
        if peaks[i] <= 0 or values[i] / peaks[i] - 1.0 > threshold:
            i += 1
            continue

        # An episode is one contiguous stretch spent below the threshold. It is
        # closed by climbing back above it, not by a full recovery: if only a
        # return to the old peak ended an episode, an instrument in a long
        # decline would have exactly one episode, open forever, and the dozen
        # real falls inside it would never be counted.
        start = i
        target = peaks[i]
        trough = values[i]

        horizon = min(n, start + RECOVERY_HORIZON)
        j = start
        while j < horizon and values[j] < target:
            trough = min(trough, values[j])
            j += 1

        profile.episodes += 1
        extra_falls.append(trough / target - 1.0 - threshold)

        if j < horizon:
            profile.recovered += 1
            days_to_recover.append(j - start)
        elif horizon >= n:
            # Ran out of data rather than out of patience.
            profile.still_underwater = True

        k = start
        while k < n and (peaks[k] <= 0 or values[k] / peaks[k] - 1.0 <= threshold):
            k += 1
        i = max(k, start + 1)

    if days_to_recover:
        profile.median_days_to_recover = float(np.median(days_to_recover))
        profile.worst_days_to_recover = int(max(days_to_recover))
    if extra_falls:
        profile.median_extra_fall = float(np.median(extra_falls))
    return profile


# --------------------------------------------------------------------------
# Is weakness worth buying?
# --------------------------------------------------------------------------
@dataclass
class DipEdge:
    forward_days: int
    mean_after_dip: float = 0.0
    mean_otherwise: float = 0.0
    samples_after_dip: int = 0
    hit_rate_after_dip: float = 0.0

    @property
    def edge(self) -> float:
        return self.mean_after_dip - self.mean_otherwise


def dip_edge(close: pd.Series, trigger: float = DIP_TRIGGER,
             forward: int = FORWARD_WINDOW) -> DipEdge:
    """Forward return when the instrument is on sale, versus when it is not.

    Overlapping windows, so no significance is claimed -- see the module
    docstring. A positive edge says weakness has historically been rewarded on
    this instrument; a negative one says weakness has historically been a
    warning, which is the more common answer.
    """
    prices = close.dropna()
    result = DipEdge(forward)
    if len(prices) < forward + 252:
        return result

    peak = prices.rolling(PEAK_WINDOW, min_periods=1).max()
    drawdown = (prices / peak - 1.0).to_numpy()
    values = prices.to_numpy()

    fwd = values[forward:] / values[:-forward] - 1.0
    dd = drawdown[: fwd.size]

    on_sale = dd <= trigger
    if on_sale.any():
        result.mean_after_dip = float(fwd[on_sale].mean())
        result.samples_after_dip = int(on_sale.sum())
        result.hit_rate_after_dip = float((fwd[on_sale] > 0).mean())
    if (~on_sale).any():
        result.mean_otherwise = float(fwd[~on_sale].mean())
    return result


# --------------------------------------------------------------------------
# Seasonality
# --------------------------------------------------------------------------
@dataclass
class MonthStat:
    month: int
    mean: float
    hit_rate: float
    years: int

    @property
    def label(self) -> str:
        return ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December"][self.month]


def monthly_stats(close: pd.Series) -> list[MonthStat]:
    """Average return in each calendar month, with how often it was positive."""
    monthly = close.resample("ME").last().pct_change().dropna()
    out: list[MonthStat] = []
    for month, group in monthly.groupby(monthly.index.month):
        if len(group) < 3:
            continue
        out.append(
            MonthStat(
                month=int(month),
                mean=float(group.mean()),
                hit_rate=float((group > 0).mean()),
                years=int(len(group)),
            )
        )
    return sorted(out, key=lambda m: m.month)


# --------------------------------------------------------------------------
# One instrument, end to end
# --------------------------------------------------------------------------
@dataclass
class PatternReport:
    symbol: str
    name: str = ""
    bars: int = 0
    first_date: str = ""
    last_date: str = ""
    annualised_return: float = 0.0
    volatility: float = 0.0

    ratios: list[VarianceRatio] = field(default_factory=list)
    dips: list[DipProfile] = field(default_factory=list)
    edge: DipEdge | None = None
    months: list[MonthStat] = field(default_factory=list)

    split_agrees: bool = False
    split_detail: str = ""
    headline: str = ""

    def ratio_at(self, horizon: int) -> VarianceRatio | None:
        return next((r for r in self.ratios if r.horizon == horizon), None)

    @property
    def best_month(self) -> MonthStat | None:
        return max(self.months, key=lambda m: m.mean) if self.months else None

    @property
    def worst_month(self) -> MonthStat | None:
        return min(self.months, key=lambda m: m.mean) if self.months else None


def _split_check(log_rets: pd.Series, horizon: int) -> tuple[bool, str]:
    """Does the variance ratio say the same thing in both halves of history?"""
    half = len(log_rets) // 2
    first = variance_ratio(log_rets.iloc[:half], horizon)
    second = variance_ratio(log_rets.iloc[half:], horizon)
    if first is None or second is None:
        return False, "not enough history to split"

    same_side = (first.ratio < 1) == (second.ratio < 1)
    detail = (
        f"first half {first.ratio:.2f} (z {first.z:+.1f}), "
        f"second half {second.ratio:.2f} (z {second.z:+.1f})"
    )
    if not same_side:
        return False, detail + " — the two halves disagree"
    if not (first.significant and second.significant):
        return False, detail + " — same direction, but not significant in both"
    return True, detail + " — same direction and significant in both"


def _headline(report: PatternReport) -> str:
    month = report.ratio_at(21)
    dip10 = next((d for d in report.dips if d.threshold == -0.10), None)

    if month and month.significant and report.split_agrees:
        if month.ratio < 1:
            base = (
                "Genuinely mean reverting on a monthly horizon, and it holds up "
                "in both halves of the history"
            )
        else:
            base = (
                "Genuinely trend-persistent on a monthly horizon, and it holds "
                "up in both halves of the history"
            )
    elif month and month.significant:
        base = (
            "Looks "
            + ("mean reverting" if month.ratio < 1 else "trending")
            + " overall, but the two halves of its history do not agree"
        )
    else:
        base = "Indistinguishable from a random walk at these horizons"

    if dip10 and dip10.episodes:
        base += (
            f". Fell 10% from a high {dip10.episodes} times; "
            f"{dip10.recovery_rate * 100:.0f}% of those recovered"
        )
        if dip10.recovered:
            base += f", typically in {dip10.median_days_to_recover / 21:.0f} months"
    return base + "."


def analyse(symbol: str, name: str = "", bars: pd.DataFrame | None = None) -> PatternReport | None:
    """Everything this module can say about one instrument."""
    if bars is None:
        end = datetime.now(timezone.utc).replace(tzinfo=None)
        bars = get_bars(symbol, end - timedelta(days=LOOKBACK_DAYS), end)

    if bars is None or len(bars) < MIN_BARS:
        return None

    close = bars["adj_close"].astype(float).dropna()
    if close.empty or float(close.iloc[0]) <= 0:
        return None

    simple = close.pct_change().dropna()
    log_rets = np.log(close / close.shift(1)).dropna()
    years = len(close) / 252.0

    report = PatternReport(
        symbol=symbol,
        name=name,
        bars=len(close),
        first_date=str(close.index.min().date()),
        last_date=str(close.index.max().date()),
        annualised_return=float((close.iloc[-1] / close.iloc[0]) ** (1 / years) - 1)
        if years > 0 else 0.0,
        volatility=float(simple.std() * np.sqrt(252)),
    )

    report.ratios = [
        vr for vr in (variance_ratio(log_rets, h) for h in VR_HORIZONS) if vr
    ]
    report.dips = [dip_profile(close, t) for t in DIP_THRESHOLDS]
    report.edge = dip_edge(close)
    report.months = monthly_stats(close)
    report.split_agrees, report.split_detail = _split_check(log_rets, 21)
    report.headline = _headline(report)
    return report


def scan(symbols: list[tuple[str, str]]) -> list[PatternReport]:
    """Analyse a list of (symbol, name) pairs, skipping what has no history."""
    out: list[PatternReport] = []
    for symbol, name in symbols:
        try:
            report = analyse(symbol, name)
        except Exception as exc:
            log.warning("pattern analysis failed for %s: %s", symbol, exc)
            continue
        if report is not None:
            out.append(report)
        else:
            log.info("skipped %s: not enough history", symbol)
    return out
