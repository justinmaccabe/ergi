"""Returns-based factor exposure. Pure: numpy, pandas and `desk.domain` only.

The regression is ordinary least squares of a holding's excess return on a set
of factor returns. Nothing here knows where the factors came from, which is the
point — the Ken French download lives in `desk.data.providers.factors`, and this
module is importable in a notebook with two frames and no network.

Three properties are deliberate, because each is a way the naive version of this
calculation misleads:

  * **A short history yields no estimate, not a noisy one.** Below
    `MIN_MONTHS` observations the fit is not reported at all. A beta from eight
    monthly points has a confidence interval wide enough to contain nearly any
    conclusion, and rendering it next to a five-year estimate implies they are
    the same kind of number.

  * **Currency is the caller's problem, and the caller must get it right.**
    Fama-French factors are denominated in USD. Regressing a CAD-denominated
    return series on them attributes the CAD/USD move to the market factor,
    which shows up as a spuriously low market beta. `factor_exposure` takes
    returns it is told are already in the factors' currency and says so; the
    conversion happens in the service layer where the FX series lives.

  * **Unexplained weight is reported, never redistributed.** A holding with no
    return history (a private mark, a fund younger than the window) is excluded
    from the weighted average and its weight is returned as `unattributed`.
    Renormalising over what remains would silently restate a 70%-covered
    portfolio as a 100%-covered one.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

# The canonical five factors plus momentum. Order is fixed because it is the
# order the chart and the table render in, and a factor loading is meaningless
# without knowing which factor it belongs to.
FACTORS: tuple[str, ...] = ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom")

# Two years of monthly observations. Below this an OLS fit on six regressors is
# fitting noise: with 12 points and 7 parameters there is almost no residual
# degree of freedom left.
MIN_MONTHS = 24


@dataclass(frozen=True, slots=True)
class FactorFit:
    """One holding's regression against the factor set."""

    ticker: str
    alpha: float
    betas: Mapping[str, float]
    r_squared: float
    months: int
    weight: float = 0.0
    # The span actually regressed, which for a young fund is much shorter than
    # the factor library's own range.
    start: dt.date | None = None
    end: dt.date | None = None

    @property
    def annualised_alpha(self) -> float:
        """Monthly alpha compounded to a year, not multiplied by twelve."""
        return (1.0 + self.alpha) ** 12 - 1.0


@dataclass(frozen=True, slots=True)
class FactorExposure:
    """Portfolio-level loadings, the per-holding fits behind them, and coverage."""

    factors: tuple[str, ...]
    portfolio: Mapping[str, float]
    fits: tuple[FactorFit, ...]
    unattributed: float
    excluded: tuple[str, ...]
    window: tuple[str, str] | None

    @property
    def is_empty(self) -> bool:
        return not self.fits


def monthly_returns(prices: pd.Series) -> pd.Series:
    """Month-end simple returns from a daily price series.

    Indexed to month-end timestamps so it aligns with the factor frame, whose
    index is the month the return was earned in.
    """
    if prices is None or prices.empty:
        return pd.Series(dtype=float)
    monthly = prices.resample("ME").last().dropna()
    returns = monthly.pct_change().dropna()
    if returns.empty:
        return returns
    returns.index = returns.index.to_period("M").to_timestamp("M")
    return returns


def regress(returns: pd.Series, factors: pd.DataFrame) -> FactorFit | None:
    """OLS of excess return on the factor set. None when the history is short.

    `factors` must carry the six factor columns plus `RF`, all as monthly
    decimals, and `returns` must already be denominated in the factors'
    currency. The intercept is the monthly alpha.
    """
    if returns is None or returns.empty or factors is None or factors.empty:
        return None
    missing = [c for c in (*FACTORS, "RF") if c not in factors.columns]
    if missing:
        return None

    joined = factors.copy()
    joined["ret"] = returns
    joined = joined.dropna(subset=["ret", "RF", *FACTORS])
    if len(joined) < MIN_MONTHS:
        return None

    y = (joined["ret"] - joined["RF"]).to_numpy(dtype=float)
    design = np.column_stack(
        [np.ones(len(joined))] + [joined[f].to_numpy(dtype=float) for f in FACTORS]
    )
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    residual = y - design @ beta
    total_variance = float(np.var(y))
    r_squared = 1.0 - float(np.var(residual)) / total_variance if total_variance > 0 else 0.0
    span = pd.DatetimeIndex(joined.index)
    return FactorFit(
        ticker="",
        alpha=float(beta[0]),
        betas={f: float(b) for f, b in zip(FACTORS, beta[1:], strict=True)},
        r_squared=r_squared,
        months=len(joined),
        start=span.min().date(),
        end=span.max().date(),
    )


