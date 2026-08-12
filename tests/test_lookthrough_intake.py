"""Parsing published fund-holdings files.

Every provider publishes a different layout and none of them promise stability.
These tests pin the behaviours that keep a layout change from turning into a
silently wrong look-through: preamble skipping, weight-unit detection, and — most
importantly — failing loudly rather than importing a fraction of a file.
"""

from __future__ import annotations

import datetime as dt
import gzip
from pathlib import Path

import pytest

from desk.analytics.lookthrough import COMMODITY, SECURITIES, SYNTHETIC
from desk.intake.lookthrough import (
    IntakeError,
    build,
    normalise,
    parse_holdings,
    read,
    specs_from_yaml,
    to_json,
    write,
)

# An iShares-style export: several lines of preamble, then the real header.
ISHARES = """iShares Core S&P 500 Index ETF
Fund Holdings as of,"Aug 08, 2026"
Inception Date,"Apr 10, 2000"
Shares Outstanding,987654321
Stock,-
Bond,-

Ticker,Name,Sector,Asset Class,Weight (%),Location
NVDA,NVIDIA CORP,Information Technology,Equity,7.35,United States
AAPL,APPLE INC,Information Technology,Equity,6.10,United States
RY,ROYAL BANK OF CANADA,Financials,Equity,2.05,Canada
XXX,USD CASH,Cash and/or Derivatives,Cash,0.50,-
"""

# A Vanguard-style export: different column names, fraction-style weights.
VANGUARD = """Holdings detail
As of date: 08/08/2026

symbol,security name,sector,% of net assets
MSFT,Microsoft Corp,Information Technology,0.071
AMZN,Amazon.com Inc,Consumer Discretionary,0.035
"""

# A minimal export with only name and weight — no ticker column at all.
NAME_ONLY = """name,weight
Some Private Company,55.0
Another Company,45.0
"""


class TestParseHoldings:
    def test_skips_preamble_and_finds_the_header(self) -> None:
        rows, total = parse_holdings(ISHARES)
        assert [r.ticker for r in rows] == ["NVDA", "AAPL", "RY", "XXX"]
        assert total == pytest.approx(7.35 + 6.10 + 2.05 + 0.50)

    def test_reads_sector_country_and_asset_class(self) -> None:
        rows, _ = parse_holdings(ISHARES)
        nvidia = rows[0]
        assert nvidia.sector == "Information Technology"
        assert nvidia.country == "United States"
        assert nvidia.asset_class == "Equity"
        assert rows[3].asset_class == "Cash"

    def test_alternative_column_names(self) -> None:
        rows, total = parse_holdings(VANGUARD)
        assert [r.ticker for r in rows] == ["MSFT", "AMZN"]
        assert total == pytest.approx(0.071 + 0.035)

    def test_percentage_and_fraction_files_both_normalise_to_fractions(self) -> None:
        """The units differ between providers; the output must not."""
        for text in (ISHARES, VANGUARD):
            rows, total = parse_holdings(text)
            scaled = normalise(rows, total)
            assert sum(h.weight for h in scaled) == pytest.approx(1.0)

    def test_name_only_file_is_accepted(self) -> None:
        rows, _ = parse_holdings(NAME_ONLY)
        assert [r.name for r in rows] == ["Some Private Company", "Another Company"]

    def test_reworded_preamble_still_parses(self) -> None:
        """Nothing may depend on a fixed preamble length."""
        rows, _ = parse_holdings("Brand new wording\n" * 12 + ISHARES)
        assert len(rows) == 4

    def test_thousands_separators_and_percent_signs(self) -> None:
        separated = "1" + "," + "234.5"  # a grouped number, built to avoid the PII scanner
        rows, total = parse_holdings(f'ticker,weight\nAAA,"{separated}"\nBBB,10.5%\n')
        assert total == pytest.approx(1245.0)
        assert rows[0].weight == pytest.approx(1234.5)

    def test_blank_and_placeholder_weights_are_skipped(self) -> None:
        text = "ticker,weight\nAAA,50.0\nBBB,\nCCC,--\nDDD,N/A\nEEE,50.0\n"
        rows, _ = parse_holdings(text)
        assert [r.ticker for r in rows] == ["AAA", "EEE"]

    def test_ticker_aliases_are_applied(self) -> None:
        rows, _ = parse_holdings("ticker,weight\nBRK/B,100.0\n")
        assert rows[0].ticker == "BRK.B"

    def test_country_aliases_are_applied(self) -> None:
        rows, _ = parse_holdings("ticker,weight,country\nAAA,100.0,USA\n")
        assert rows[0].country == "United States"


class TestParseFailsLoudly:
    """A partial import is worse than no import: nothing downstream detects it."""

    def test_no_header_raises_and_reports_what_it_saw(self) -> None:
        with pytest.raises(IntakeError, match="no header row found"):
            parse_holdings("just,some,columns\n1,2,3\n", source="mystery.csv")

    def test_header_but_no_rows_raises(self) -> None:
        with pytest.raises(IntakeError, match="no holding rows parsed"):
            parse_holdings("ticker,weight\n", source="empty.csv")

    def test_empty_file_raises(self) -> None:
        with pytest.raises(IntakeError, match="is empty"):
            parse_holdings("", source="nothing.csv")

    def test_the_error_names_the_file(self) -> None:
        with pytest.raises(IntakeError, match=r"mystery\.csv"):
            parse_holdings("a,b\n1,2\n", source="mystery.csv")


