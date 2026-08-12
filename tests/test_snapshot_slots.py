"""Slot resolution and the daily-move measurement.

The DST cases are the reason this file exists. GitHub's scheduler is UTC and
DST-blind, so each local target is registered twice and exactly one variant must
win — on both sides of the changeover. Getting this wrong is not a crash, it is
two snapshots on one day for half the year and a snapshot an hour before the
open for the other half, which is the kind of fault that is only visible months
later in a chart nobody can explain.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from desk.services.market import daily_move, resolve_slot

TORONTO = "America/Toronto"

# The four crons the workflow registers.
OPEN_EDT = "35 13 * * 1-5"
OPEN_EST = "35 14 * * 1-5"
CLOSE_EDT = "5 20 * * 1-5"
CLOSE_EST = "5 21 * * 1-5"

SUMMER = dt.datetime(2026, 7, 15, 9, 40, tzinfo=ZoneInfo(TORONTO))  # EDT, UTC-4
WINTER = dt.datetime(2026, 1, 14, 9, 40, tzinfo=ZoneInfo(TORONTO))  # EST, UTC-5


class TestResolveSlot:
    def test_summer_runs_the_edt_crons_only(self) -> None:
        assert resolve_slot(OPEN_EDT, SUMMER, TORONTO) == "open"
        assert resolve_slot(CLOSE_EDT, SUMMER, TORONTO) == "close"
        assert resolve_slot(OPEN_EST, SUMMER, TORONTO) is None
        assert resolve_slot(CLOSE_EST, SUMMER, TORONTO) is None

    def test_winter_runs_the_est_crons_only(self) -> None:
        assert resolve_slot(OPEN_EST, WINTER, TORONTO) == "open"
        assert resolve_slot(CLOSE_EST, WINTER, TORONTO) == "close"
        assert resolve_slot(OPEN_EDT, WINTER, TORONTO) is None
        assert resolve_slot(CLOSE_EDT, WINTER, TORONTO) is None

    def test_exactly_one_open_and_one_close_per_day_year_round(self) -> None:
        """The property that matters: no duplicates and no gaps, either season."""
        for moment in (SUMMER, WINTER):
            for crons in ((OPEN_EDT, OPEN_EST), (CLOSE_EDT, CLOSE_EST)):
                fired = [resolve_slot(c, moment, TORONTO) for c in crons]
                assert len([s for s in fired if s is not None]) == 1

    def test_a_delayed_run_keeps_its_scheduled_identity(self) -> None:
        """A morning cron that GitHub runs late is still an open, not a close.

        This is why the decision keys off the scheduled cron rather than the
        wall clock: scheduled jobs are routinely delayed under load.
        """
        late = dt.datetime(2026, 7, 15, 15, 20, tzinfo=ZoneInfo(TORONTO))
        assert resolve_slot(OPEN_EDT, late, TORONTO) == "open"

    def test_manual_run_uses_time_of_day(self) -> None:
        morning = dt.datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo(TORONTO))
        afternoon = dt.datetime(2026, 7, 15, 16, 30, tzinfo=ZoneInfo(TORONTO))
        assert resolve_slot(None, morning, TORONTO) == "open"
        assert resolve_slot("", afternoon, TORONTO) == "close"

    def test_naive_datetime_is_read_as_local(self) -> None:
        assert resolve_slot(None, dt.datetime(2026, 7, 15, 10, 0), TORONTO) == "open"

    def test_unrecognised_cron_skips_rather_than_guessing(self) -> None:
        assert resolve_slot("0 3 * * *", SUMMER, TORONTO) is None
        assert resolve_slot("garbage", SUMMER, TORONTO) is None
        assert resolve_slot("* *", SUMMER, TORONTO) is None


@dataclass(frozen=True)
class FakePosition:
    ticker: str
    quantity: float
    currency: str


class TestDailyMove:
    def test_move_is_quantity_times_price_change(self) -> None:
        positions = [FakePosition("AAA", 100.0, "CAD")]
        move, pct = daily_move(positions, {"AAA": 11.0}, {"AAA": 10.0}, {"CAD": 1.0})
        assert move == 100.0
        assert pct is not None
        assert round(pct, 10) == 0.1

    def test_foreign_holding_moves_at_todays_rate(self) -> None:
        positions = [FakePosition("USD_FUND", 10.0, "USD")]
        move, _ = daily_move(positions, {"USD_FUND": 21.0}, {"USD_FUND": 20.0}, {"USD": 1.40})
        assert move is not None
        assert round(move, 6) == 14.0

    def test_holding_without_a_prior_close_is_excluded_from_both_parts(self) -> None:
        """Not merely from the numerator — including its value in the base would
        dilute the percentage toward zero and understate a real move."""
        positions = [FakePosition("AAA", 100.0, "CAD"), FakePosition("NEW", 100.0, "CAD")]
        move, pct = daily_move(positions, {"AAA": 11.0, "NEW": 50.0}, {"AAA": 10.0}, {"CAD": 1.0})
        assert move == 100.0
        assert pct is not None
        assert round(pct, 10) == 0.1  # 100/1000, not 100/6000

    def test_no_priced_holdings_reports_nothing_rather_than_zero(self) -> None:
        """A flat day and an unmeasurable one must not render identically."""
        positions = [FakePosition("AAA", 100.0, "CAD")]
        assert daily_move(positions, {"AAA": 11.0}, {}, {"CAD": 1.0}) == (None, None)
        assert daily_move([], {}, {}, {}) == (None, None)

    def test_a_fall_is_negative(self) -> None:
        positions = [FakePosition("AAA", 100.0, "CAD")]
        move, pct = daily_move(positions, {"AAA": 9.0}, {"AAA": 10.0}, {"CAD": 1.0})
        assert move == -100.0
        assert pct is not None
        assert round(pct, 10) == -0.1
