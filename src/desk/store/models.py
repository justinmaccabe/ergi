"""Database schema. State, never policy and never secrets.

Two departures from the build this replaces are structural rather than cosmetic:

  * **`valuations` is a time series, not a scalar.** Its `manual_price` was a
    single column on the instrument, so every historical snapshot silently
    revalued an illiquid holding at today's mark. A private position's history
    was therefore fiction. Here a mark is an observation with a date, and a
    historical snapshot looks up the mark that was current then.

  * **`staged_transactions` exists.** Parsed statement rows land there and are
    reviewed before they can affect the ledger. A PDF parser that writes
    directly to the books is a corrupted track record waiting for a layout
    change.

There is deliberately no seed data in this module. Seeding is a CLI command
reading from outside the repository, which is the whole reason this project
exists.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    """Securities held or referenced.

    Currency is stored, never inferred from the ticker suffix.
    """

    __tablename__ = "instruments"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    quote_symbol: Mapped[str | None] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="etf")
    name: Mapped[str | None] = mapped_column(String(128))
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Transaction(Base):
    """The ledger. Append-mostly; the source of every position figure."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("instruments.ticker"), nullable=False)
    account_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fees: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Captured at entry and never recomputed. This column is why a cost basis
    # cannot drift with the exchange rate.
    fx_rate: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    # Content hash of the originating statement row, so re-importing the same
    # statement is a no-op rather than a duplicate ledger.
    source_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_tx_ticker_account", "ticker", "account_id"),)


class StagedTransaction(Base):
    """Parsed statement rows awaiting human review.

    Nothing here affects any reported number until it is approved and copied
    into `transactions`.
    """

    __tablename__ = "staged_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_file: Mapped[str] = mapped_column(String(256), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parsed_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    date: Mapped[dt.date | None] = mapped_column(Date)
    ticker: Mapped[str | None] = mapped_column(String(16))
    account_id: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str | None] = mapped_column(String(20))
    quantity: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    fees: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    confidence: Mapped[float | None] = mapped_column(Float)
    parse_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("batch", "source_hash", name="uq_staged_row"),)


class Valuation(Base):
    """Marks for instruments with no market quote.

    A time series, so a snapshot from a year ago uses the mark that was current
    a year ago. The reference stored one scalar per instrument and therefore
    rewrote its own history every time the mark changed.
    """

    __tablename__ = "valuations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("instruments.ticker"), nullable=False)
    as_of: Mapped[dt.date] = mapped_column(Date, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("ticker", "as_of", name="uq_valuation_asof"),)


class PriceCache(Base):
    """Last known good quote per symbol.

    Lets a failed fetch degrade to a dated number rather than to an invented
    one. `as_of` travels with it so the UI can show the age.
    """

    __tablename__ = "price_cache"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    as_of: Mapped[dt.date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())


class FxRate(Base):
    __tablename__ = "fx_rates"

    pair: Mapped[str] = mapped_column(String(7), primary_key=True)
    as_of: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    rate: Mapped[float] = mapped_column(Float, nullable=False)


class Cash(Base):
    __tablename__ = "cash"

    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class ContributionRow(Base):
    __tablename__ = "contributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="contribution")
    note: Mapped[str | None] = mapped_column(Text)


class Snapshot(Base):
    """One row per trading day per slot. Upserted, so a re-run is idempotent."""

    __tablename__ = "snapshots"

    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    slot: Mapped[str] = mapped_column(String(8), primary_key=True)  # open | close
    market_value: Mapped[float | None] = mapped_column(Float)
    book_value: Mapped[float | None] = mapped_column(Float)
    cash_value: Mapped[float | None] = mapped_column(Float)
    daily_pnl: Mapped[float | None] = mapped_column(Float)
    daily_pnl_pct: Mapped[float | None] = mapped_column(Float)
    benchmark_pct: Mapped[float | None] = mapped_column(Float)

    # How much of the market value came from a live quote. A snapshot built
    # mostly from stale marks is not the same measurement as a clean one, and
    # the difference should survive into the history.
    price_coverage: Mapped[float | None] = mapped_column(Float)


class JobRequest(Base):
    """Work the app wants a scheduled runner to do.

    This table is why the web application holds no CI credential. The reference
    kept a GitHub token with Actions-write in its public web app's secret store
    and called the API from UI code; here the app inserts a row and the
    scheduled job services it.
    """

    __tablename__ = "job_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_job_pending", "kind", "status"),)


class AuthEvent(Base):
    """Login attempts.

    The client identifier is stored as a truncated one-way digest: enough to
    see a burst of failures from one source, not enough to keep an address
    next to a portfolio.
    """

    __tablename__ = "auth_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    client_digest: Mapped[str | None] = mapped_column(String(32))


class AppConfig(Base):
    """Configuration as a row, for hosts with a read-only filesystem.

    Streamlit Cloud cannot be written to, so a deployment there keeps its
    config here. The loader resolves file first, then this table, so the rest
    of the codebase never learns which one it got.
    """

    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
