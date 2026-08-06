"""Market-data orchestration: fetch marks, build base-currency history, and
record snapshots. Services may reach the data providers, the analytics engine
and the store; none of those call back up.

Providers are constructed here with sensible defaults but the functions take
plain, cacheable inputs and return plain outputs, so the app can wrap the slow
network calls in its own cache without dragging a provider object through it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

import pandas as pd
from sqlalchemy import select

from desk.data.providers.market import YFinanceFx, YFinanceProvider
from desk.store.engine import build_engine, create_all, session_factory, session_scope
from desk.store.models import Snapshot


def fetch_marks(
    symbols: Sequence[str], currencies: Sequence[str], base: str
) -> tuple[dict[str, float], dict[str, float]]:
    """Latest native prices per symbol and FX rates per currency into base.

    Only usable (live) values are returned; a missing symbol simply does not
    appear, so the caller values what it can and reports coverage on the rest.
    """
    provider = YFinanceProvider()
    fx_provider = YFinanceFx()
    quotes = provider.quotes(list(symbols))
    prices = {s: q.price for s, q in quotes.items() if q.is_usable and q.price is not None}
    fx: dict[str, float] = {}
    for currency in currencies:
        if currency == base:
            fx[currency] = 1.0
            continue
        rate = fx_provider.rate(currency, base)
        if rate.is_usable and rate.price is not None:
            fx[currency] = rate.price
    return prices, fx


def base_history(
    symbols: Mapping[str, str],
    currencies: Mapping[str, str],
    base: str,
    period: str,
) -> pd.DataFrame:
    """Adjusted-close history per ticker, converted to base currency.

    `symbols` maps ticker -> quote symbol; `currencies` maps ticker -> the
    holding's currency. A foreign series is multiplied by the FX series aligned
    on date, so a Canadian investor's USD holding shows the return they actually
    experienced, FX move included.
    """
    provider = YFinanceProvider()
    fx_provider = YFinanceFx()
    raw = provider.history(list(symbols.values()), period)
    if raw.empty:
        return pd.DataFrame()
    fx_cache: dict[str, pd.Series] = {}
    out: dict[str, pd.Series] = {}
    for ticker, symbol in symbols.items():
        if symbol not in raw.columns:
            continue
        series = raw[symbol].dropna()
        currency = currencies.get(ticker, base)
        if currency != base:
            if currency not in fx_cache:
                fx_cache[currency] = fx_provider.series(currency, base, period)
            fx_series = fx_cache[currency]
            if not fx_series.empty:
                series = (series * fx_series.reindex(series.index).ffill().bfill()).dropna()
        out[ticker] = series
    return pd.DataFrame(out).dropna(how="all") if out else pd.DataFrame()


def _slot_for(now: dt.datetime) -> str:
    """Before noon local time is the 'open' record, otherwise 'close'."""
    return "open" if now.hour < 12 else "close"


def record_snapshot(
    database_url: str,
    *,
    market_value: float,
    book_value: float,
    cash_value: float,
    coverage: float,
    on_date: dt.date,
    slot: str,
) -> None:
    """Upsert one snapshot row (idempotent per date+slot)."""
    engine = build_engine(database_url)
    create_all(engine)
    factory = session_factory(engine)
    with session_scope(factory) as s:
        s.merge(
            Snapshot(
                date=on_date,
                slot=slot,
                market_value=market_value,
                book_value=book_value,
                cash_value=cash_value,
                price_coverage=coverage,
            )
        )


def read_snapshots(database_url: str) -> pd.DataFrame:
    """Every recorded snapshot, one row per date+slot, oldest first."""
    engine = build_engine(database_url)
    create_all(engine)
    factory = session_factory(engine)
    with session_scope(factory) as s:
        rows = s.execute(select(Snapshot).order_by(Snapshot.date, Snapshot.slot)).scalars().all()
        data = [
            {
                "date": r.date,
                "slot": r.slot,
                "market_value": r.market_value,
                "book_value": r.book_value,
                "cash_value": r.cash_value,
                "price_coverage": r.price_coverage,
            }
            for r in rows
        ]
    return pd.DataFrame(data)
