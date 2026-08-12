"""The market-data contract.

Every price this system produces carries its source and its age. That is the
whole design of this layer, and it is a direct response to how the reference
handled a failed fetch: it returned a hardcoded exchange rate, which downstream
is indistinguishable from a real one. A confidently-rendered wrong number is
worse than a blank.

The resolution order is explicit and every step is labelled:

    live quote          PriceSource.LIVE
    last known good     PriceSource.LAST_KNOWN   (with the date it was good)
    manual mark         PriceSource.MANUAL       (private holdings)
    cost basis          PriceSource.COST         (last resort, clearly labelled)
    nothing             PriceSource.UNAVAILABLE  (price is None; never a guess)
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

import pandas as pd

from desk.domain.types import PriceSource, Quote


@runtime_checkable
class PriceProvider(Protocol):
    """A source of quotes and price history.

    Implementations must not raise on a network failure. They return empty or
    `UNAVAILABLE` results and let the caller decide what to do, because a
    dashboard that crashes when one symbol is delisted is not usable.
    """

    name: str

    def quotes(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        """Latest quote per symbol. Missing symbols yield UNAVAILABLE quotes."""
        ...

    def history(self, symbols: Sequence[str], period: str) -> pd.DataFrame:
        """Adjusted close history, indexed by date, one column per symbol.

        Returns an empty frame rather than raising when the fetch fails.
        """
        ...

    def quotes_with_previous(
        self, symbols: Sequence[str]
    ) -> tuple[Mapping[str, Quote], Mapping[str, float]]:
        """Latest quotes, plus the prior session's close per symbol.

        Part of the contract rather than a convenience: a daily P&L figure needs
        both marks, and deriving the prior close from a second fetch invites the
        two to disagree. Symbols with only one observation are absent from the
        second mapping, so the caller reports no daily move rather than a
        fabricated one against a repeated price.
        """
        ...


@runtime_checkable
class FxProvider(Protocol):
    name: str

    def rate(self, base: str, quote: str) -> Quote:
        """Latest rate for base->quote, or an UNAVAILABLE quote. Never a guess."""
        ...

    def series(self, base: str, quote: str, period: str) -> pd.Series:
        """Historical rates, empty on failure."""
        ...


def unavailable(symbol: str, currency: str = "") -> Quote:
    return Quote(
        symbol=symbol,
        price=None,
        currency=currency,
        as_of=None,
        source=PriceSource.UNAVAILABLE,
    )


def resolve_price(
    ticker: str,
    *,
    live: Quote | None = None,
    last_known: Quote | None = None,
    manual: Quote | None = None,
    cost: float | None = None,
    currency: str = "",
    is_private: bool = False,
) -> Quote:
    """Pick the best available mark and say which one it is.

    A private holding prefers its manual mark over any accidental symbol match,
    which is the only case where ordering is not simply freshness.
    """
    if is_private and manual is not None and manual.is_usable:
        return manual
    for candidate in (live, last_known, manual):
        if candidate is not None and candidate.is_usable:
            return candidate
    if cost is not None and cost > 0:
        return Quote(
            symbol=ticker,
            price=cost,
            currency=currency,
            as_of=None,
            source=PriceSource.COST,
        )
    return unavailable(ticker, currency)


def coverage(quotes: Mapping[str, Quote]) -> float:
    """Fraction of quotes that came from a live fetch.

    Recorded on every snapshot: a valuation built mostly from stale marks is a
    different measurement from a clean one, and that difference should survive
    into the history rather than being flattened away.
    """
    if not quotes:
        return 0.0
    live = sum(1 for q in quotes.values() if q.source is PriceSource.LIVE)
    return live / len(quotes)


def staleness_report(
    quotes: Mapping[str, Quote], today: dt.date, max_days: int
) -> tuple[tuple[str, int], ...]:
    """Symbols whose mark is older than the configured tolerance."""
    stale: list[tuple[str, int]] = []
    for symbol, quote in quotes.items():
        age = quote.staleness_days(today)
        if age is not None and age > max_days:
            stale.append((symbol, age))
    return tuple(sorted(stale, key=lambda pair: -pair[1]))
