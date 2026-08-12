"""Load the real book from the database into the same shape the demo produces.

This is the leaf-node swap the app shell was built for: the dashboard renders a
`LedgerResult` plus cash and contributions, and neither cares whether those came
from `services.demo` (synthetic) or from here (the ledger in the store).

It takes a database URL as an argument and never reads the environment — that is
`desk.settings`'s job, asserted by the import contracts. Being a service (a
higher layer than the store) it may read the store and call the analytics
engine; the store and analytics never call back up.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml
from sqlalchemy import select

from desk.analytics.positions import LedgerEntry
from desk.domain.types import Action
from desk.store.engine import build_engine, create_all, session_factory, session_scope
from desk.store.models import AppConfig, Cash, ContributionRow, Instrument, Transaction


@dataclass(frozen=True)
class LoadedBook:
    """The real portfolio, in the same fields `services.demo.DemoBook` exposes."""

    entries: tuple[LedgerEntry, ...]
    cash: tuple[tuple[str, str, float], ...]
    contributions: tuple[tuple[dt.date, str, float], ...]


def load(database_url: str) -> LoadedBook:
    """Read transactions, cash and contributions from the store.

    Currency travels from the instrument definition onto each ledger entry, so
    the analytics layer keeps its promise of never inferring currency from a
    ticker suffix.
    """
    engine = build_engine(database_url)
    create_all(engine)
    factory = session_factory(engine)
    with session_scope(factory) as s:
        currency = {i.ticker: i.currency for i in s.execute(select(Instrument)).scalars()}
        entries = tuple(
            LedgerEntry(
                date=t.date,
                ticker=t.ticker,
                account_id=t.account_id,
                action=Action(t.action),
                quantity=t.quantity,
                price=t.price,
                fees=t.fees,
                fx_rate=t.fx_rate,
                currency=currency.get(t.ticker, "CAD"),
            )
            for t in s.execute(select(Transaction).order_by(Transaction.date)).scalars()
        )
        cash = tuple(
            (c.account_id, c.currency, float(c.amount)) for c in s.execute(select(Cash)).scalars()
        )
        contributions = tuple(
            (c.date, c.account_id, float(c.amount))
            for c in s.execute(select(ContributionRow).order_by(ContributionRow.date)).scalars()
        )
    return LoadedBook(entries=entries, cash=cash, contributions=contributions)


def load_config_payload(database_url: str) -> Mapping[str, Any] | None:
    """The config stored as a row, for a read-only host. None if unset.

    This is the callable the app hands to `config.loader.load` as its database
    fallback, so config resolution stays file-then-database-then-example without
    the config layer ever importing the store.
    """
    engine = build_engine(database_url)
    create_all(engine)
    factory = session_factory(engine)
    with session_scope(factory) as s:
        row = s.get(AppConfig, 1)
        if row is None:
            return None
        parsed = yaml.safe_load(row.payload)
    return parsed if isinstance(parsed, Mapping) else None


def save_config_payload(database_url: str, yaml_text: str) -> None:
    """Store the config as a row (id=1, upserted)."""
    engine = build_engine(database_url)
    create_all(engine)
    factory = session_factory(engine)
    with session_scope(factory) as s:
        s.merge(AppConfig(id=1, payload=yaml_text))
