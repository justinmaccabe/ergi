"""Canadian contribution room.

The limit tables live in `data/jurisdictions/ca.yaml` — a new tax year is a
one-line edit there. Only the accrual arithmetic is here, and the three regimes
genuinely differ:

  TFSA  room accrues from the year you turn 18, carries forward without limit
  FHSA  room accrues only from the year you opened one, carryforward capped at
        one year, and there is a lifetime participation limit
  RRSP  room is a share of prior-year earned income and cannot be inferred at
        all — the holder has to supply it from their notice of assessment

The reference build implemented the first two as free functions in its math
layer that reached back into a database module for the owner's birth year.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from desk.domain.types import Room
from desk.jurisdictions.base import Contribution, UnknownRoomGroup

DATA_FILE = "ca.yaml"


@lru_cache(maxsize=1)
def _tables() -> dict[str, Any]:
    for root in Path(__file__).resolve().parents:
        candidate = root / "data" / "jurisdictions" / DATA_FILE
        if candidate.is_file():
            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            return dict(loaded.get("room_groups", {}))
    raise FileNotFoundError(f"data/jurisdictions/{DATA_FILE} not found")


def _total(contributions: Sequence[Contribution], *, year: int | None = None) -> float:
    if year is None:
        return float(sum(c.amount for c in contributions))
    return float(sum(c.amount for c in contributions if c.date.year == year))


class CanadaJurisdiction:
    id = "ca"

    def room_group_labels(self) -> Mapping[str, str]:
        return {key: str(spec.get("label", key)) for key, spec in _tables().items()}

    def room(
        self,
        group: str,
        contributions: Sequence[Contribution],
        year: int,
        params: Mapping[str, object],
    ) -> Room:
        spec = _tables().get(group)
        if spec is None:
            raise UnknownRoomGroup(f"{group!r} is not a Canadian room group")

        accrual = spec.get("accrual")
        if accrual == "from_age":
            return self._age_based_room(group, spec, contributions, year, params)
        if accrual == "from_account_open":
            return self._open_year_room(group, spec, contributions, year, params)
        return self._declared_room(group, spec, contributions, year, params)

    # -- TFSA: accrues from the year you turn 18, unlimited carryforward -----
    def _age_based_room(
        self,
        group: str,
        spec: Mapping[str, Any],
        contributions: Sequence[Contribution],
        year: int,
        params: Mapping[str, object],
    ) -> Room:
        birth_year = params.get("birth_year")
        if not isinstance(birth_year, int):
            return Room(
                group=group,
                year=year,
                unlimited=False,
                notes=(
                    "Room cannot be computed without a year of birth: it accrues from "
                    "the year the holder turns 18. Set jurisdiction.params.birth_year.",
                ),
            )

        limits: dict[int, float] = {int(k): float(v) for k, v in spec["annual_limits"].items()}
        eligible_from = max(int(spec["first_year"]), birth_year + int(spec["accrual_age"]))

        known_years = [y for y in limits if eligible_from <= y <= year]
        cumulative = sum(limits[y] for y in known_years)

        # Beyond the published table, say so rather than guessing forward.
        notes: list[str] = list(spec.get("notes", []))
        last_published = max(limits) if limits else eligible_from
        if year > last_published:
            notes.append(
                f"No published limit beyond {last_published}; "
                f"room shown excludes {last_published + 1}-{year}."
            )

        contributed_lifetime = _total(contributions)
        contributed_this_year = _total(contributions, year=year)

        return Room(
            group=group,
            year=year,
            unlimited=False,
            available_this_year=cumulative - contributed_lifetime,
            contributed_this_year=contributed_this_year,
            contributed_lifetime=contributed_lifetime,
            lifetime_limit=cumulative,
            carryforward=cumulative - contributed_lifetime - limits.get(year, 0.0),
            notes=tuple(notes),
        )

    # -- FHSA: accrues from the open year, carryforward capped, lifetime cap --
    def _open_year_room(
        self,
        group: str,
        spec: Mapping[str, Any],
        contributions: Sequence[Contribution],
        year: int,
        params: Mapping[str, object],
    ) -> Room:
        open_year = params.get("fhsa_open_year")
        if not isinstance(open_year, int):
            return Room(
                group=group,
                year=year,
                notes=(
                    "Room cannot be computed without the year the account was opened. "
                    "Set jurisdiction.params.fhsa_open_year.",
                ),
            )

        annual = float(spec["annual_limit"])
        max_carry = float(spec["max_carryforward"])
        lifetime = float(spec["lifetime_limit"])
        start = max(int(spec["first_year"]), open_year)

        if year < start:
            return Room(
                group=group,
                year=year,
                available_this_year=0.0,
                contributed_this_year=0.0,
                contributed_lifetime=0.0,
                lifetime_limit=lifetime,
                notes=(f"No room accrues before the account was opened in {start}.",),
            )

        # Walk the years so the carryforward cap is applied each year rather
        # than to a single cumulative total — the cap does not commute.
        carry = 0.0
        available = 0.0
        for y in range(start, year + 1):
            entitlement = min(annual + carry, annual + max_carry)
            used = _total(contributions, year=y)
            carry = max(0.0, min(entitlement - used, max_carry))
            if y == year:
                available = entitlement - used

        contributed_lifetime = _total(contributions)
        # The lifetime participation cap binds independently of annual room.
        available = min(available, lifetime - contributed_lifetime)

        return Room(
            group=group,
            year=year,
            available_this_year=available,
            contributed_this_year=_total(contributions, year=year),
            contributed_lifetime=contributed_lifetime,
            lifetime_limit=lifetime,
            carryforward=carry,
            notes=tuple(spec.get("notes", [])),
        )

    # -- RRSP: cannot be inferred; the holder supplies it ---------------------
    def _declared_room(
        self,
        group: str,
        spec: Mapping[str, Any],
        contributions: Sequence[Contribution],
        year: int,
        params: Mapping[str, object],
    ) -> Room:
        declared = params.get("rrsp_deduction_limit")
        contributed_this_year = _total(contributions, year=year)
        if not isinstance(declared, int | float):
            return Room(
                group=group,
                year=year,
                contributed_this_year=contributed_this_year,
                contributed_lifetime=_total(contributions),
                notes=(
                    "Room depends on prior-year earned income and pension adjustments, "
                    "so it cannot be inferred. Take the figure from your notice of "
                    "assessment and set jurisdiction.params.rrsp_deduction_limit.",
                ),
            )
        return Room(
            group=group,
            year=year,
            available_this_year=float(declared) - contributed_this_year,
            contributed_this_year=contributed_this_year,
            contributed_lifetime=_total(contributions),
            notes=tuple(spec.get("notes", [])),
        )


class GenericJurisdiction:
    """No registered-account rules.

    The honest answer for a taxable-only investor, or anywhere nobody has
    written rules yet. Contributions are still tracked; only the limit is
    absent. This is the degradation path the reference had no concept of.
    """

    id = "generic"

    def room_group_labels(self) -> Mapping[str, str]:
        return {}

    def room(
        self,
        group: str,
        contributions: Sequence[Contribution],
        year: int,
        params: Mapping[str, object],
    ) -> Room:
        return Room(
            group=group,
            year=year,
            unlimited=True,
            contributed_this_year=_total(contributions, year=year),
            contributed_lifetime=_total(contributions),
            notes=("No contribution limit is modelled for this jurisdiction.",),
        )


_REGISTRY: dict[str, Any] = {
    CanadaJurisdiction.id: CanadaJurisdiction,
    GenericJurisdiction.id: GenericJurisdiction,
}


def get_jurisdiction(jurisdiction_id: str) -> Any:
    """Look up a jurisdiction by id, defaulting to generic rather than failing.

    An unknown id degrades to unlimited room with a note, so a config typo
    costs a warning rather than a dead application.
    """
    factory = _REGISTRY.get(jurisdiction_id, GenericJurisdiction)
    return factory()


def today_year(clock: dt.date | None = None) -> int:
    return (clock or dt.date.today()).year
