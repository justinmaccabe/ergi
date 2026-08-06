"""Risk and return statistics, and the correlation matrix.

Pure: pandas, numpy and `desk.domain` only. Every statistic is optional in
`RiskStats` for a reason — a three-month history should yield fewer numbers,
not fabricated ones, so each is computed only when there is enough data and is
left as None otherwise.

Returns are periodic (monthly by default). The caller supplies an already
base-currency series, because deciding what currency a return is measured in is
a portfolio question, not a statistics question.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from desk.domain.types import RiskStats

MIN_PERIODS = 6


def periodic_returns(values: pd.Series, freq: str = "ME") -> pd.Series:
    """Resample a value series to period-end and return simple returns."""
    if values is None or values.empty:
        return pd.Series(dtype=float)
    resampled = values.resample(freq).last().dropna()
    return resampled.pct_change().dropna()


def max_drawdown(values: pd.Series) -> float | None:
    """Worst peak-to-trough decline of a value series, as a negative fraction."""
    if values is None or len(values) < 2:
        return None
    running_max = values.cummax()
    return float((values / running_max - 1.0).min())


def correlation_matrix(history: pd.DataFrame, freq: str = "ME") -> pd.DataFrame:
    """Correlation of periodic returns between columns.

    Pairwise-complete: two assets with different inception dates are compared
    over the history they share rather than dropping every date one of them
    lacks, which would silently shorten the whole matrix to the youngest asset.
    """
    if history is None or history.empty or history.shape[1] < 2:
        return pd.DataFrame()
    returns = history.resample(freq).last().pct_change(fill_method=None)
    returns = returns.dropna(axis=1, how="all")
    if returns.shape[1] < 2:
        return pd.DataFrame()
    return returns.corr(min_periods=MIN_PERIODS)


def _safe(value: float) -> float | None:
    return None if value is None or not np.isfinite(value) else float(value)


def risk_stats(
    values: pd.Series,
    benchmark: pd.Series | None = None,
    *,
    freq: str = "ME",
    periods_per_year: int = 12,
    risk_free_rate: float = 0.0,
) -> RiskStats:
    """The full suite, computed from a portfolio value series.

    `risk_free_rate` is annual; benchmark-relative statistics are produced only
    when a benchmark is supplied and overlaps the portfolio history.
    """
    returns = periodic_returns(values, freq)
    n = len(returns)
    if n < MIN_PERIODS:
        return RiskStats(periods=n, periods_per_year=periods_per_year)

    r = returns.to_numpy(dtype=float)
    rf_period = risk_free_rate / periods_per_year
    excess = r - rf_period

    arithmetic = float(np.mean(r)) * periods_per_year
    geometric = float(np.prod(1.0 + r) ** (periods_per_year / n) - 1.0)
    vol = float(np.std(r, ddof=1)) * np.sqrt(periods_per_year)
    downside = r[r < rf_period]
    downside_dev = (
        float(np.sqrt(np.mean((downside - rf_period) ** 2))) * np.sqrt(periods_per_year)
        if downside.size
        else None
    )
    mdd = max_drawdown(values.resample(freq).last().dropna())

    ann_excess = float(np.mean(excess)) * periods_per_year
    sharpe = _safe(ann_excess / vol) if vol > 0 else None
    sortino = _safe(ann_excess / downside_dev) if downside_dev else None
    calmar = _safe(geometric / abs(mdd)) if mdd else None

    centred = r - np.mean(r)
    sd = np.std(r, ddof=1)
    skew = _safe(float(np.mean(centred**3) / sd**3)) if sd > 0 else None
    kurt = _safe(float(np.mean(centred**4) / sd**4 - 3.0)) if sd > 0 else None

    var_hist = _safe(float(-np.percentile(r, 5)))
    var_analytical = _safe(float(-(np.mean(r) - 1.645 * sd)))
    tail = r[r <= np.percentile(r, 5)]
    cvar = _safe(float(-np.mean(tail))) if tail.size else None

    gains, losses = r[r > 0], r[r < 0]
    gain_loss = (
        _safe(float(np.mean(gains) / abs(np.mean(losses))))
        if gains.size and losses.size
        else None
    )
    positive = _safe(float(gains.size / n))

    beta = alpha = r_squared = treynor = None
    tracking_error = information_ratio = active_return = None
    up_capture = down_capture = None

    if benchmark is not None and not benchmark.empty:
        bench_returns = periodic_returns(benchmark, freq)
        joined = pd.concat(
            [returns.rename("p"), bench_returns.rename("b")], axis=1
        ).dropna()
        if len(joined) >= MIN_PERIODS:
            p = joined["p"].to_numpy(dtype=float)
            b = joined["b"].to_numpy(dtype=float)
            bench_var = float(np.var(b, ddof=1))
            if bench_var > 0:
                beta = _safe(float(np.cov(p, b, ddof=1)[0, 1] / bench_var))
                if beta is not None:
                    alpha = _safe(
                        (float(np.mean(p)) - rf_period - beta * (float(np.mean(b)) - rf_period))
                        * periods_per_year
                    )
                    treynor = _safe(ann_excess / beta) if beta else None
            corr = float(np.corrcoef(p, b)[0, 1]) if len(p) > 1 else float("nan")
            r_squared = _safe(corr**2)
            diff = p - b
            te = float(np.std(diff, ddof=1)) * np.sqrt(periods_per_year)
            tracking_error = _safe(te)
            geo_b = float(np.prod(1.0 + b) ** (periods_per_year / len(b)) - 1.0)
            geo_p = float(np.prod(1.0 + p) ** (periods_per_year / len(p)) - 1.0)
            active_return = _safe(geo_p - geo_b)
            information_ratio = (
                _safe(active_return / te) if te > 0 and active_return is not None else None
            )
            up, down = b > 0, b < 0
            if up.any() and float(np.mean(b[up])) != 0:
                up_capture = _safe(float(np.mean(p[up]) / np.mean(b[up])))
            if down.any() and float(np.mean(b[down])) != 0:
                down_capture = _safe(float(np.mean(p[down]) / np.mean(b[down])))

    return RiskStats(
        periods=n,
        periods_per_year=periods_per_year,
        arithmetic_mean=_safe(arithmetic),
        geometric_mean=_safe(geometric),
        volatility=_safe(vol),
        downside_deviation=downside_dev,
        max_drawdown=mdd,
        beta=beta,
        alpha=alpha,
        r_squared=r_squared,
        sharpe=sharpe,
        sortino=sortino,
        treynor=treynor,
        calmar=calmar,
        tracking_error=tracking_error,
        information_ratio=information_ratio,
        active_return=active_return,
        skew=skew,
        excess_kurtosis=kurt,
        var_historical=var_hist,
        var_analytical=var_analytical,
        cvar=cvar,
        up_capture=up_capture,
        down_capture=down_capture,
        positive_periods=positive,
        gain_loss_ratio=gain_loss,
    )