def factor_exposure(
    returns_by_ticker: Mapping[str, pd.Series],
    factors: pd.DataFrame,
    weights: Mapping[str, float],
) -> FactorExposure:
    """Market-value-weighted factor loadings for a portfolio.

    `returns_by_ticker` holds monthly returns already converted to the factors'
    currency. `weights` is market value per ticker over the *whole* portfolio,
    including holdings absent from `returns_by_ticker` — that is what makes the
    reported `unattributed` share meaningful rather than tautologically zero.
    """
    total_weight = sum(w for w in weights.values() if w > 0)
    fits: list[FactorFit] = []
    excluded: list[str] = []

    for ticker in sorted(weights):
        series = returns_by_ticker.get(ticker)
        fit = regress(series, factors) if series is not None else None
        if fit is None:
            excluded.append(ticker)
            continue
        fits.append(
            FactorFit(
                ticker=ticker,
                alpha=fit.alpha,
                betas=fit.betas,
                r_squared=fit.r_squared,
                months=fit.months,
                start=fit.start,
                end=fit.end,
            )
        )

    attributed = sum(weights.get(f.ticker, 0.0) for f in fits)
    # Weights are renormalised across the *fitted* holdings so the portfolio
    # loading is a true weighted average of what was measured. The share that
    # could not be measured travels separately, in `unattributed`.
    weighted = tuple(
        FactorFit(
            ticker=f.ticker,
            alpha=f.alpha,
            betas=f.betas,
            r_squared=f.r_squared,
            months=f.months,
            weight=(weights.get(f.ticker, 0.0) / attributed) if attributed > 0 else 0.0,
            start=f.start,
            end=f.end,
        )
        for f in fits
    )
    portfolio = {
        factor: sum(f.weight * f.betas.get(factor, 0.0) for f in weighted) for factor in FACTORS
    }
    # The window is the span actually regressed, not the factor library's range.
    # Reporting the latter would claim three decades behind a loading estimated
    # from four years, which is the more flattering number and the wrong one.
    starts = [f.start for f in weighted if f.start is not None]
    ends = [f.end for f in weighted if f.end is not None]
    window = (
        (min(starts).strftime("%b %Y"), max(ends).strftime("%b %Y"))
        if starts and ends
        else None
    )
    return FactorExposure(
        factors=FACTORS,
        portfolio=portfolio,
        fits=tuple(sorted(weighted, key=lambda f: -f.weight)),
        unattributed=((total_weight - attributed) / total_weight) if total_weight > 0 else 0.0,
        excluded=tuple(excluded),
        window=window,
    )


def to_usd(prices: pd.Series, usd_per_unit: pd.Series) -> pd.Series:
    """Convert a price series into USD given a rate series.

    Present here rather than in the service layer because it is arithmetic on
    two aligned series and belongs with the regression it exists to feed. The
    rate is quoted as USD per unit of the price's currency, forward-filled onto
    the price index so a market holiday in one series does not drop the day.
    """
    if prices is None or prices.empty or usd_per_unit is None or usd_per_unit.empty:
        return pd.Series(dtype=float) if prices is None else prices
    aligned = usd_per_unit.reindex(prices.index).ffill().bfill()
    return (prices * aligned).dropna()


