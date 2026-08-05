"""Cost basis and position accounting.

Pure: numpy, pandas and `desk.domain` only. No config, no database, no network,
no clock. A function that takes only arrays cannot contain somebody's portfolio,
which is what makes this layer both reusable and safe to publish.

Three things the reference build got wrong, fixed here:

  1. **Sells did not reduce book value.** Its average cost was computed over
     buys only, so disposing of a position left its full original cost on the
     books forever, overstating book value and understating gain.
  2. **Foreign cost basis drifted with the exchange rate.** Cost must be frozen
     at the trade-date rate. A CAD cost basis that moves because the dollar
     moved is not a cost basis.
  3. **Accounts were columns.** Positions were emitted with one hardcoded column
     per account the author happened to own, so a third account meant editing
     the engine. Here the account is a field and accounts are data.

Canadian adjusted cost base is a *pooled average* per security, not per-lot
FIFO: every unit of a security in an account shares one average cost, and a
partial sale relieves cost at that average. This engine implements the pooled
convention, and keeps the lot detail alongside it for reporting.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from desk.domain.types import Action, LedgerResult, Lot, Position, RealizedGain

QUANTITY_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One transaction, already normalised.

    `fx_rate` converts the instrument's currency to base currency **on the trade
    date** and is captured at entry. It is never recomputed: that is the whole
    point.
    """

    date: dt.date
    ticker: str
    account_id: str
    action: Action
    quantity: float = 0.0
    price: float = 0.0
    fees: float = 0.0
    fx_rate: float = 1.0
    currency: str = "CAD"

    def __post_init__(self) -> None:
        if self.fx_rate <= 0:
            raise ValueError(
                f"{self.ticker} on {self.date}: fx_rate must be positive, got {self.fx_rate}"
            )
        if self.quantity < 0:
            raise ValueError(
                f"{self.ticker} on {self.date}: quantity must be non-negative "
                "(direction comes from `action`, not the sign)"
            )
        if self.action is Action.SPLIT and self.quantity <= 0:
            raise ValueError(f"{self.ticker} on {self.date}: a split needs a positive ratio")


class InsufficientUnits(ValueError):
    """A sale exceeds the units held.

    Raised rather than clamped. Silently selling into a negative position turns
    a data-entry slip into a plausible-looking short, and every downstream
    number then quietly lies.
    """


@dataclass
class _Pool:
    """Mutable running state for one (ticker, account) pair."""

    quantity: float = 0.0
    cost_native: float = 0.0
    cost_base: float = 0.0
    lots: list[Lot] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.lots is None:
            self.lots = []

    @property
    def acb_native(self) -> float:
        return self.cost_native / self.quantity if self.quantity > QUANTITY_EPSILON else 0.0

    @property
    def acb_base(self) -> float:
        return self.cost_base / self.quantity if self.quantity > QUANTITY_EPSILON else 0.0


def _apply_buy(pool: _Pool, e: LedgerEntry) -> None:
    """Acquisition. Fees are capitalised into cost, which is both the Canadian
    treatment and the honest one — a commission is part of what you paid."""
    gross_native = e.quantity * e.price + e.fees
    pool.quantity += e.quantity
    pool.cost_native += gross_native
    pool.cost_base += gross_native * e.fx_rate
    pool.lots.append(
        Lot(
            opened=e.date,
            quantity=e.quantity,
            unit_cost_native=(e.price + e.fees / e.quantity) if e.quantity else 0.0,
            unit_cost_base=((e.price + e.fees / e.quantity) * e.fx_rate) if e.quantity else 0.0,
            fx_rate=e.fx_rate,
        )
    )


def _apply_sell(pool: _Pool, e: LedgerEntry) -> RealizedGain:
    """Disposition. Relieves cost at the pooled average and realises the gain.

    This is the branch the reference omitted entirely.
    """
    if e.quantity > pool.quantity + QUANTITY_EPSILON:
        raise InsufficientUnits(
            f"{e.ticker} in {e.account_id} on {e.date}: sell of {e.quantity:g} units "
            f"exceeds the {pool.quantity:g} held"
        )

    sold = min(e.quantity, pool.quantity)
    share = sold / pool.quantity if pool.quantity > QUANTITY_EPSILON else 0.0
    relieved_native = pool.cost_native * share
    relieved_base = pool.cost_base * share

    proceeds_base = (sold * e.price - e.fees) * e.fx_rate

    pool.quantity -= sold
    pool.cost_native -= relieved_native
    pool.cost_base -= relieved_base
    if pool.quantity <= QUANTITY_EPSILON:
        # Fully closed: clear residual float dust so the position reads as flat.
        pool.quantity = 0.0
        pool.cost_native = 0.0
        pool.cost_base = 0.0
        pool.lots.clear()
    else:
        pool.lots = _shrink_lots(pool.lots, 1.0 - share)

    return RealizedGain(
        ticker=e.ticker,
        account_id=e.account_id,
        date=e.date,
        quantity=sold,
        proceeds_base=proceeds_base,
        cost_base=relieved_base,
    )


