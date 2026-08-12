"""Known-answer tests for the factor regression.

The load-bearing case is `test_recovers_known_betas`: a return series is built
*from* a chosen set of loadings, so the regression's job is to recover numbers we
already know. A factor model is easy to write in a way that produces
plausible-looking output and wrong coefficients — a sign error, a misaligned
index, or forgetting to subtract the risk-free rate all yield betas that look
reasonable on a chart. Constructing the answer first is the only way to catch it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from desk.analytics.factors import (
    FACTORS,
    MIN_MONTHS,
    blended_r_squared,
    contribution,
    factor_exposure,
    monthly_returns,
    regress,
    summarise,
    tilt_summary,
    to_usd,
)


def factor_frame(months: int = 60, seed: int = 7) -> pd.DataFrame:
    """A synthetic factor frame with the published column names and units."""
    rng = np.random.default_rng(seed)
    index = pd.date_range("2020-01-31", periods=months, freq="ME")
    data = {name: rng.normal(0.004, 0.04, months) for name in FACTORS}
    data["RF"] = np.full(months, 0.002)
    return pd.DataFrame(data, index=index)


def series_from_betas(
    factors: pd.DataFrame, betas: dict[str, float], alpha: float = 0.0
) -> pd.Series:
    """Build a return series that a correct regression must reproduce."""
    excess = sum(betas.get(name, 0.0) * factors[name] for name in FACTORS)
    return excess + alpha + factors["RF"]


class TestRegress:
    def test_recovers_known_betas(self) -> None:
        factors = factor_frame()
        truth = {"Mkt-RF": 1.05, "SMB": -0.30, "HML": 0.42, "RMW": 0.15, "CMA": -0.08, "Mom": 0.22}
        fit = regress(series_from_betas(factors, truth, alpha=0.001), factors)

        assert fit is not None
        for name, expected in truth.items():
            assert fit.betas[name] == pytest.approx(expected, abs=1e-9)
        assert fit.alpha == pytest.approx(0.001, abs=1e-9)
        # A series constructed entirely from the factors is explained entirely.
        assert fit.r_squared == pytest.approx(1.0, abs=1e-9)
        assert fit.months == 60

    def test_risk_free_rate_is_subtracted(self) -> None:
        """Regressing raw rather than excess return inflates alpha by RF.

        With RF at 0.002/month, forgetting the subtraction shows up as roughly
        +0.2% of monthly alpha out of nowhere.
        """
        factors = factor_frame()
        fit = regress(series_from_betas(factors, {"Mkt-RF": 1.0}, alpha=0.0), factors)
        assert fit is not None
        assert fit.alpha == pytest.approx(0.0, abs=1e-9)

    def test_short_history_yields_no_fit(self) -> None:
        factors = factor_frame(months=MIN_MONTHS - 1)
        series = series_from_betas(factors, {"Mkt-RF": 1.0})
        assert regress(series, factors) is None

    def test_exactly_the_minimum_is_enough(self) -> None:
        factors = factor_frame(months=MIN_MONTHS)
        series = series_from_betas(factors, {"Mkt-RF": 1.0})
        fit = regress(series, factors)
        assert fit is not None and fit.months == MIN_MONTHS

    def test_missing_factor_column_is_refused(self) -> None:
        factors = factor_frame().drop(columns=["RMW"])
        series = series_from_betas(factor_frame(), {"Mkt-RF": 1.0})
        assert regress(series, factors) is None

    def test_empty_inputs_return_none(self) -> None:
        assert regress(pd.Series(dtype=float), factor_frame()) is None
        assert regress(series_from_betas(factor_frame(), {}), pd.DataFrame()) is None

    def test_only_overlapping_months_are_used(self) -> None:
        """A fund younger than the factor window is fitted on the overlap only."""
        factors = factor_frame(months=60)
        full = series_from_betas(factors, {"Mkt-RF": 1.0})
        fit = regress(full.iloc[-30:], factors)
        assert fit is not None and fit.months == 30


class TestFactorExposure:
    def test_portfolio_loading_is_the_weighted_average(self) -> None:
        factors = factor_frame()
        returns = {
            "AAA": series_from_betas(factors, {"Mkt-RF": 1.2}),
            "BBB": series_from_betas(factors, {"Mkt-RF": 0.8}),
        }
        exposure = factor_exposure(returns, factors, {"AAA": 750.0, "BBB": 250.0})

        # 0.75 * 1.2 + 0.25 * 0.8 = 1.1
        assert exposure.portfolio["Mkt-RF"] == pytest.approx(1.1, abs=1e-8)
        assert exposure.unattributed == pytest.approx(0.0)
        assert {f.ticker for f in exposure.fits} == {"AAA", "BBB"}

    def test_unfittable_weight_is_reported_not_redistributed(self) -> None:
        """The whole point of `unattributed`.

        A holding with no history must not vanish: renormalising over the rest
        would restate a two-thirds-covered portfolio as fully covered.
        """
        factors = factor_frame()
        returns = {"AAA": series_from_betas(factors, {"Mkt-RF": 1.0})}
        exposure = factor_exposure(returns, factors, {"AAA": 600.0, "PRIVATE": 400.0})

        assert exposure.unattributed == pytest.approx(0.4)
        assert exposure.excluded == ("PRIVATE",)
        # The fitted holding still carries full weight *within* the fitted set,
        # so the reported loading is a true average of what was measured.
        assert exposure.fits[0].weight == pytest.approx(1.0)
        assert exposure.portfolio["Mkt-RF"] == pytest.approx(1.0, abs=1e-8)

    def test_weights_within_the_fitted_set_sum_to_one(self) -> None:
        factors = factor_frame()
        returns = {
            "AAA": series_from_betas(factors, {"Mkt-RF": 1.0}),
            "BBB": series_from_betas(factors, {"Mkt-RF": 1.0}),
        }
        exposure = factor_exposure(returns, factors, {"AAA": 300.0, "BBB": 100.0, "DARK": 600.0})
        assert sum(f.weight for f in exposure.fits) == pytest.approx(1.0)
        assert exposure.unattributed == pytest.approx(0.6)

    def test_contributions_decompose_the_portfolio_loading(self) -> None:
        factors = factor_frame()
        returns = {
            "AAA": series_from_betas(factors, {"HML": 0.6}),
            "BBB": series_from_betas(factors, {"HML": -0.2}),
        }
        exposure = factor_exposure(returns, factors, {"AAA": 400.0, "BBB": 600.0})
        parts = contribution(exposure, "HML")
        assert sum(v for _, v in parts) == pytest.approx(exposure.portfolio["HML"], abs=1e-9)

    def test_empty_when_nothing_can_be_fitted(self) -> None:
        exposure = factor_exposure({}, factor_frame(), {"AAA": 100.0})
        assert exposure.is_empty
        assert exposure.unattributed == pytest.approx(1.0)
        assert blended_r_squared(exposure) is None
        assert summarise(exposure).empty

    def test_window_reports_the_span_actually_regressed(self) -> None:
        """Not the factor library's range.

        The library publishes from 1990; a fund four years old is fitted on four
        years. Reporting the library's range would claim three decades of
        evidence behind an estimate that has four years — the more flattering
        number and the wrong one.
        """
        factors = factor_frame(months=120)  # ten years of factors
        full = series_from_betas(factors, {"Mkt-RF": 1.0})
        young = full.iloc[-30:]  # a fund with thirty months of history
        exposure = factor_exposure({"YOUNG": young}, factors, {"YOUNG": 100.0})

        assert exposure.fits[0].months == 30
        assert exposure.window is not None
        # Thirty months back from the end of the factor frame, not its start.
        assert exposure.window[0] != factors.index.min().strftime("%b %Y")
        assert exposure.window[1] == factors.index.max().strftime("%b %Y")

    def test_window_spans_the_union_across_holdings(self) -> None:
        factors = factor_frame(months=120)
        full = series_from_betas(factors, {"Mkt-RF": 1.0})
        exposure = factor_exposure(
            {"OLD": full, "YOUNG": full.iloc[-30:]},
            factors,
            {"OLD": 100.0, "YOUNG": 100.0},
        )
        assert exposure.window == (
            factors.index.min().strftime("%b %Y"),
            factors.index.max().strftime("%b %Y"),
        )

    def test_no_fits_leaves_the_window_unset(self) -> None:
        assert factor_exposure({}, factor_frame(), {}).window is None

    def test_summary_frame_has_a_column_per_factor(self) -> None:
        factors = factor_frame()
        returns = {"AAA": series_from_betas(factors, {"Mkt-RF": 1.0})}
        frame = summarise(factor_exposure(returns, factors, {"AAA": 100.0}))
        for name in FACTORS:
            assert name in frame.columns
        assert list(frame["Ticker"]) == ["AAA"]


class TestCurrencyConversion:
    def test_conversion_multiplies_prices(self) -> None:
        index = pd.date_range("2024-01-01", periods=3, freq="D")
        prices = pd.Series([10.0, 11.0, 12.0], index=index)
        rate = pd.Series([0.75, 0.75, 0.75], index=index)
        assert list(to_usd(prices, rate)) == pytest.approx([7.5, 8.25, 9.0])

    def test_missing_rate_days_are_carried_forward(self) -> None:
        """A market holiday in the FX series must not drop a price day."""
        index = pd.date_range("2024-01-01", periods=3, freq="D")
        prices = pd.Series([10.0, 11.0, 12.0], index=index)
        rate = pd.Series([0.75], index=index[:1])
        assert len(to_usd(prices, rate)) == 3

    def test_currency_move_alone_produces_a_return(self) -> None:
        """The reason conversion happens before differencing.

        A flat CAD price with a falling CAD still lost money in USD, and the
        factor regression must see that.
        """
        index = pd.date_range("2024-01-31", periods=3, freq="ME")
        flat = pd.Series([10.0, 10.0, 10.0], index=index)
        weakening = pd.Series([0.75, 0.70, 0.65], index=index)
        returns = monthly_returns(to_usd(flat, weakening))
        assert (returns < 0).all()

    def test_empty_rate_series_leaves_prices_untouched(self) -> None:
        prices = pd.Series([1.0, 2.0], index=pd.date_range("2024-01-01", periods=2))
        assert list(to_usd(prices, pd.Series(dtype=float))) == [1.0, 2.0]


class TestEligibility:
    """Which holdings an equity-factor regression should be applied to at all."""

    @dataclass(frozen=True)
    class Inst:
        ticker: str
        asset_class: str | None = None

    def test_unset_asset_class_defaults_to_eligible(self) -> None:
        from desk.services.factors import factor_eligible

        assert factor_eligible([self.Inst("VFV"), self.Inst("ZEB")]) == {"VFV", "ZEB"}

    def test_equity_synonyms_are_eligible(self) -> None:
        from desk.services.factors import factor_eligible

        instruments = [self.Inst("A", "equity"), self.Inst("B", "Equities"), self.Inst("C", "ETF")]
        assert factor_eligible(instruments) == {"A", "B", "C"}

    def test_a_digital_asset_is_excluded(self) -> None:
        from desk.services.factors import factor_eligible

        instruments = [self.Inst("VFV"), self.Inst("FBTC", "digital_asset")]
        assert factor_eligible(instruments) == {"VFV"}

    def test_a_commodity_is_excluded(self) -> None:
        from desk.services.factors import factor_eligible

        assert factor_eligible([self.Inst("GLD", "commodity")]) == set()

    def test_restricted_to_held_tickers(self) -> None:
        from desk.services.factors import factor_eligible

        instruments = [self.Inst("VFV"), self.Inst("UNHELD")]
        assert factor_eligible(instruments, ["VFV"]) == {"VFV"}

    def test_an_excluded_holding_keeps_its_weight_as_unattributed(self) -> None:
        """The property that makes exclusion honest rather than a quiet omission."""
        factors = factor_frame()
        returns = {"VFV": series_from_betas(factors, {"Mkt-RF": 1.0})}
        exposure = factor_exposure(returns, factors, {"VFV": 900.0, "FBTC": 100.0})
        assert exposure.unattributed == pytest.approx(0.1)
        assert "FBTC" in exposure.excluded
        # And the portfolio loading is the equity sleeve's, undragged.
        assert exposure.portfolio["Mkt-RF"] == pytest.approx(1.0, abs=1e-8)


class TestKenFrenchParsing:
    """The published file layout is hostile to naive parsing.

    A real Developed_5_Factors CSV carries citation text, then the monthly table,
    then a second *annual* table keyed by YYYY, then more prose. Splicing the
    annual rows onto the monthly ones yields returns roughly twelve times too
    large for recent years — which looks like a spectacular run rather than a
    parse bug, so it is exactly the failure that survives review.
    """

    FILE_BODY = (
        "This file was created by CMPT_ME_BEVA_5F using the 202512 CRSP database.\n"
        "The Tmv and TFF factors are constructed from the six size-BE/ME portfolios.\n"
        "\n"
        ",Mkt-RF,SMB,HML,RMW,CMA,RF\n"
        "202001,  -0.11,   1.20,  -2.30,   0.45,  -0.60,   0.13\n"
        "202002,  -8.13,   0.90,  -3.10,   1.05,  -0.20,   0.12\n"
        "202003, -13.39,  -1.50,  -4.40,   2.15,   0.80,   0.12\n"
        "\n"
        " Annual Factors: January-December\n"
        "\n"
        ",Mkt-RF,SMB,HML,RMW,CMA,RF\n"
        "2020,  16.25,  13.20, -46.60,   4.55,  -8.20,   0.45\n"
        "2021,  18.70,  -2.40,  10.20,   6.15,   3.80,   0.04\n"
        "\n"
        "Copyright 2026 Kenneth R. French\n"
    )

    def _zipped(self, body: str) -> bytes:
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Developed_5_Factors.csv", body)
        return buffer.getvalue()

    def _parse(self, body: str) -> pd.DataFrame:
        from desk.data.providers.factors import FIVE_FACTOR_COLUMNS, KenFrenchFactors

        return KenFrenchFactors()._parse(self._zipped(body), FIVE_FACTOR_COLUMNS)

    def test_reads_only_the_monthly_table(self) -> None:
        frame = self._parse(self.FILE_BODY)
        assert len(frame) == 3
        assert list(frame.index.strftime("%Y-%m")) == ["2020-01", "2020-02", "2020-03"]

    def test_percentages_become_decimals(self) -> None:
        frame = self._parse(self.FILE_BODY)
        assert frame.loc[frame.index[1], "Mkt-RF"] == pytest.approx(-8.13 / 100)
        assert frame.loc[frame.index[0], "RF"] == pytest.approx(0.13 / 100)

    def test_index_is_month_end(self) -> None:
        frame = self._parse(self.FILE_BODY)
        assert frame.index[0].day == 31
        assert frame.index[1].day == 29  # 2020 was a leap year

    def test_missing_data_sentinel_becomes_nan(self) -> None:
        """-99.99 must not enter a regression as a -9999% return."""
        body = self.FILE_BODY.replace(
            "202002,  -8.13,   0.90,  -3.10,   1.05,  -0.20,   0.12",
            "202002, -99.99, -99.99, -99.99, -99.99, -99.99, -99.99",
        )
        frame = self._parse(body)
        assert frame.loc[frame.index[1]].isna().all()

    def test_reworded_preamble_does_not_break_it(self) -> None:
        """Nothing depends on a fixed number of header lines."""
        body = "Entirely new wording.\n" * 9 + self.FILE_BODY
        assert len(self._parse(body)) == 3

    def test_corrupt_archive_returns_empty_rather_than_raising(self) -> None:
        from desk.data.providers.factors import FIVE_FACTOR_COLUMNS, KenFrenchFactors

        assert KenFrenchFactors()._parse(b"not a zip", FIVE_FACTOR_COLUMNS).empty

    def test_no_monthly_rows_returns_empty(self) -> None:
        assert self._parse("prose only, no data\n").empty

    def test_disabled_provider_is_empty_not_an_error(self) -> None:
        from desk.data.providers.factors import get_provider

        assert get_provider("none").load().empty
        assert get_provider("kenfrench").name == "kenfrench"


class TestTiltSummary:
    def test_describes_only_meaningful_loadings(self) -> None:
        factors = factor_frame()
        returns = {"AAA": series_from_betas(factors, {"Mkt-RF": 1.0, "HML": 0.5, "SMB": 0.01})}
        phrases = " ".join(tilt_summary(factor_exposure(returns, factors, {"AAA": 100.0})))
        assert "value" in phrases
        # 0.01 is indistinguishable from zero at these sample sizes.
        assert "smaller companies" not in phrases

    def test_growth_tilt_is_the_negative_of_value(self) -> None:
        factors = factor_frame()
        returns = {"AAA": series_from_betas(factors, {"Mkt-RF": 1.0, "HML": -0.5})}
        phrases = " ".join(tilt_summary(factor_exposure(returns, factors, {"AAA": 100.0})))
        assert "growth" in phrases
