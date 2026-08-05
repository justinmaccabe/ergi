"""Cost basis engine — known-answer tests.

Fixtures are deliberately fake and round: tickers AAA/BBB, prices that divide
evenly. Golden numbers here should read like a textbook, never like somebody's
brokerage statement.
"""

from __future__ import annotations

import datetime as dt

import pytest

from desk.analytics.positions import (
    InsufficientUnits,
    LedgerEntry,
    aggregate_by_ticker,
    build_ledger,
)
from desk.domain.types import Action

D = dt.date


def buy(day: int, ticker: str = "AAA", account: str = "acct_one", **kw: float) -> LedgerEntry:
    return LedgerEntry(D(2024, 1, day), ticker, account, Action.BUY, **kw)  # type: ignore[arg-type]


def sell(day: int, ticker: str = "AAA", account: str = "acct_one", **kw: float) -> LedgerEntry:
    return LedgerEntry(D(2024, 1, day), ticker, account, Action.SELL, **kw)  # type: ignore[arg-type]


class TestPooledAverageCost:
    """Worked example in the shape of the CRA's own adjusted-cost-base guidance.

    buy  100 units at 20, commission 50   -> pooled cost 2050
    buy  150 units at 25, commission 75   -> pooled cost 5875 over 250 units
                                             average cost 23.50 per unit
    sell 200 units at 30, commission 100  -> proceeds 5900
                                             cost relieved 4700
                                             realized gain 1200
                                             50 units remain at 23.50
    """

    def test_worked_example(self) -> None:
        result = build_ledger(
            [
                buy(5, quantity=100, price=20.0, fees=50.0),
                buy(10, quantity=150, price=25.0, fees=75.0),
                sell(20, quantity=200, price=30.0, fees=100.0),
            ]
        )
        (position,) = result.positions
        assert position.quantity == pytest.approx(50.0)
        assert position.acb_base == pytest.approx(23.50)
        assert position.book_value_base == pytest.approx(1175.0)

        (realized,) = result.realized
        assert realized.proceeds_base == pytest.approx(5900.0)
        assert realized.cost_base == pytest.approx(4700.0)
        assert realized.gain_base == pytest.approx(1200.0)

    def test_average_cost_is_pooled_not_fifo(self) -> None:
        """Selling relieves cost at the blended average, not at the oldest lot."""
        result = build_ledger(
            [
                buy(5, quantity=100, price=10.0),
                buy(10, quantity=100, price=20.0),
                sell(15, quantity=100, price=30.0),
            ]
        )
        (realized,) = result.realized
        assert realized.cost_base == pytest.approx(1500.0)  # 100 x 15, not 100 x 10
        (position,) = result.positions
        assert position.acb_base == pytest.approx(15.0)

    def test_commissions_are_capitalised_into_cost(self) -> None:
        (position,) = build_ledger([buy(5, quantity=100, price=10.0, fees=25.0)]).positions
        assert position.book_value_base == pytest.approx(1025.0)
        assert position.acb_base == pytest.approx(10.25)


class TestSellsReduceBookValue:
    """The reference build averaged buys only, so a disposition left the full
    original cost on the books forever. These are the regression tests."""

    def test_partial_sale_reduces_book_value(self) -> None:
        result = build_ledger([buy(5, quantity=100, price=10.0), sell(10, quantity=40, price=12.0)])
        (position,) = result.positions
        assert position.quantity == pytest.approx(60.0)
        assert position.book_value_base == pytest.approx(600.0)

    def test_full_sale_closes_the_position_entirely(self) -> None:
        result = build_ledger(
            [buy(5, quantity=100, price=10.0), sell(10, quantity=100, price=12.0)]
        )
        assert result.positions == ()
        assert result.realized[0].gain_base == pytest.approx(200.0)

    def test_buy_sell_buy_leaves_only_the_reopened_cost(self) -> None:
        result = build_ledger(
            [
                buy(5, quantity=100, price=10.0),
                sell(10, quantity=100, price=12.0),
                buy(15, quantity=50, price=20.0),
            ]
        )
        (position,) = result.positions
        assert position.quantity == pytest.approx(50.0)
        assert position.book_value_base == pytest.approx(1000.0)
        assert position.acb_base == pytest.approx(20.0)

    def test_overselling_raises_rather_than_going_short(self) -> None:
        with pytest.raises(InsufficientUnits, match="exceeds the 100 held"):
            build_ledger([buy(5, quantity=100, price=10.0), sell(10, quantity=150, price=12.0)])


