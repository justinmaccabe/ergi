"""Factor-exposure composition: fetch factors and prices, convert, regress.

The currency handling here is the part worth reading. Fama-French factors are
USD-denominated excess returns. A CAD-listed fund's price series is in CAD, so
regressing it directly on those factors mixes the fund's own return with the
CAD/USD move and loads the difference onto the market beta — which is why a
plain S&P 500 tracker listed in Toronto appears to have a market beta well below
1.0 when the conversion is skipped.

So every holding's price series is converted into USD before it is regressed,
and the resulting loadings describe the fund's exposure in the factors' own
currency. This is the opposite direction from `market.base_history`, which
converts into the investor's base currency for reporting. Both are correct for
their purpose, and conflating them is the bug this docstring exists to prevent.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from desk.analytics.factors import (
    FactorExposure,
    factor_exposure,
    monthly_returns,
    to_usd,
)
from desk.data.providers.factors import get_provider
from desk.data.providers.market import YFinanceFx, YFinanceProvider

# The factor library publishes monthly data with a lag of several weeks, so a
# window shorter than a couple of years leaves very few usable observations.
DEFAULT_PERIOD = "5y"

FACTOR_CURRENCY = "USD"

# Asset classes for which loadings on *equity* factors carry meaning. Anything
# else is excluded and reported as unattributed weight.
#
# This exists because the regression is perfectly happy to fit a spot-bitcoin
# fund and return an R² of 0.36 with a -2.9 investment-factor loading. Those
# numbers are arithmetically correct and financially meaningless: bitcoin has no
# book-to-market and no reinvestment rate, so the coefficients are noise given a
# name. Left in, one such holding at a tenth of the book visibly drags every
# portfolio-level loading. An unset asset class defaults to equity, so this only
# ever excludes a holding somebody has explicitly labelled.
EQUITY_LIKE = frozenset({"equity", "equities", "stock", "stocks", "etf", "fund"})


def factor_eligible(
    instruments: Iterable[object], held: Iterable[str] | None = None
) -> set[str]:
    """Tickers for which an equity-factor regression is meaningful.

    A holding is eligible unless its `asset_class` is set to something outside
    `EQUITY_LIKE` — so labelling a commodity or digital-asset fund in config is
    what keeps it out of the regression.
    """
    eligible: set[str] = set()
    held_set = None if held is None else set(held)
    for inst in instruments:
        ticker = getattr(inst, "ticker", None)
        if ticker is None or (held_set is not None and ticker not in held_set):
            continue
        asset_class = (getattr(inst, "asset_class", None) or "").strip().lower()
        if not asset_class or asset_class in EQUITY_LIKE:
            eligible.add(ticker)
    return eligible


def load_factors(provider_name: str, cache_dir: str | None = None) -> pd.DataFrame:
    """The factor frame, or empty when unavailable or switched off."""
    return get_provider(provider_name, cache_dir=cache_dir).load()


def usd_returns_by_ticker(
    symbols: dict[str, str],
    currencies: dict[str, str],
    period: str = DEFAULT_PERIOD,
) -> dict[str, pd.Series]:
    """Monthly returns per ticker, denominated in the factor currency (USD).

    A holding already quoted in USD is used as-is. Anything else is multiplied by
    its currency's USD rate series before returns are taken — converting the
    *prices* and then differencing, rather than converting a return, because the
    latter is only an approximation and drifts as the rate moves.
    """
    provider = YFinanceProvider()
    fx_provider = YFinanceFx()
    history = provider.history(list(symbols.values()), period)
    if history.empty:
        return {}

    rates: dict[str, pd.Series] = {}
    out: dict[str, pd.Series] = {}
    for ticker, symbol in symbols.items():
        if symbol not in history.columns:
            continue
        prices = history[symbol].dropna()
        if prices.empty:
            continue
        currency = currencies.get(ticker, FACTOR_CURRENCY)
        if currency != FACTOR_CURRENCY:
            if currency not in rates:
                rates[currency] = fx_provider.series(currency, FACTOR_CURRENCY, period)
            series = rates[currency]
            if series.empty:
                # Without a rate the series cannot be put in the factors'
                # currency, and regressing it anyway would silently attribute
                # the currency move to the market factor. Skip it instead; the
                # holding surfaces as unattributed weight.
                continue
            prices = to_usd(prices, series)
        returns = monthly_returns(prices)
        if not returns.empty:
            out[ticker] = returns
    return out


def exposure(
    symbols: dict[str, str],
    currencies: dict[str, str],
    weights: dict[str, float],
    *,
    provider_name: str = "kenfrench",
    cache_dir: str | None = None,
    period: str = DEFAULT_PERIOD,
    eligible: set[str] | None = None,
) -> FactorExposure | None:
    """Portfolio factor loadings. None when the factor data is unavailable.

    `weights` covers the whole portfolio by market value, including holdings that
    cannot be regressed, so the returned `unattributed` share is honest.
    `eligible` restricts which holdings are regressed at all; excluded ones keep
    their weight and surface as unattributed rather than disappearing.
    """
    factors = load_factors(provider_name, cache_dir)
    if factors.empty:
        return None
    if eligible is not None:
        symbols = {t: s for t, s in symbols.items() if t in eligible}
    returns = usd_returns_by_ticker(symbols, currencies, period)
    if not returns:
        return None
    return factor_exposure(returns, factors, weights)
