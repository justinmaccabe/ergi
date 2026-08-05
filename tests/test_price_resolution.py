"""Price resolution and staleness.

The rule under test: a price is never invented, and whatever is returned says
where it came from. The reference substituted a hardcoded exchange rate on a
failed fetch, which downstream reads exactly like a real quote.
"""

from __future__ import annotations

import datetime as dt

import pytest

from desk.data.providers.base import (
    coverage,
    resolve_price,
    staleness_report,
    unavailable,
)
from desk.domain.types import PriceSource, Quote

TODAY = dt.date(2026, 3, 10)


def quote(price: float | None, source: PriceSource, days_old: int = 0) -> Quote:
    return Quote(
        symbol="AAA",
        price=price,
        currency="CAD",
        as_of=TODAY - dt.timedelta(days=days_old),
        source=source,
    )


class TestResolutionOrder:
    def test_live_wins(self) -> None:
        resolved = resolve_price(
            "AAA",
            live=quote(10.0, PriceSource.LIVE),
            last_known=quote(9.0, PriceSource.LAST_KNOWN, 3),
            cost=8.0,
        )
        assert resolved.price == pytest.approx(10.0)
        assert resolved.source is PriceSource.LIVE

    def test_falls_back_to_last_known_and_says_so(self) -> None:
        resolved = resolve_price(
            "AAA", live=None, last_known=quote(9.0, PriceSource.LAST_KNOWN, 3), cost=8.0
        )
        assert resolved.price == pytest.approx(9.0)
        assert resolved.source is PriceSource.LAST_KNOWN
        assert resolved.staleness_days(TODAY) == 3

    def test_falls_back_to_cost_clearly_labelled(self) -> None:
        resolved = resolve_price("AAA", cost=8.0, currency="CAD")
        assert resolved.price == pytest.approx(8.0)
        assert resolved.source is PriceSource.COST

    def test_nothing_available_yields_none_not_zero_and_not_a_guess(self) -> None:
        resolved = resolve_price("AAA")
        assert resolved.price is None
        assert resolved.source is PriceSource.UNAVAILABLE
        assert resolved.is_usable is False

    def test_a_zero_live_quote_is_not_usable(self) -> None:
        resolved = resolve_price("AAA", live=quote(0.0, PriceSource.LIVE), cost=8.0)
        assert resolved.source is PriceSource.COST

    def test_a_private_holding_prefers_its_manual_mark(self) -> None:
        """The one case where freshness is not the ordering: an accidental
        symbol collision must not override a deliberate mark."""
        resolved = resolve_price(
            "PRIV",
            live=quote(99.0, PriceSource.LIVE),
            manual=quote(17.0, PriceSource.MANUAL, 40),
            is_private=True,
        )
        assert resolved.price == pytest.approx(17.0)
        assert resolved.source is PriceSource.MANUAL


class TestCoverage:
    def test_all_live_is_full_coverage(self) -> None:
        quotes = {"A": quote(1.0, PriceSource.LIVE), "B": quote(2.0, PriceSource.LIVE)}
        assert coverage(quotes) == pytest.approx(1.0)

    def test_mixed_sources_report_partial_coverage(self) -> None:
        quotes = {
            "A": quote(1.0, PriceSource.LIVE),
            "B": quote(2.0, PriceSource.LAST_KNOWN, 4),
            "C": quote(3.0, PriceSource.COST),
            "D": quote(4.0, PriceSource.MANUAL, 20),
        }
        assert coverage(quotes) == pytest.approx(0.25)

    def test_empty_is_zero_not_an_error(self) -> None:
        assert coverage({}) == pytest.approx(0.0)


class TestStaleness:
    def test_reports_marks_older_than_tolerance_worst_first(self) -> None:
        quotes = {
            "A": quote(1.0, PriceSource.LIVE, 0),
            "B": quote(2.0, PriceSource.LAST_KNOWN, 9),
            "C": quote(3.0, PriceSource.LAST_KNOWN, 30),
        }
        assert staleness_report(quotes, TODAY, max_days=5) == (("C", 30), ("B", 9))

    def test_fresh_marks_are_not_reported(self) -> None:
        quotes = {"A": quote(1.0, PriceSource.LIVE, 1)}
        assert staleness_report(quotes, TODAY, max_days=5) == ()

    def test_an_undated_quote_is_not_treated_as_fresh_or_stale(self) -> None:
        assert staleness_report({"A": unavailable("A")}, TODAY, max_days=5) == ()