class TestTradeDateFx:
    """Cost basis is frozen at the trade-date rate. A base-currency cost that
    moves because the exchange rate moved is not a cost basis."""

    def test_cost_uses_the_rate_at_purchase(self) -> None:
        result = build_ledger(
            [
                LedgerEntry(
                    D(2024, 1, 5),
                    "BBB",
                    "acct_one",
                    Action.BUY,
                    quantity=10,
                    price=100.0,
                    fx_rate=1.30,
                    currency="USD",
                ),
                LedgerEntry(
                    D(2024, 6, 5),
                    "BBB",
                    "acct_one",
                    Action.BUY,
                    quantity=10,
                    price=100.0,
                    fx_rate=1.40,
                    currency="USD",
                ),
            ]
        )
        (position,) = result.positions
        assert position.acb_native == pytest.approx(100.0)
        assert position.book_value_base == pytest.approx(2700.0)  # 1300 + 1400
        assert position.acb_base == pytest.approx(135.0)

    def test_later_rates_never_restate_an_earlier_cost(self) -> None:
        base = LedgerEntry(
            D(2024, 1, 5),
            "BBB",
            "acct_one",
            Action.BUY,
            quantity=10,
            price=100.0,
            fx_rate=1.30,
            currency="USD",
        )
        first = build_ledger([base]).positions[0].book_value_base
        later = (
            build_ledger(
                [
                    base,
                    LedgerEntry(
                        D(2024, 9, 1),
                        "BBB",
                        "acct_one",
                        Action.DIVIDEND,
                        quantity=0,
                        price=50.0,
                        fx_rate=1.99,
                        currency="USD",
                    ),
                ]
            )
            .positions[0]
            .book_value_base
        )
        assert first == pytest.approx(later)

    def test_proceeds_use_the_rate_at_sale(self) -> None:
        result = build_ledger(
            [
                LedgerEntry(
                    D(2024, 1, 5),
                    "BBB",
                    "acct_one",
                    Action.BUY,
                    quantity=10,
                    price=100.0,
                    fx_rate=1.30,
                    currency="USD",
                ),
                LedgerEntry(
                    D(2024, 6, 5),
                    "BBB",
                    "acct_one",
                    Action.SELL,
                    quantity=10,
                    price=100.0,
                    fx_rate=1.40,
                    currency="USD",
                ),
            ]
        )
        (realized,) = result.realized
        assert realized.proceeds_base == pytest.approx(1400.0)
        assert realized.cost_base == pytest.approx(1300.0)
        # No move in the security at all; the entire gain is currency.
        assert realized.gain_base == pytest.approx(100.0)

    def test_a_nonpositive_fx_rate_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="fx_rate must be positive"):
            LedgerEntry(
                D(2024, 1, 5), "AAA", "acct_one", Action.BUY, quantity=1, price=1.0, fx_rate=0.0
            )