def _apply_split(pool: _Pool, e: LedgerEntry) -> None:
    """Share split or consolidation. Units scale, total cost does not, so the
    per-unit average moves inversely. A ratio below 1 is a reverse split."""
    ratio = e.quantity
    pool.quantity *= ratio
    pool.lots = [
        Lot(
            opened=lot.opened,
            quantity=lot.quantity * ratio,
            unit_cost_native=lot.unit_cost_native / ratio,
            unit_cost_base=lot.unit_cost_base / ratio,
            fx_rate=lot.fx_rate,
        )
        for lot in pool.lots
    ]


def _apply_roc(pool: _Pool, e: LedgerEntry) -> None:
    """Return of capital: reduces cost base without a disposition.

    Cannot be modelled as a partial sale — no units change hands. Ignoring it,
    as the reference does, overstates book value and understates the eventual
    capital gain. Cost is floored at zero; below that the excess is an immediate
    gain, which is flagged for the caller rather than silently absorbed.
    """
    amount_native = e.price if e.price else e.quantity
    reduction_native = min(amount_native, pool.cost_native)
    share = reduction_native / pool.cost_native if pool.cost_native > 0 else 0.0
    pool.cost_native -= reduction_native
    pool.cost_base -= pool.cost_base * share
    pool.lots = _shrink_lots(pool.lots, 1.0 - share, cost_only=True)


def _shrink_lots(lots: Sequence[Lot], factor: float, *, cost_only: bool = False) -> list[Lot]:
    """Scale the lot detail so it stays consistent with the pooled totals."""
    if cost_only:
        return [
            Lot(
                opened=lot.opened,
                quantity=lot.quantity,
                unit_cost_native=lot.unit_cost_native * factor,
                unit_cost_base=lot.unit_cost_base * factor,
                fx_rate=lot.fx_rate,
            )
            for lot in lots
        ]
    return [
        Lot(
            opened=lot.opened,
            quantity=lot.quantity * factor,
            unit_cost_native=lot.unit_cost_native,
            unit_cost_base=lot.unit_cost_base,
            fx_rate=lot.fx_rate,
        )
        for lot in lots
        if lot.quantity * factor > QUANTITY_EPSILON
    ]


def build_ledger(entries: Iterable[LedgerEntry]) -> LedgerResult:
    """Replay a transaction ledger into positions, realized gains, and lots.

    Entries are processed in date order, with same-day entries kept in the order
    supplied so a same-day buy-then-sell behaves as written.
    """
    ordered = sorted(entries, key=lambda e: e.date)
    pools: dict[tuple[str, str], _Pool] = {}
    currencies: dict[str, str] = {}
    realized: list[RealizedGain] = []

    for entry in ordered:
        currencies.setdefault(entry.ticker, entry.currency)
        key = (entry.ticker, entry.account_id)
        pool = pools.setdefault(key, _Pool())

        if entry.action is Action.BUY:
            _apply_buy(pool, entry)
        elif entry.action is Action.SELL:
            realized.append(_apply_sell(pool, entry))
        elif entry.action is Action.SPLIT:
            _apply_split(pool, entry)
        elif entry.action is Action.ROC:
            _apply_roc(pool, entry)
        elif entry.action is Action.DIVIDEND:
            # Cash income. It affects the cash balance and the contribution
            # ledger, not the cost base of the security.
            continue

    positions = tuple(
        Position(
            ticker=ticker,
            account_id=account_id,
            quantity=pool.quantity,
            book_value_base=pool.cost_base,
            acb_native=pool.acb_native,
            acb_base=pool.acb_base,
            currency=currencies.get(ticker, "CAD"),
        )
        for (ticker, account_id), pool in sorted(pools.items())
        if pool.quantity > QUANTITY_EPSILON
    )

    lots = {
        key: tuple(pool.lots)
        for key, pool in sorted(pools.items())
        if pool.quantity > QUANTITY_EPSILON
    }

    return LedgerResult(positions=positions, realized=tuple(realized), lots=lots)


def aggregate_by_ticker(positions: Sequence[Position]) -> tuple[Position, ...]:
    """Roll positions up across accounts, pooling cost correctly.

    The account is set to the sentinel `"*"`: an aggregate is not a holding in
    an account, and pretending otherwise is how account-shaped bugs start.
    """
    grouped: dict[str, list[Position]] = {}
    for p in positions:
        grouped.setdefault(p.ticker, []).append(p)

    out: list[Position] = []
    for ticker, group in sorted(grouped.items()):
        quantity = sum(p.quantity for p in group)
        book = sum(p.book_value_base for p in group)
        native_cost = sum(p.acb_native * p.quantity for p in group)
        out.append(
            Position(
                ticker=ticker,
                account_id="*",
                quantity=quantity,
                book_value_base=book,
                acb_native=native_cost / quantity if quantity > QUANTITY_EPSILON else 0.0,
                acb_base=book / quantity if quantity > QUANTITY_EPSILON else 0.0,
                currency=group[0].currency,
            )
        )
    return tuple(out)