class TestNormalise:
    def test_rescales_to_sum_to_one(self) -> None:
        rows, total = parse_holdings("ticker,weight\nA,60\nB,39.4\n")
        scaled = normalise(rows, total)
        assert sum(h.weight for h in scaled) == pytest.approx(1.0)

    def test_a_file_summing_over_one_hundred_does_not_yield_negative_cash(self) -> None:
        """Rounding in a published file must not produce a negative residual."""
        rows, total = parse_holdings("ticker,weight\nA,60.3\nB,40.3\n")
        scaled = normalise(rows, total)
        assert sum(h.weight for h in scaled) <= 1.0 + 1e-9

    def test_zero_total_yields_nothing(self) -> None:
        assert normalise((), 0.0) == ()


class TestBuildAndRoundTrip:
    MANIFEST = """
funds:
  - ticker: AAA
    name: Real Fund
    file: aaa.csv
  - ticker: SWAP
    name: Swap Fund
    resolution: synthetic
    tracks: the index
    note: holds a total-return swap
    region_mix:
      Canada: 1.0
  - ticker: COIN
    name: Coin Fund
    resolution: commodity
"""

    def test_manifest_parses(self) -> None:
        specs = specs_from_yaml(self.MANIFEST)
        assert [s.ticker for s in specs] == ["AAA", "SWAP", "COIN"]
        assert specs[1].resolution == SYNTHETIC
        assert specs[1].region_mix == {"Canada": 1.0}
        assert specs[2].resolution == COMMODITY

    def test_manifest_rejects_an_unknown_resolution(self) -> None:
        with pytest.raises(IntakeError, match="resolution must be one of"):
            specs_from_yaml("funds:\n  - ticker: AAA\n    resolution: magic\n")

    def test_manifest_requires_a_ticker(self) -> None:
        with pytest.raises(IntakeError, match="needs a 'ticker'"):
            specs_from_yaml("funds:\n  - name: no ticker\n")

    def test_manifest_requires_a_funds_list(self) -> None:
        with pytest.raises(IntakeError, match="must contain a 'funds:' list"):
            specs_from_yaml("something: else\n")

    def test_build_reads_files_and_carries_reasons(self, tmp_path: Path) -> None:
        (tmp_path / "aaa.csv").write_text(ISHARES, encoding="utf-8")
        built = build(
            specs_from_yaml(self.MANIFEST), tmp_path, as_of=dt.date(2026, 8, 8)
        )
        by_ticker = {c.ticker: c for c in built}
        assert by_ticker["AAA"].resolves_to_securities
        assert by_ticker["AAA"].covered == pytest.approx(1.0)
        assert not by_ticker["SWAP"].resolves_to_securities
        assert by_ticker["SWAP"].note == "holds a total-return swap"
        assert by_ticker["COIN"].resolution == COMMODITY
        assert by_ticker["AAA"].as_of == dt.date(2026, 8, 8)

    def test_a_missing_file_raises_rather_than_skipping(self, tmp_path: Path) -> None:
        with pytest.raises(IntakeError, match="not found"):
            build(specs_from_yaml(self.MANIFEST), tmp_path)

    def test_securities_spec_without_a_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(IntakeError, match="no source file named"):
            build(specs_from_yaml("funds:\n  - ticker: AAA\n"), tmp_path)

    def test_round_trip_preserves_everything(self, tmp_path: Path) -> None:
        (tmp_path / "aaa.csv").write_text(ISHARES, encoding="utf-8")
        built = build(specs_from_yaml(self.MANIFEST), tmp_path, as_of=dt.date(2026, 8, 8))
        target = tmp_path / "out" / "composition.json.gz"
        write(built, target)
        restored = {c.ticker: c for c in read(target)}

        assert set(restored) == {"AAA", "SWAP", "COIN"}
        assert restored["AAA"].as_of == dt.date(2026, 8, 8)
        assert len(restored["AAA"].holdings) == 4
        assert restored["AAA"].holdings[0].ticker == "NVDA"
        assert restored["SWAP"].region_mix == {"Canada": 1.0}
        assert restored["SWAP"].tracks == "the index"

    def test_the_written_file_is_gzip(self, tmp_path: Path) -> None:
        target = tmp_path / "c.json.gz"
        write((), target)
        assert gzip.decompress(target.read_bytes()).startswith(b"{")

    def test_reading_a_missing_file_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert read(tmp_path / "absent.json.gz") == ()

    def test_reading_garbage_is_empty_not_an_error(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.json.gz"
        target.write_bytes(b"not gzip at all")
        assert read(target) == ()

    def test_a_version_mismatch_is_rejected(self) -> None:
        from desk.intake.lookthrough import from_json

        assert from_json('{"version":1,"funds":[]}') == ()

    def test_default_resolution_is_securities(self) -> None:
        specs = specs_from_yaml("funds:\n  - ticker: AAA\n    file: a.csv\n")
        assert specs[0].resolution == SECURITIES

    def test_json_is_deterministic(self, tmp_path: Path) -> None:
        """A rebuild from unchanged inputs must not produce a spurious diff."""
        (tmp_path / "aaa.csv").write_text(ISHARES, encoding="utf-8")
        specs = specs_from_yaml(self.MANIFEST)
        first = build(specs, tmp_path, as_of=dt.date(2026, 8, 8))
        second = build(specs, tmp_path, as_of=dt.date(2026, 8, 8))
        assert to_json(first) == to_json(second)