class TestCorporateActions:
    def test_split_scales_units_and_halves_unit_cost(self) -> None:
        result = build_ledger(
            [
                buy(5, quantity=100, price=20.0),
                LedgerEntry(D(2024, 3, 1), "AAA", "acct_one", Action.SPLIT, quantity=2.0),
            ]
        )
        (position,) = result.positions
        assert position.quantity == pytest.approx(200.0)
        assert position.acb_base == pytest.approx(10.0)
        assert position.book_value_base == pytest.approx(2000.0)  # unchanged

    def test_reverse_split(self) -> None:
        result = build_ledger(
            [
                buy(5, quantity=100, price=10.0),
                LedgerEntry(D(2024, 3, 1), "AAA", "acct_one", Action.SPLIT, quantity=0.5),
            ]
        )
        (position,) = result.positions
        assert position.quantity == pytest.approx(50.0)
        assert position.acb_base == pytest.approx(20.0)

    def test_return_of_capital_reduces_cost_without_a_disposition(self) -> None:
        result = build_ledger(
            [
                buy(5, quantity=100, price=10.0),
                LedgerEntry(D(2024, 6, 1), "AAA", "acct_one", Action.ROC, price=150.0),
            ]
        )
        (position,) = result.positions
        assert position.quantity == pytest.approx(100.0)  # no units disposed
        assert position.book_value_base == pytest.approx(850.0)
        assert result.realized == ()

    def test_return_of_capital_floors_cost_at_zero(self) -> None:
        result = build_ledger(
            [
                buy(5, quantity=100, price=10.0),
                LedgerEntry(D(2024, 6, 1), "AAA", "acct_one", Action.ROC, price=5000.0),
            ]
        )
        assert result.positions[0].book_value_base == pytest.approx(0.0)

    def test_dividends_do_not_touch_cost_base(self) -> None:
        result = build_ledger(
            [
                buy(5, quantity=100, price=10.0),
                LedgerEntry(D(2024, 6, 1), "AAA", "acct_one", Action.DIVIDEND, price=42.0),
            ]
        )
        assert result.positions[0].book_value_base == pytest.approx(1000.0)


class TestAccountsAreData:
    """Accounts are a field, never a column. The reference emitted one fixed
    column per account its author held, so a third account meant editing the
    engine."""

    def test_the_same_security_pools_separately_per_account(self) -> None:
        result = build_ledger(
            [
                buy(5, account="acct_one", quantity=100, price=10.0),
                buy(5, account="acct_two", quantity=100, price=30.0),
            ]
        )
        by_account = {p.account_id: p for p in result.positions}
        assert by_account["acct_one"].acb_base == pytest.approx(10.0)
        assert by_account["acct_two"].acb_base == pytest.approx(30.0)

    def test_selling_in_one_account_leaves_the_other_untouched(self) -> None:
        result = build_ledger(
            [
                buy(5, account="acct_one", quantity=100, price=10.0),
                buy(5, account="acct_two", quantity=100, price=10.0),
                sell(10, account="acct_one", quantity=100, price=15.0),
            ]
        )
        assert [p.account_id for p in result.positions] == ["acct_two"]

    def test_arbitrarily_many_accounts_need_no_code_change(self) -> None:
        entries = [buy(5, account=f"acct_{n}", quantity=10, price=10.0) for n in range(1, 8)]
        assert len({p.account_id for p in build_ledger(entries).positions}) == 7

    def test_aggregate_pools_cost_across_accounts(self) -> None:
        result = build_ledger(
            [
                buy(5, account="acct_one", quantity=100, price=10.0),
                buy(5, account="acct_two", quantity=100, price=20.0),
            ]
        )
        (rolled,) = aggregate_by_ticker(result.positions)
        assert rolled.account_id == "*"
        assert rolled.quantity == pytest.approx(200.0)
        assert rolled.acb_base == pytest.approx(15.0)


class TestOrdering:
    def test_entries_are_replayed_in_date_order_regardless_of_input_order(self) -> None:
        forward = build_ledger(
            [buy(5, quantity=100, price=10.0), sell(20, quantity=50, price=12.0)]
        )
        shuffled = build_ledger(
            [sell(20, quantity=50, price=12.0), buy(5, quantity=100, price=10.0)]
        )
        assert forward.positions[0].book_value_base == pytest.approx(
            shuffled.positions[0].book_value_base
        )

    def test_an_empty_ledger_yields_nothing_rather_than_failing(self) -> None:
        result = build_ledger([])
        assert result.positions == () and result.realized == ()
