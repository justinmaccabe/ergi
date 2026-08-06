"""Valuation and attribution — known-answer tests.

Round numbers throughout: a foreign holding that doubles while the currency
moves 25% has a gain that splits cleanly into a price part and an FX part, so
the arithmetic can be checked by eye.
"""

from __future__ import annotations

import pytest

from desk.analytics.valuation import (
    attribution,
    portfolio_market_value,
    priced_coverage,
    value_positions,
)
from desk.domain.types import Position


def position(
    ticker: str = "AAA",
    *,
    quantity: float = 100.0,
    acb_native: float = 10.0,
    acb_base: float | None = None,
    currency: str = "CAD",
    account: str = "acct_one",
) -> Position:
    base = acb_native if acb_base is None else acb_base
    return Position(
        ticker=ticker,
        account_id=account,
        quantity=quantity,
        book_value_base=quantity * base,
        acb_native=acb_native,
        acb_base=base,
        currency=currency,
    )


class TestValuePositions:
    def test_domestic_gain(self) -> None:
        # 100 units, cost 10, now 15 -> market 1500, gain 500, +50%
        valued = value_positions([position()], {"AAA": 15.0}, {"CAD": 1.0})
        assert valued[0].market_value_base == pytest.approx(1500.0)
        assert valued[0].gain_base == pytest.approx(500.0)
        assert valued[0].return_pct == pytest.approx(0.5)

    def test_foreign_holding_uses_current_fx_for_market_value(self) -> None:
        # cost 10 USD at 1.20 -> book 1200 CAD; now 20 USD at 1.50 -> 3000 CAD
        p = position(currency="USD", acb_native=10.0, acb_base=12.0)
        valued = value_positions([p], {"AAA": 20.0}, {"USD": 1.50})
        assert valued[0].market_value_base == pytest.approx(3000.0)
        assert valued[0].gain_base == pytest.approx(1800.0)

    def test_price_and_fx_parts_sum_to_the_total_gain(self) -> None:
        p = position(currency="USD", acb_native=10.0, acb_base=12.0)
        valued = value_positions([p], {"AAA": 20.0}, {"USD": 1.50})[0]
        # security part: (20 - 10) * 100 = 1000 USD -> 1500 CAD at today's rate
        assert valued.price_gain_native == pytest.approx(1000.0)
        price_part = valued.price_gain_native * 1.50
        fx_part = valued.fx_gain_base(1.50)
        assert price_part + fx_part == pytest.approx(valued.gain_base)
        # the rest is the currency: cost was struck at 1.20, marked at 1.50
        assert fx_part == pytest.approx(300.0)

    def test_unpriced_position_yields_none_not_zero(self) -> None:
        valued = value_positions([position()], {}, {"CAD": 1.0})
        assert valued[0].market_value_base is None
        assert valued[0].gain_base is None
        assert valued[0].return_pct is None

    def test_market_value_excludes_unpriced(self) -> None:
        valued = value_positions(
            [position("AAA"), position("BBB")], {"AAA": 15.0}, {"CAD": 1.0}
        )
        assert portfolio_market_value(valued) == pytest.approx(1500.0)
        assert priced_coverage(valued) == pytest.approx(0.5)


class TestAttribution:
    def test_contributions_sum_to_the_portfolio_return(self) -> None:
        positions = [
            position("AAA", quantity=100, acb_native=10.0),  # -> 15: +500
            position("BBB", quantity=100, acb_native=20.0),  # -> 18: -200
        ]
        valued = value_positions(positions, {"AAA": 15.0, "BBB": 18.0}, {"CAD": 1.0})
        report = attribution(valued, {"CAD": 1.0})

        assert report.total_book == pytest.approx(3000.0)
        assert report.total_market == pytest.approx(3300.0)
        assert report.total_gain == pytest.approx(300.0)
        assert report.total_return == pytest.approx(0.1)
        contributions = sum(r.contribution or 0.0 for r in report.rows)
        assert contributions == pytest.approx(report.total_return)

    def test_weights_sum_to_one(self) -> None:
        positions = [position("AAA"), position("BBB", acb_native=20.0)]
        valued = value_positions(positions, {"AAA": 15.0, "BBB": 18.0}, {"CAD": 1.0})
        report = attribution(valued, {"CAD": 1.0})
        assert sum(r.weight for r in report.rows) == pytest.approx(1.0)

    def test_rows_are_ranked_by_gain(self) -> None:
        positions = [
            position("LOSER", quantity=100, acb_native=20.0),
            position("WINNER", quantity=100, acb_native=10.0),
        ]
        valued = value_positions(
            positions, {"LOSER": 18.0, "WINNER": 15.0}, {"CAD": 1.0}
        )
        report = attribution(valued, {"CAD": 1.0})
        assert [r.ticker for r in report.rows] == ["WINNER", "LOSER"]
        assert [r.ticker for r in report.winners] == ["WINNER"]
        assert [r.ticker for r in report.losers] == ["LOSER"]

    def test_price_and_fx_totals_reconcile_to_total_gain(self) -> None:
        positions = [
            position("DOM", quantity=100, acb_native=10.0),
            position("FGN", quantity=100, acb_native=10.0, acb_base=12.0, currency="USD"),
        ]
        valued = value_positions(positions, {"DOM": 15.0, "FGN": 20.0}, {"CAD": 1.0, "USD": 1.5})
        report = attribution(valued, {"CAD": 1.0, "USD": 1.5})
        assert report.price_gain + report.fx_gain == pytest.approx(report.total_gain)

    def test_unpriced_holdings_are_named(self) -> None:
        valued = value_positions(
            [position("AAA"), position("DARK")], {"AAA": 15.0}, {"CAD": 1.0}
        )
        report = attribution(valued, {"CAD": 1.0})
        assert report.unpriced == ("DARK",)
        # and the priced totals are not polluted by the unpriced holding
        assert report.total_book == pytest.approx(1000.0)
