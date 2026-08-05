"""Contribution-room rules.

Amounts here are the published limits from `data/jurisdictions/ca.yaml`, which
are public facts. The personal inputs — year of birth, account open year, what
was actually contributed — are supplied per test and belong to nobody.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from desk.jurisdictions.base import UnknownRoomGroup
from desk.jurisdictions.ca import CanadaJurisdiction, GenericJurisdiction, get_jurisdiction


@dataclass(frozen=True)
class Contribution:
    date: dt.date
    amount: float


def contrib(year: int, amount: float, month: int = 6) -> Contribution:
    return Contribution(dt.date(year, month, 1), amount)


CA = CanadaJurisdiction()


class TestTfsaRoom:
    def test_room_accrues_from_the_year_the_holder_turns_eighteen(self) -> None:
        """Someone born in 2000 turns 18 in 2018, so room starts that year and
        the 2009-2017 limits never accrue to them."""
        room = CA.room("ca:tfsa", [], 2019, {"birth_year": 2000})
        assert room.lifetime_limit == pytest.approx(5500 + 6000)

    def test_someone_of_age_since_inception_accrues_from_the_first_year(self) -> None:
        room = CA.room("ca:tfsa", [], 2010, {"birth_year": 1980})
        assert room.lifetime_limit == pytest.approx(10000)

    def test_unused_room_carries_forward_without_limit(self) -> None:
        room = CA.room("ca:tfsa", [], 2015, {"birth_year": 1980})
        expected = 5000 * 4 + 5500 * 2 + 10000
        assert room.lifetime_limit == pytest.approx(expected)
        assert room.available_this_year == pytest.approx(expected)

    def test_contributions_reduce_available_room(self) -> None:
        room = CA.room("ca:tfsa", [contrib(2019, 6000)], 2019, {"birth_year": 2000})
        assert room.available_this_year == pytest.approx(5500)
        assert room.contributed_this_year == pytest.approx(6000)

    def test_over_contribution_is_reported_not_clamped(self) -> None:
        room = CA.room("ca:tfsa", [contrib(2019, 20000)], 2019, {"birth_year": 2000})
        assert room.available_this_year < 0
        assert room.over_contributed is True

    def test_room_is_pooled_across_accounts_in_the_group(self) -> None:
        """Two accounts at different custodians draw on one CRA limit. The
        reference could only model this by merging them into a single bucket
        and losing per-account reporting."""
        both = [contrib(2019, 3000), contrib(2019, 2000)]
        room = CA.room("ca:tfsa", both, 2019, {"birth_year": 2000})
        assert room.contributed_this_year == pytest.approx(5000)
        assert room.available_this_year == pytest.approx(5500 + 6000 - 5000)

    def test_missing_birth_year_explains_itself_rather_than_guessing(self) -> None:
        room = CA.room("ca:tfsa", [], 2024, {})
        assert room.available_this_year is None
        assert "year of birth" in room.notes[0]

    def test_a_year_beyond_the_published_table_says_so(self) -> None:
        room = CA.room("ca:tfsa", [], 2031, {"birth_year": 1980})
        assert any("No published limit beyond" in n for n in room.notes)


class TestFhsaRoom:
    def test_no_room_before_the_account_was_opened(self) -> None:
        room = CA.room("ca:fhsa", [], 2024, {"fhsa_open_year": 2025})
        assert room.available_this_year == pytest.approx(0.0)

    def test_first_year_room_is_one_annual_limit(self) -> None:
        room = CA.room("ca:fhsa", [], 2025, {"fhsa_open_year": 2025})
        assert room.available_this_year == pytest.approx(8000)

    def test_carryforward_is_capped_at_one_year(self) -> None:
        """Unlike the TFSA, three idle years do not accumulate three years of
        room — the cap applies each year, so it does not simply add up."""
        room = CA.room("ca:fhsa", [], 2028, {"fhsa_open_year": 2025})
        assert room.available_this_year == pytest.approx(16000)

    def test_a_contribution_consumes_carryforward_first(self) -> None:
        room = CA.room("ca:fhsa", [contrib(2026, 10000)], 2026, {"fhsa_open_year": 2025})
        assert room.available_this_year == pytest.approx(6000)

    def test_the_lifetime_cap_binds_independently(self) -> None:
        used = [contrib(y, 8000) for y in range(2025, 2030)]
        room = CA.room("ca:fhsa", used, 2030, {"fhsa_open_year": 2025})
        assert room.contributed_lifetime == pytest.approx(40000)
        assert room.available_this_year == pytest.approx(0.0)

    def test_missing_open_year_explains_itself(self) -> None:
        room = CA.room("ca:fhsa", [], 2026, {})
        assert room.available_this_year is None
        assert "opened" in room.notes[0]


class TestRrspRoom:
    def test_room_cannot_be_inferred_and_says_so(self) -> None:
        room = CA.room("ca:rrsp", [contrib(2026, 5000)], 2026, {})
        assert room.available_this_year is None
        assert room.contributed_this_year == pytest.approx(5000)
        assert "notice of assessment" in room.notes[0]

    def test_a_declared_limit_is_used(self) -> None:
        room = CA.room("ca:rrsp", [contrib(2026, 5000)], 2026, {"rrsp_deduction_limit": 18000})
        assert room.available_this_year == pytest.approx(13000)


class TestGenericJurisdiction:
    def test_reports_unlimited_but_still_tracks_contributions(self) -> None:
        """The degradation path: a working app for someone with no registered
        accounts, rather than a Canada-shaped assumption imposed on them."""
        room = GenericJurisdiction().room("anything", [contrib(2026, 1000)], 2026, {})
        assert room.unlimited is True
        assert room.contributed_this_year == pytest.approx(1000)
        assert room.over_contributed is False


class TestRegistry:
    def test_known_id_resolves(self) -> None:
        assert get_jurisdiction("ca").id == "ca"

    def test_unknown_id_degrades_to_generic_rather_than_failing(self) -> None:
        assert get_jurisdiction("atlantis").id == "generic"

    def test_an_unknown_room_group_is_an_error(self) -> None:
        with pytest.raises(UnknownRoomGroup):
            CA.room("ca:not_a_thing", [], 2026, {})

    def test_labels_come_from_the_data_file(self) -> None:
        labels = CA.room_group_labels()
        assert "ca:tfsa" in labels and "ca:fhsa" in labels
