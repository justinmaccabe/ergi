"""Fama-French factor returns from the Dartmouth data library.

Separated from `desk.analytics.factors` because that module may not touch the
network — the import contract asserts it. This is the only place that knows the
factors arrive as a zipped CSV with a preamble, several stacked tables, and
percentage units.

The parsing is defensive by necessity. The published files carry a header of
citation text, then an annual table below the monthly one, and occasional
sentinel rows (-99.99) for missing data. A parser that assumed a fixed number of
preamble lines would break silently the next time the citation is reworded, and
a naive read would splice the annual table onto the end of the monthly one —
producing twelve-times-too-large "monthly" returns for recent years. Rows are
therefore taken only when the first field is exactly six digits (YYYYMM).

Like every provider here, this one never raises on a network failure. An empty
frame means the factor tab renders an explanation instead of a chart, which is
the correct outcome — a factor loading regressed against nothing is not a
degraded answer, it is a wrong one.
"""

from __future__ import annotations

import io
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

BASE_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"

# The developed-markets set, not the US-only one. Every holding in a portfolio
# with international exposure is priced partly by non-US factors, and regressing
# a global fund on US factors loads the difference onto alpha.
FIVE_FACTOR_FILE = "Developed_5_Factors"
MOMENTUM_FILE = "Developed_Mom_Factor"

FIVE_FACTOR_COLUMNS = ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF")
MOMENTUM_COLUMNS = ("WML",)

# The library's missing-data sentinel. Left as NaN so a gap drops the month from
# the regression rather than entering it as a -99.99% return.
SENTINEL = -99.99

_MONTH_KEY = re.compile(r"^\d{6}$")


class KenFrenchFactors:
    """Monthly factor returns, cached on disk between runs."""

    name = "kenfrench"

    def __init__(self, *, cache_dir: str | None = None, timeout: int = 30) -> None:
        self._cache = Path(cache_dir) if cache_dir else None
        self._timeout = timeout

    def _fetch_zip(self, stem: str) -> bytes | None:
        """The raw zip, from disk cache if present, else the network."""
        cached = self._cache / f"{stem}_CSV.zip" if self._cache else None
        if cached is not None and cached.exists():
            try:
                return cached.read_bytes()
            except OSError:
                pass
        try:
            with urllib.request.urlopen(BASE_URL + stem + "_CSV.zip", timeout=self._timeout) as r:
                payload: bytes = r.read()
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if cached is not None:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(payload)
            except OSError:
                pass  # a read-only host still gets the data, just uncached
        return payload

    def _parse(self, payload: bytes, columns: tuple[str, ...]) -> pd.DataFrame:
        """The monthly block of one factor file, as decimals indexed by month end."""
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
            names = archive.namelist()
            if not names:
                return pd.DataFrame()
            raw = archive.read(names[0]).decode("latin-1")
        except (zipfile.BadZipFile, OSError, UnicodeDecodeError, KeyError):
            return pd.DataFrame()

        rows: list[list[float | str]] = []
        for line in raw.splitlines():
            parts = [p.strip() for p in line.split(",")]
            # A YYYYMM key in the first field is what distinguishes a monthly
            # observation from the preamble, a blank line, a section heading, or
            # the annual table further down the same file (keyed YYYY).
            if len(parts) < len(columns) + 1 or not _MONTH_KEY.match(parts[0]):
                continue
            try:
                values = [float(parts[i + 1]) for i in range(len(columns))]
            except ValueError:
                continue
            rows.append([parts[0], *values])
        if not rows:
            return pd.DataFrame()

        frame = pd.DataFrame(rows, columns=["month", *columns])
        frame["month"] = pd.to_datetime(frame["month"], format="%Y%m")
        frame = frame.set_index("month")
        frame.index = frame.index.to_period("M").to_timestamp("M")
        numeric = frame.astype(float)
        return numeric.mask(numeric <= SENTINEL) / 100.0

    def load(self) -> pd.DataFrame:
        """The five factors plus momentum and RF, monthly decimals.

        Empty frame on any failure. Momentum is joined rather than required: the
        five-factor file is the load-bearing one, and losing momentum should cost
        one column, not the whole tab.
        """
        five_payload = self._fetch_zip(FIVE_FACTOR_FILE)
        if five_payload is None:
            return pd.DataFrame()
        five = self._parse(five_payload, FIVE_FACTOR_COLUMNS)
        if five.empty:
            return pd.DataFrame()

        momentum_payload = self._fetch_zip(MOMENTUM_FILE)
        if momentum_payload is not None:
            momentum = self._parse(momentum_payload, MOMENTUM_COLUMNS)
            if not momentum.empty:
                return five.join(momentum).rename(columns={"WML": "Mom"})
        # No momentum file: present the column as missing rather than as zero,
        # which would read as "no momentum tilt" instead of "not measured".
        five["Mom"] = float("nan")
        return five


class NoFactors:
    """The `factor_provider: none` case. Always empty, never an error."""

    name = "none"

    def load(self) -> pd.DataFrame:
        return pd.DataFrame()


def get_provider(name: str, *, cache_dir: str | None = None) -> KenFrenchFactors | NoFactors:
    """Resolve the configured provider name to an implementation."""
    return KenFrenchFactors(cache_dir=cache_dir) if name == "kenfrench" else NoFactors()
