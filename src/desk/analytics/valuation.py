"""Value positions at market. Pure: given prices and FX, produce base-currency
market values. No network, no database, no provider — the service layer fetches
the marks and hands them in, which keeps this reusable and testable.

Market value in base currency uses *today's* FX (a current value), while book
value stays frozen at trade-date FX (a settled cost) — so an unrealized gain on
a foreign holding correctly includes the currency move.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from desk.domain.types import Position


@dataclass(frozen=True, slots=True)
class ValuedPosition:
    position: Position
    market_value_base: float | None  # None when no usable price exists
    price_native: float | None

    @property
    def gain_base(self) -> float | None:
        if self.market_value_base is None:
            return None
        return self.market_value_base - self.position.book_value_base

    @property
    def return_pct(self) -> float | None:
        """Unrealized return on cost. None when unpriced or cost is zero."""
        gain = self.gain_base
        book = self.position.book_value_base
        if gain is None or book <= 0:
            return None
        return gain / book

    @property
    def price_gain_native(self) -> float | None:
        """The part of the gain from the security itself, in its own currency.

        Separated from the currency effect because a Canadian investor holding a
        US fund earns two different things — the fund's return and the dollar's
        move — and averaging them into one number hides which one worked.
        """
        if self.price_native is None:
            return None
        return (self.price_native - self.position.acb_native) * self.position.quantity

    def fx_gain_base(self, fx_to_base: float) -> float | None:
        """The remainder of the gain attributable to the exchange rate."""
        gain, price_part = self.gain_base, self.price_gain_native
        if gain is None or price_part is None:
            return None
        return gain - price_part * fx_to_base


def value_positions(
    positions: Sequence[Position],
    price_native: Mapping[str, float | None],
    fx_to_base: Mapping[str, float],
) -> tuple[ValuedPosition, ...]:
    """Value each position. `price_native` is keyed by ticker in the holding's
    own currency; `fx_to_base` maps a currency to its rate into the base."""
    out: list[ValuedPosition] = []
    for p in positions:
        px = price_native.get(p.ticker)
        if px is None:
            out.append(ValuedPosition(position=p, market_value_base=None, price_native=None))
            continue
        fx = fx_to_base.get(p.currency, 1.0)
        out.append(
            ValuedPosition(
                position=p,
                market_value_base=p.quantity * px * fx,
                price_native=px,
            )
        )
    return tuple(out)


def portfolio_market_value(valued: Sequence[ValuedPosition]) -> float:
    """Sum of known market values. Positions with no price contribute nothing
    rather than silently falling back to book — the gap shows in coverage."""
    return sum(v.market_value_base for v in valued if v.market_value_base is not None)


def priced_coverage(valued: Sequence[ValuedPosition]) -> float:
    """Fraction of book value that carries a live market price."""
    total = sum(v.position.book_value_base for v in valued)
    if total <= 0:
        return 0.0
    priced = sum(
        v.position.book_value_base for v in valued if v.market_value_base is not None
    )
    return priced / total


@dataclass(frozen=True, slots=True)
class AttributionRow:
    """One holding's share of the portfolio's unrealized gain."""

    ticker: str
    currency: str
    quantity: float
    acb_base: float
    price_native: float | None
    book_value_base: float
    market_value_base: float | None
    gain_base: float | None
    return_pct: float | None
    weight: float  # share of market value
    contribution: float | None  # gain as a fraction of total cost
    price_gain_base: float | None
    fx_gain_base: float | None


@dataclass(frozen=True, slots=True)
class Attribution:
    """Where the portfolio's gain came from.

    `rows` sum to the totals: contribution is each holding's gain over the
    portfolio's total cost, so the column adds up to the portfolio return. That
    identity is the point — an attribution whose parts do not sum to the whole
    is decoration.
    """

    rows: tuple[AttributionRow, ...]
    total_book: float
    total_market: float
    total_gain: float
    total_return: float | None
    price_gain: float
    fx_gain: float
    unpriced: tuple[str, ...]

    @property
    def winners(self) -> tuple[AttributionRow, ...]:
        return tuple(r for r in self.rows if (r.gain_base or 0.0) > 0)

    @property
    def losers(self) -> tuple[AttributionRow, ...]:
        return tuple(r for r in self.rows if (r.gain_base or 0.0) < 0)


def attribution(
    valued: Sequence[ValuedPosition], fx_to_base: Mapping[str, float]
) -> Attribution:
    """Decompose the unrealized gain by holding, and into price versus currency.

    Unpriced holdings are named rather than dropped: a report that quietly omits
    a position understates the book and gives no hint that it did.
    """
    priced = [v for v in valued if v.market_value_base is not None]
    total_market = sum(v.market_value_base or 0.0 for v in priced)
    total_book = sum(v.position.book_value_base for v in priced)
    total_gain = total_market - total_book

    rows: list[AttributionRow] = []
    price_total = fx_total = 0.0
    for v in valued:
        p = v.position
        fx = fx_to_base.get(p.currency, 1.0)
        price_gain = v.price_gain_native
        price_gain_base = None if price_gain is None else price_gain * fx
        fx_gain = v.fx_gain_base(fx)
        if price_gain_base is not None:
            price_total += price_gain_base
        if fx_gain is not None:
            fx_total += fx_gain
        rows.append(
            AttributionRow(
                ticker=p.ticker,
                currency=p.currency,
                quantity=p.quantity,
                acb_base=p.acb_base,
                price_native=v.price_native,
                book_value_base=p.book_value_base,
                market_value_base=v.market_value_base,
                gain_base=v.gain_base,
                return_pct=v.return_pct,
                weight=(
                    (v.market_value_base / total_market)
                    if v.market_value_base is not None and total_market > 0
                    else 0.0
                ),
                contribution=(
                    (v.gain_base / total_book)
                    if v.gain_base is not None and total_book > 0
                    else None
                ),
                price_gain_base=price_gain_base,
                fx_gain_base=fx_gain,
            )
        )

    rows.sort(key=lambda r: r.gain_base if r.gain_base is not None else float("-inf"),
              reverse=True)
    return Attribution(
        rows=tuple(rows),
        total_book=total_book,
        total_market=total_market,
        total_gain=total_gain,
        total_return=(total_gain / total_book) if total_book > 0 else None,
        price_gain=price_total,
        fx_gain=fx_total,
        unpriced=tuple(
            v.position.ticker for v in valued if v.market_value_base is None
        ),
    )
