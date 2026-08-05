"""Shared value types.

These cross the purity boundary: analytics returns them, services and the app
consume them. They hold no behaviour that needs config, a database, or a clock.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum


class Action(StrEnum):
    """What a ledger row does to a position.

    SPLIT carries the ratio in `quantity` (2.0 for a 2-for-1) and leaves book
    value untouched. ROC reduces cost basis without a disposition, which is why
    it cannot be modelled as a partial sell.
    """

    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    SPLIT = "split"
    ROC = "return_of_capital"


class PriceSource(StrEnum):
    """Where a mark came from. Rendered in the UI so a stale number is visible
    as a stale number rather than passing for a live quote."""

    LIVE = "live"
    LAST_KNOWN = "last_known"
    MANUAL = "manual"
    COST = "cost"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Money:
    amount: float
    currency: str

    def __post_init__(self) -> None:
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError(f"currency must be an ISO-4217 code, got {self.currency!r}")


@dataclass(frozen=True, slots=True)
class Quote:
    """A price with its provenance and age attached.

    `source` and `as_of` travel with the number deliberately. The build this
    replaces returned a bare float and substituted an invented FX rate when the
    network failed, which is indistinguishable downstream from a real quote.
    """

    symbol: str
    price: float | None
    currency: str
    as_of: dt.date | None
    source: PriceSource

    @property
    def is_usable(self) -> bool:
        return self.price is not None and self.price > 0

    def staleness_days(self, today: dt.date) -> int | None:
        return None if self.as_of is None else (today - self.as_of).days


@dataclass(frozen=True, slots=True)
class Lot:
    """An open tax lot. Cost is held in the instrument's own currency and in
    base currency at the trade-date rate, so a later FX move cannot restate a
    cost basis that was already settled."""

    opened: dt.date
    quantity: float
    unit_cost_native: float
    unit_cost_base: float
    fx_rate: float


@dataclass(frozen=True, slots=True)
class Position:
    """A holding in one account. Long format: the account is a field, never a
    column name, so accounts stay data rather than becoming code."""

    ticker: str
    account_id: str
    quantity: float
    book_value_base: float
    acb_native: float
    acb_base: float
    currency: str

    @property
    def is_open(self) -> bool:
        return abs(self.quantity) > 1e-9


@dataclass(frozen=True, slots=True)
class RealizedGain:
    ticker: str
    account_id: str
    date: dt.date
    quantity: float
    proceeds_base: float
    cost_base: float

    @property
    def gain_base(self) -> float:
        return self.proceeds_base - self.cost_base


@dataclass(frozen=True, slots=True)
class LedgerResult:
    positions: tuple[Position, ...]
    realized: tuple[RealizedGain, ...]
    lots: dict[tuple[str, str], tuple[Lot, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Room:
    """Contribution room for one room group in one year.

    `unlimited` is the honest answer for a jurisdiction with no registered
    accounts, and for a taxable account anywhere. Callers must check it before
    reading the numbers.
    """

    group: str
    year: int
    unlimited: bool = False
    available_this_year: float | None = None
    contributed_this_year: float | None = None
    contributed_lifetime: float | None = None
    lifetime_limit: float | None = None
    carryforward: float | None = None
    notes: tuple[str, ...] = ()

    @property
    def over_contributed(self) -> bool:
        if self.unlimited or self.available_this_year is None:
            return False
        return self.available_this_year < -1e-9


@dataclass(frozen=True, slots=True)
class RiskStats:
    """The risk suite. Every field is optional because a short history should
    yield fewer statistics, not fabricated ones."""

    periods: int
    periods_per_year: int
    arithmetic_mean: float | None = None
    geometric_mean: float | None = None
    volatility: float | None = None
    downside_deviation: float | None = None
    max_drawdown: float | None = None
    beta: float | None = None
    alpha: float | None = None
    r_squared: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    treynor: float | None = None
    calmar: float | None = None
    tracking_error: float | None = None
    information_ratio: float | None = None
    active_return: float | None = None
    skew: float | None = None
    excess_kurtosis: float | None = None
    var_historical: float | None = None
    var_analytical: float | None = None
    cvar: float | None = None
    up_capture: float | None = None
    down_capture: float | None = None
    positive_periods: float | None = None
    gain_loss_ratio: float | None = None


@dataclass(frozen=True, slots=True)
class LookThrough:
    """Decomposition of the book into building blocks.

    `coverage` and `unmapped` are load-bearing. The reference silently folded an
    unrecognised fund into an "Other" bucket, which quietly corrupted every
    region and style rollup with no signal to the reader.
    """

    blocks: dict[str, float]
    region: dict[str, float]
    style: dict[str, float]
    coverage: float
    unmapped: tuple[str, ...] = ()
    stale: tuple[tuple[str, dt.date], ...] = ()


@dataclass(frozen=True, slots=True)
class DriftRow:
    sleeve_id: str
    label: str
    target_weight: float
    actual_weight: float
    band_pp: float

    @property
    def drift_pp(self) -> float:
        return (self.actual_weight - self.target_weight) * 100.0

    @property
    def breached(self) -> bool:
        return abs(self.drift_pp) > self.band_pp
