"""yfinance-backed market data.

Implements the `PriceProvider` and `FxProvider` contracts. The one rule that
matters: never raise on a bad fetch and never invent a number. A missing quote
is `UNAVAILABLE` with `price=None`, a failed history is an empty frame, and the
caller decides what to do — because a confidently-rendered wrong price is worse
than a blank one.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

import pandas as pd

from desk.data.providers.base import unavailable
from desk.domain.types import PriceSource, Quote


def _closes(raw: pd.DataFrame, symbols: Sequence[str]) -> pd.DataFrame:
    """Normalise a yfinance download to a date-indexed frame of adjusted closes."""
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else pd.DataFrame()
    else:  # single symbol → flat columns
        close = raw[["Close"]].rename(columns={"Close": symbols[0]})
    return close.dropna(how="all")


class YFinanceProvider:
    """Quotes and adjusted-close history from Yahoo Finance."""

    name = "yfinance"

    def __init__(self, *, retries: int = 2) -> None:
        self._retries = retries

    def _download(self, symbols: Sequence[str], period: str) -> pd.DataFrame:
        import time

        try:
            import yfinance as yf
        except ImportError:
            return pd.DataFrame()
        for attempt in range(self._retries + 1):
            try:
                raw = yf.download(
                    list(symbols), period=period, auto_adjust=True,
                    progress=False, group_by="column",
                )
                closes = _closes(raw, symbols)
                if not closes.empty:
                    return closes
            except Exception:
                pass
            if attempt < self._retries:
                time.sleep(1.0)
        return pd.DataFrame()

    def history(self, symbols: Sequence[str], period: str) -> pd.DataFrame:
        syms = [s for s in symbols if s]
        return self._download(syms, period) if syms else pd.DataFrame()

    def quotes(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        syms = [s for s in symbols if s]
        if not syms:
            return {}
        hist = self._download(syms, "5d")
        out: dict[str, Quote] = {}
        for s in syms:
            if s in hist.columns:
                col = hist[s].dropna()
                if len(col):
                    as_of = col.index[-1]
                    out[s] = Quote(
                        symbol=s, price=float(col.iloc[-1]), currency="",
                        as_of=as_of.date() if hasattr(as_of, "date") else None,
                        source=PriceSource.LIVE,
                    )
                    continue
            out[s] = unavailable(s)
        return out


class YFinanceFx:
    """FX from Yahoo's `BASEQUOTE=X` symbol (e.g. USDCAD=X)."""

    name = "yfinance"

    def __init__(self, *, retries: int = 2) -> None:
        self._provider = YFinanceProvider(retries=retries)

    def _symbol(self, base: str, quote: str) -> str:
        return f"{base}{quote}=X"

    def rate(self, base: str, quote: str) -> Quote:
        if base == quote:
            return Quote(symbol=f"{base}{quote}", price=1.0, currency=quote,
                         as_of=dt.date.today(), source=PriceSource.LIVE)
        sym = self._symbol(base, quote)
        q = self._provider.quotes([sym]).get(sym)
        if q is None or not q.is_usable:
            return unavailable(sym, quote)
        return Quote(symbol=sym, price=q.price, currency=quote,
                     as_of=q.as_of, source=q.source)

    def series(self, base: str, quote: str, period: str) -> pd.Series:
        if base == quote:
            return pd.Series(dtype=float)
        sym = self._symbol(base, quote)
        hist = self._provider.history([sym], period)
        return hist[sym].dropna() if sym in hist.columns else pd.Series(dtype=float)
