"""Engine and session construction.

The database URL arrives as an argument. This module does not read the
environment — `desk.settings` does that, and CI asserts it.

No implicit SQLite fallback. The reference silently created an empty local file
when its URL was missing, which in a hosted deployment is indistinguishable from
having lost every snapshot ever recorded.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from desk.store.models import Base


def normalise_url(url: str) -> str:
    """Accept the `postgres://` form some providers hand out."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def build_engine(url: str, *, echo: bool = False) -> Engine:
    if not url:
        raise ValueError(
            "a database URL is required. There is no silent local-file fallback: "
            "in a hosted deployment that would look like losing your history."
        )
    normalised = normalise_url(url)
    engine = create_engine(normalised, echo=echo, future=True, pool_pre_ping=True)

    if normalised.startswith("sqlite"):
        # WAL for concurrent reads while the snapshot job writes; foreign keys
        # are off by default in SQLite and silently ignore constraint errors.
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_all(engine: Engine, *, retries: int = 3, delay: float = 2.0) -> None:
    """Create tables, retrying so a sleeping serverless database wakes rather
    than failing the first request after an idle period."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            Base.metadata.create_all(engine)
            return
        except OperationalError as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(delay)
    raise RuntimeError(f"could not reach the database after {retries} attempts: {last}") from last


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