def contribution(exposure: FactorExposure, factor: str) -> tuple[tuple[str, float], ...]:
    """Each holding's share of one portfolio-level loading, largest first.

    The portfolio loading is a weighted sum, so it decomposes exactly: these
    contributions add up to `exposure.portfolio[factor]`.
    """
    rows = tuple(
        (f.ticker, f.weight * f.betas.get(factor, 0.0))
        for f in exposure.fits
    )
    return tuple(sorted(rows, key=lambda pair: -abs(pair[1])))


def summarise(exposure: FactorExposure) -> pd.DataFrame:
    """The per-holding fits as a frame, for display."""
    if exposure.is_empty:
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "Ticker": f.ticker,
                "Weight": f.weight,
                "R²": f.r_squared,
                "Months": f.months,
                "Alpha (monthly)": f.alpha,
                **{name: f.betas.get(name, float("nan")) for name in exposure.factors},
            }
            for f in exposure.fits
        ]
    )


def factor_names() -> Mapping[str, str]:
    """What each factor is, in words, for the UI to caption."""
    return {
        "Mkt-RF": "Market minus the risk-free rate — broad equity exposure.",
        "SMB": "Small minus big — a positive loading tilts toward smaller companies.",
        "HML": "High minus low book-to-market — positive is a value tilt.",
        "RMW": "Robust minus weak profitability — positive favours profitable firms.",
        "CMA": "Conservative minus aggressive investment — positive favours low reinvestment.",
        "Mom": "Winners minus losers over the prior year — positive is a momentum tilt.",
    }


def excluded_reason(
    ticker: str, returns_by_ticker: Mapping[str, pd.Series], factors: pd.DataFrame
) -> str:
    """Why a holding produced no fit, so the UI can say rather than just omit."""
    series = returns_by_ticker.get(ticker)
    if series is None or series.empty:
        return "no return history"
    overlap = len(series.index.intersection(factors.index)) if not factors.empty else 0
    if overlap < MIN_MONTHS:
        return f"{overlap} months of overlap; {MIN_MONTHS} needed"
    return "regression did not converge"


def blended_r_squared(exposure: FactorExposure) -> float | None:
    """Weighted mean R² across the fitted holdings.

    A single figure for how much of the portfolio's movement the factor model
    explains at all. A low value means the loadings above describe a small part
    of what actually happened, and should be read accordingly.
    """
    if exposure.is_empty:
        return None
    total = sum(f.weight for f in exposure.fits)
    if total <= 0:
        return None
    return sum(f.weight * f.r_squared for f in exposure.fits) / total


def annualised_alpha(exposure: FactorExposure) -> float | None:
    """Weighted portfolio alpha, compounded to a year."""
    if exposure.is_empty:
        return None
    monthly = sum(f.weight * f.alpha for f in exposure.fits)
    return (1.0 + monthly) ** 12 - 1.0


def tilt_summary(exposure: FactorExposure, threshold: float = 0.1) -> tuple[str, ...]:
    """Plain-language readings of the loadings that are large enough to mean something.

    Below the threshold a loading is not distinguishable from zero at the sample
    sizes involved here, and describing it as a tilt overstates the evidence.
    """
    wording = {
        "SMB": ("tilted toward smaller companies", "tilted toward larger companies"),
        "HML": ("tilted toward value", "tilted toward growth"),
        "RMW": ("tilted toward profitable companies", "tilted toward less profitable companies"),
        "CMA": ("tilted toward conservative investment", "tilted toward aggressive investment"),
        "Mom": ("carrying a momentum tilt", "carrying a contrarian tilt"),
    }
    out: list[str] = []
    market = exposure.portfolio.get("Mkt-RF")
    if market is not None:
        if market >= 1.05:
            out.append(f"more market risk than the index ({market:.2f}x)")
        elif market <= 0.95:
            out.append(f"less market risk than the index ({market:.2f}x)")
    for factor, (positive, negative) in wording.items():
        loading = exposure.portfolio.get(factor, 0.0)
        if abs(loading) >= threshold:
            out.append(f"{positive if loading > 0 else negative} ({loading:+.2f})")
    return tuple(out)
