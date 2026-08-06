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
