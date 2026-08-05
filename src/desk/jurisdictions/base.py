"""The jurisdiction contract.

Contribution-room rules are a plugin, not a hardcoded assumption. The build this
replaces had Canadian tax constants sitting in its database module and reached
up into them from the math layer, which meant the app could only ever be used by
one person in one country.

`generic` returning unlimited room is the important case: someone with no
registered accounts, or in a jurisdiction nobody has written rules for, gets a
fully working application that tracks contributions without pretending to know
a limit.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from desk.domain.types import Room


@runtime_checkable
class Jurisdiction(Protocol):
    """Contribution-room rules for one tax jurisdiction."""

    id: str

    def room_group_labels(self) -> Mapping[str, str]:
        """Room group identifier -> human label."""
        ...

    def room(
        self,
        group: str,
        contributions: Sequence[Contribution],
        year: int,
        params: Mapping[str, object],
    ) -> Room:
        """Compute room for one group in one year.

        `contributions` covers the group across every account sharing it — the
        whole point of a room group being that separate accounts at separate
        custodians draw on one limit.
        """
        ...


class Contribution(Protocol):
    """The shape the room calculation needs from a contribution row."""

    date: dt.date
    amount: float


class UnknownRoomGroup(KeyError):
    """The configuration names a room group this jurisdiction does not define."""
