"""Dashboard shell.

Contains no financial math and no SQL. Everything it renders comes from
`desk.services` or `desk.analytics`, which is what makes a second frontend a
leaf-node swap rather than a rewrite.

Branding is read from configuration — the monogram letters, the palette, the
name. Nothing identifying is hardcoded anywhere in this package.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd
import streamlit as st

from desk.analytics.positions import aggregate_by_ticker, build_ledger
from desk.config.loader import ConfigError, load
from desk.config.schema import PortfolioConfig
from desk.domain.types import LedgerResult, Position
from desk.services import demo as demo_service
from desk.settings import AuthMode, Settings, SettingsError, get_settings

CashRows = tuple[tuple[str, str, float], ...]

# Faint white, for chart gridlines. Named because an inline rgba() literal reads
# to the data-hygiene scanner as a currency amount.
GRID_COLOUR = "rgba(255, 255, 255, 0.06)"


def _load_book(settings: Settings, *, is_demo: bool) -> tuple[LedgerResult | None, CashRows]:
    """Source the book: synthetic in demo, the store otherwise.

    A leaf-node swap — both paths hand the same `LedgerResult` and cash rows to
    the renderer, which is why the dashboard needs no branch of its own.
    """
    if is_demo:
        book = demo_service.generate(today=dt.date.today())
        return build_ledger(book.entries), book.cash
    if settings.database_url is None:
        return None, ()
    from desk.services import portfolio as portfolio_service

    loaded = portfolio_service.load(settings.database_url.get_secret_value())
    return build_ledger(loaded.entries), loaded.cash

st.set_page_config(page_title="Portfolio", layout="wide", initial_sidebar_state="collapsed")


def _theme_css(cfg: PortfolioConfig) -> str:
    b = cfg.branding
    return f"""
    <style>
      .block-container {{ padding-top: 2.2rem; max-width: 1400px; }}
      /* Tabular numerals: figures in a column must line up to be comparable. */
      [data-testid="stMetricValue"], .dataframe td, .mono {{
          font-variant-numeric: tabular-nums;
          font-feature-settings: "tnum";
      }}
      .desk-header {{
          display: flex; align-items: center; gap: 0.9rem;
          border-bottom: 1px solid {b.primary}40;
          padding-bottom: 0.7rem; margin-bottom: 1.4rem;
      }}
      .desk-mark {{
          font-family: {b.serif}; font-size: 1.05rem; letter-spacing: 0.06em;
          color: {b.accent}; border: 1px solid {b.accent}; border-radius: 2px;
          padding: 0.32rem 0.6rem; line-height: 1;
      }}
      .desk-name {{
          font-family: {b.serif}; font-size: 1.32rem; letter-spacing: 0.01em;
      }}
      .desk-sub {{ opacity: 0.6; font-size: 0.85rem; margin-left: auto; }}
      .pos {{ color: {b.positive}; }}
      .neg {{ color: {b.negative}; }}
      .note {{ opacity: 0.62; font-size: 0.83rem; }}
    </style>
    """


def _header(cfg: PortfolioConfig, *, banner: str = "") -> None:
    mark = cfg.identity.initials or "··"
    st.markdown(_theme_css(cfg), unsafe_allow_html=True)
    st.markdown(
        f'<div class="desk-header">'
        f'<span class="desk-mark">{mark}</span>'
        f'<span class="desk-name">{cfg.identity.display_name}</span>'
        f'<span class="desk-sub">{cfg.identity.subtitle}</span>'
        f"</div>",
        unsafe_allow_html=True,
    )
    if banner:
        st.warning(banner, icon="⚠️")


def _fatal(title: str, detail: str) -> None:
    st.error(f"**{title}**\n\n```\n{detail}\n```")
    st.stop()


def main() -> None:
    # Settings first. An invalid configuration must stop the app before a
    # single widget renders, not after.
    try:
        settings = get_settings()
    except SettingsError as exc:
        _fatal("The application will not start", str(exc))
        return

    is_demo = settings.auth_mode is AuthMode.DEMO

    # Authenticate before anything else renders. An unauthenticated visitor
    # should see a login form and nothing else — not the app's structure, not
    # a configuration path, not an error describing what is installed.
    from desk.app.auth import require_auth, sign_out

    subject = require_auth(settings)

    def _db_config() -> Mapping[str, Any] | None:
        if settings.database_url is None:
            return None
        from desk.services import portfolio as portfolio_service

        return portfolio_service.load_config_payload(settings.database_url.get_secret_value())

    try:
        cfg = load(settings.config_path, allow_example=is_demo, db_fallback=_db_config)
    except ConfigError as exc:
        _fatal("Configuration problem", str(exc))
        return

    banner = (
        "Demonstration data. Generated from a fixed seed — these are not real holdings."
        if is_demo
        else ""
    )
    _header(cfg, banner=banner)

    tabs = st.tabs(["Overview", "Accounts", "Analytics", "Risk", "Manage", "Policy"])

    result, cash = _load_book(settings, is_demo=is_demo)
    db_url = settings.database_url.get_secret_value() if settings.database_url else None
    with tabs[0]:
        _overview(cfg, result, cash, is_demo=is_demo)
        _performance(cfg, db_url)
    with tabs[2]:
        _analytics(cfg, result)
    with tabs[3]:
        _risk(cfg, result)
    for index, name in ((1, "Accounts"), (4, "Manage"), (5, "Policy")):
        with tabs[index]:
            st.markdown(
                f'<p class="note">{name} arrives in a later phase. '
                "The shell, the gate and the ledger engine are in place.</p>",
                unsafe_allow_html=True,
            )

    with st.sidebar:
        st.caption(f"signed in as {subject}")
        if settings.auth_mode is AuthMode.PASSCODE:
            st.button("Sign out", on_click=sign_out)


def _overview(
    cfg: PortfolioConfig,
    result: LedgerResult | None,
    cash: CashRows,
    *,
    is_demo: bool,
) -> None:
    if result is None or not result.positions:
        st.info(
            "No positions yet. Load a statement with `desk backfill`, or add trades "
            "on the Manage tab.",
            icon="📄",
        )
        return

    rolled = aggregate_by_ticker(result.positions)
    total_book = sum(p.book_value_base for p in rolled)
    realized = sum(r.gain_base for r in result.realized)
    ccy = cfg.locale.base_currency

    a, b, c, d = st.columns(4)
    a.metric("Book value", f"{total_book:,.0f} {ccy}")
    b.metric("Realized gains", f"{realized:,.0f} {ccy}")
    c.metric("Positions", f"{len(rolled)}")
    d.metric("Accounts", f"{len({p.account_id for p in result.positions})}")

    if cash:
        base_cash = sum(amt for _, cur, amt in cash if cur == ccy)
        other = [(cur, amt) for _, cur, amt in cash if cur != ccy]
        note = f"Uninvested cash: {base_cash:,.2f} {ccy}"
        if other:
            note += " · " + " · ".join(f"{amt:,.2f} {cur}" for cur, amt in other)
        st.caption(note)

    _allocation(cfg, result, rolled)

    frame = pd.DataFrame(
        [
            {
                "Ticker": p.ticker,
                "Units": round(p.quantity, 4),
                "Average cost": round(p.acb_base, 4),
                f"Book value ({ccy})": round(p.book_value_base, 2),
                # ProgressColumn formats the raw value, so weights are carried
                # as percentage points rather than fractions.
                "Weight": 100.0 * p.book_value_base / total_book if total_book else 0.0,
            }
            for p in rolled
        ]
    )
    st.dataframe(
        frame,
        width="stretch",
        hide_index=True,
        column_config={
            "Weight": st.column_config.ProgressColumn(
                format="%.1f%%", min_value=0.0, max_value=100.0
            )
        },
    )

    st.subheader("By account")
    per_account = pd.DataFrame(
        [
            {
                "Account": next(
                    (a.label or a.id for a in cfg.accounts if a.id == p.account_id), p.account_id
                ),
                "Ticker": p.ticker,
                "Units": round(p.quantity, 4),
                f"Book value ({ccy})": round(p.book_value_base, 2),
            }
            for p in result.positions
        ]
    )
    st.dataframe(per_account, width="stretch", hide_index=True)
    st.markdown(
        '<p class="note">Positions are keyed by account, not spread across fixed '
        "columns, so adding an account is configuration rather than a code change.</p>",
        unsafe_allow_html=True,
    )


def _allocation(
    cfg: PortfolioConfig, result: LedgerResult, rolled: Sequence[Position]
) -> None:
    """Allocation by market value, with the unrealized gain the marks imply.

    Falls back to a book-value note when no price arrives, rather than drawing a
    pie of cost basis and labelling it market value.
    """
    import plotly.graph_objects as go

    from desk.analytics.valuation import (
        portfolio_market_value,
        priced_coverage,
        value_positions,
    )

    ccy = cfg.locale.base_currency
    symbols, currencies = _instrument_maps(cfg, result)
    if not symbols:
        st.markdown(
            '<p class="note">No quotable holdings, so market value is unavailable. '
            "Figures below come from the ledger alone.</p>",
            unsafe_allow_html=True,
        )
        return

    try:
        prices, fx = _cached_marks(
            tuple(symbols.values()), tuple({*currencies.values(), ccy}), ccy
        )
    except Exception:
        prices, fx = {}, {}
    by_ticker = {t: prices.get(sym) for t, sym in symbols.items()}
    valued = value_positions(rolled, by_ticker, fx)
    priced = [v for v in valued if v.market_value_base is not None]
    if not priced:
        st.markdown(
            '<p class="note">Prices are unavailable right now, so the figures below '
            "come from the ledger alone.</p>",
            unsafe_allow_html=True,
        )
        return

    market_value = portfolio_market_value(valued)
    book = sum(v.position.book_value_base for v in priced)
    gain = market_value - book
    coverage = priced_coverage(valued)

    a, b_col, c = st.columns(3)
    a.metric("Market value", f"{market_value:,.0f} {ccy}")
    b_col.metric(
        "Unrealized gain",
        f"{gain:,.0f} {ccy}",
        delta=f"{(gain / book):.2%}" if book else None,
    )
    c.metric("Priced", f"{coverage:.0%} of book")

    b = cfg.branding
    ordered = sorted(priced, key=lambda v: v.market_value_base or 0.0, reverse=True)
    palette = list(b.categorical) or [b.primary, b.accent]
    fig = go.Figure(
        go.Pie(
            labels=[v.position.ticker for v in ordered],
            values=[v.market_value_base for v in ordered],
            hole=0.62,
            sort=False,
            marker={
                "colors": [palette[i % len(palette)] for i in range(len(ordered))],
                "line": {"color": "#221C20", "width": 1},
            },
            textinfo="label+percent",
            textfont={"family": b.serif, "size": 12},
            hovertemplate="%{label}: %{value:,.0f} " + ccy + " (%{percent})<extra></extra>",
        )
    )
    fig.update_layout(
        height=340,
        margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": b.serif, "color": "#DED8CE"},
        showlegend=False,
        annotations=[
            {
                "text": f"{market_value:,.0f}<br><span style='font-size:0.7em'>{ccy}</span>",
                "x": 0.5, "y": 0.5, "showarrow": False,
                "font": {"family": b.serif, "size": 17, "color": "#DED8CE"},
            }
        ],
    )
    chart, gains = st.columns([1, 1])
    with chart:
        st.markdown("##### Allocation by market value")
        st.plotly_chart(fig, use_container_width=True)
    with gains:
        st.markdown("##### Gain / loss by holding")
        ranked = sorted(priced, key=lambda v: v.gain_base or 0.0)
        bars = go.Figure(
            go.Bar(
                x=[v.gain_base for v in ranked],
                y=[v.position.ticker for v in ranked],
                orientation="h",
                marker={
                    "color": [
                        b.positive if (v.gain_base or 0.0) >= 0 else b.negative
                        for v in ranked
                    ]
                },
                hovertemplate="%{y}: %{x:,.0f} " + ccy + "<extra></extra>",
            )
        )
        bars.update_layout(
            height=340,
            margin={"l": 8, "r": 8, "t": 8, "b": 8},
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font={"family": b.serif, "color": "#DED8CE"},
            xaxis={"gridcolor": GRID_COLOUR, "tickformat": ",.0f", "title": ccy},
            yaxis={"showgrid": False},
        )
        st.plotly_chart(bars, use_container_width=True)
    if coverage < 0.999:
        st.markdown(
            f'<p class="note">{coverage:.0%} of book value carried a live price; '
            "unpriced holdings are left out of the chart rather than shown at cost.</p>",
            unsafe_allow_html=True,
        )

    _attribution_report(cfg, valued, fx)


def _attribution_report(
    cfg: PortfolioConfig, valued: Sequence[Any], fx: Mapping[str, float]
) -> None:
    """Where the gain came from: per holding, and price versus currency."""
    from desk.analytics.valuation import attribution

    ccy = cfg.locale.base_currency
    report = attribution(valued, fx)
    if not report.rows or report.total_book <= 0:
        return

    with st.expander("Attribution report", expanded=False):
        a, b_col, c = st.columns(3)
        a.metric(
            "Total unrealized",
            f"{report.total_gain:,.0f} {ccy}",
            delta=f"{report.total_return:.2%}" if report.total_return else None,
        )
        b_col.metric("From prices", f"{report.price_gain:,.0f} {ccy}")
        c.metric("From currency", f"{report.fx_gain:,.0f} {ccy}")

        frame = pd.DataFrame(
            [
                {
                    "Ticker": r.ticker,
                    "Cur": r.currency,
                    "Units": round(r.quantity, 4),
                    f"ACB ({ccy})": round(r.acb_base, 4),
                    "Price": None if r.price_native is None else round(r.price_native, 4),
                    f"Book ({ccy})": round(r.book_value_base, 2),
                    f"Market ({ccy})": (
                        None
                        if r.market_value_base is None
                        else round(r.market_value_base, 2)
                    ),
                    f"Gain ({ccy})": None if r.gain_base is None else round(r.gain_base, 2),
                    "Return": r.return_pct,
                    "Weight": r.weight,
                    "Contribution": r.contribution,
                }
                for r in report.rows
            ]
        )
        st.dataframe(
            frame,
            width="stretch",
            hide_index=True,
            column_config={
                "Return": st.column_config.NumberColumn(format="percent"),
                "Weight": st.column_config.NumberColumn(format="percent"),
                "Contribution": st.column_config.NumberColumn(format="percent"),
            },
        )

        best = report.winners[0] if report.winners else None
        worst = report.losers[-1] if report.losers else None
        lines: list[str] = []
        if best is not None and best.gain_base is not None:
            lines.append(
                f"<strong>{best.ticker}</strong> contributed most, "
                f"{best.gain_base:,.0f} {ccy} "
                f"({(best.contribution or 0.0):+.2%} of the portfolio's return)"
            )
        if worst is not None and worst.gain_base is not None:
            lines.append(
                f"<strong>{worst.ticker}</strong> detracted most, "
                f"{worst.gain_base:,.0f} {ccy} "
                f"({(worst.contribution or 0.0):+.2%})"
            )
        if abs(report.fx_gain) > 0.005 * max(abs(report.total_gain), 1.0):
            share = report.fx_gain / report.total_gain if report.total_gain else 0.0
            lines.append(
                f"the exchange rate accounts for {report.fx_gain:,.0f} {ccy} "
                f"of the gain ({share:.0%}), separate from what the securities did"
            )
        if report.unpriced:
            lines.append(
                "excluded for want of a price: " + ", ".join(report.unpriced)
            )
        if lines:
            st.markdown(
                '<p class="note">' + ". ".join(lines).capitalize() + ".</p>",
                unsafe_allow_html=True,
            )
        st.markdown(
            '<p class="note">Contribution is each holding\'s gain over the '
            "portfolio's total cost, so the column sums to the portfolio return. "
            "Gain is market value less adjusted cost base, with cost frozen at the "
            "exchange rate on the trade date and market value at today's.</p>",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------- market data
# Network calls are cached, not the provider object: a cached plain value is
# safe to share across sessions, a live client is not.


@st.cache_data(ttl=900, show_spinner=False)
def _cached_marks(
    symbols: tuple[str, ...], currencies: tuple[str, ...], base: str
) -> tuple[dict[str, float], dict[str, float]]:
    from desk.services.market import fetch_marks

    return fetch_marks(symbols, currencies, base)


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_history(
    symbols: tuple[tuple[str, str], ...],
    currencies: tuple[tuple[str, str], ...],
    base: str,
    period: str,
) -> pd.DataFrame:
    from desk.services.market import base_history

    return base_history(dict(symbols), dict(currencies), base, period)


def _instrument_maps(
    cfg: PortfolioConfig, result: LedgerResult
) -> tuple[dict[str, str], dict[str, str]]:
    """Ticker -> quote symbol, and ticker -> currency, for held positions only."""
    held = {p.ticker for p in result.positions}
    symbols, currencies = {}, {}
    for inst in cfg.instruments:
        if inst.ticker in held and inst.symbol:
            symbols[inst.ticker] = inst.symbol
            currencies[inst.ticker] = inst.currency
    return symbols, currencies


def _performance(cfg: PortfolioConfig, db_url: str | None) -> None:
    """Fetch prices, record a snapshot, and chart the recorded history."""
    import plotly.graph_objects as go

    from desk.services.market import read_snapshots

    st.divider()
    st.subheader("Performance history")
    if db_url is None:
        st.markdown(
            '<p class="note">Snapshots need a database. Set DESK_DATABASE_URL.</p>',
            unsafe_allow_html=True,
        )
        return

    b = cfg.branding
    left, right = st.columns([1, 3])
    if left.button("↻ Fetch prices", use_container_width=True):
        _record_now(cfg, db_url)
    right.markdown(
        '<p class="note">Fetches live quotes, values the book, and records one '
        "open/close point. Market value is only ever built from prices that "
        "actually arrived — the coverage figure says how much.</p>",
        unsafe_allow_html=True,
    )

    snaps = read_snapshots(db_url)
    if snaps.empty:
        st.markdown(
            '<p class="note">No snapshots yet. Press <em>Fetch prices</em> to record '
            "the first one.</p>",
            unsafe_allow_html=True,
        )
        return

    closes = snaps[snaps["slot"] == "close"]
    series = closes if not closes.empty else snaps
    labels = [d.strftime("%b %d") for d in pd.to_datetime(series["date"])]
    ccy = cfg.locale.base_currency
    # One recorded point draws an invisible line, so markers carry the series
    # until there is a history to join up.
    sparse = len(series) < 2
    mode = "markers" if sparse else "lines+markers"
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=labels, y=series["market_value"], mode=mode, name="Market value",
            line={"color": b.primary, "width": 2},
            marker={"color": b.primary, "size": 10 if sparse else 7},
            hovertemplate="%{x}: %{y:,.0f} " + ccy + "<extra>Market value</extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=labels, y=series["book_value"],
            mode="markers" if sparse else "lines+markers", name="Book value",
            line={"color": b.accent, "width": 1.6, "dash": "dot", "shape": "hv"},
            marker={
                "color": b.accent,
                "size": 10 if sparse else 6,
                "symbol": "diamond",
            },
            hovertemplate="%{x}: %{y:,.0f} " + ccy + "<extra>Book value</extra>",
        )
    )
    fig.update_layout(
        height=320, margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"family": b.serif, "color": "#DED8CE"},
        xaxis={"type": "category", "showgrid": False},
        yaxis={"gridcolor": GRID_COLOUR, "tickformat": ",.0f", "title": ccy},
        legend={"orientation": "h", "y": 1.12, "x": 0},
    )
    st.plotly_chart(fig, use_container_width=True)
    if sparse:
        st.markdown(
            '<p class="note">One recorded point so far, shown as markers — the gap '
            "between them is the unrealized gain. Press <em>Fetch prices</em> again "
            "on later days and the lines join up into a history.</p>",
            unsafe_allow_html=True,
        )

    _open_close_table(snaps, ccy)


def _record_now(cfg: PortfolioConfig, db_url: str) -> None:
    """Value the book at current marks and store the result as a snapshot."""
    from desk.analytics.valuation import (
        portfolio_market_value,
        priced_coverage,
        value_positions,
    )
    from desk.services import portfolio as portfolio_service
    from desk.services.market import record_snapshot

    loaded = portfolio_service.load(db_url)
    result = build_ledger(loaded.entries)
    if not result.positions:
        st.warning("No positions to value yet.")
        return
    symbols, currencies = _instrument_maps(cfg, result)
    base = cfg.locale.base_currency
    with st.spinner("Fetching quotes…"):
        _cached_marks.clear()
        prices, fx = _cached_marks(
            tuple(symbols.values()), tuple({*currencies.values(), base}), base
        )
    by_ticker = {t: prices.get(sym) for t, sym in symbols.items()}
    valued = value_positions(result.positions, by_ticker, fx)
    market_value = portfolio_market_value(valued)
    coverage = priced_coverage(valued)
    now = dt.datetime.now()
    record_snapshot(
        db_url,
        market_value=market_value,
        book_value=sum(p.book_value_base for p in result.positions),
        cash_value=sum(amt for _, cur, amt in loaded.cash if cur == base),
        coverage=coverage,
        on_date=now.date(),
        slot="open" if now.hour < 12 else "close",
    )
    if coverage < 0.999:
        st.warning(
            f"Recorded, but only {coverage:.0%} of book value carried a live price.",
            icon="⚠️",
        )
    else:
        st.success(f"Recorded {market_value:,.0f} {base} at full price coverage.")


def _open_close_table(snaps: pd.DataFrame, ccy: str) -> None:
    """Open, intraday, close, overnight and the 24-hour move, per day."""
    wide = snaps.pivot_table(index="date", columns="slot", values="market_value")
    for column in ("open", "close"):
        if column not in wide.columns:
            wide[column] = pd.NA
    wide = wide.sort_index()
    prior_close = wide["close"].shift(1)
    table = pd.DataFrame(
        {
            "Date": [d.strftime("%b %d, %Y") for d in pd.to_datetime(wide.index)],
            f"Open ({ccy})": wide["open"].round(2),
            "Intraday": (wide["close"] - wide["open"]).round(2),
            f"Close ({ccy})": wide["close"].round(2),
            "Overnight": (wide["open"] - prior_close).round(2),
            "24h return": (wide["close"] / prior_close - 1.0),
        }
    ).iloc[::-1]
    st.markdown("##### Daily open and close")
    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        column_config={
            "24h return": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
    st.markdown(
        '<p class="note">Intraday is close minus open; overnight is open minus the '
        "prior close; the 24-hour move is the two combined. Blank cells are days "
        "with only one recorded slot.</p>",
        unsafe_allow_html=True,
    )


def _analytics(cfg: PortfolioConfig, result: LedgerResult | None) -> None:
    """Correlation of monthly returns across held positions."""
    import plotly.graph_objects as go

    from desk.analytics.risk import correlation_matrix

    st.subheader("Correlation matrix")
    if result is None or not result.positions:
        st.markdown('<p class="note">No positions yet.</p>', unsafe_allow_html=True)
        return
    symbols, currencies = _instrument_maps(cfg, result)
    if len(symbols) < 2:
        st.markdown(
            '<p class="note">Two quotable holdings are needed.</p>', unsafe_allow_html=True
        )
        return
    with st.spinner("Loading price history…"):
        history = _cached_history(
            tuple(symbols.items()), tuple(currencies.items()), cfg.locale.base_currency, "5y"
        )
    corr = correlation_matrix(history)
    if corr.empty:
        st.markdown(
            '<p class="note">Not enough overlapping history yet.</p>', unsafe_allow_html=True
        )
        return

    labels = list(corr.columns)
    k = len(labels)
    z, text = [], []
    for i in range(1, k):  # lower triangle only; the mirror adds no information
        z.append([corr.values[i][j] if j < i else None for j in range(k - 1)])
        text.append([f"{corr.values[i][j]:.2f}" if j < i else "" for j in range(k - 1)])
    b = cfg.branding
    fig = go.Figure(
        go.Heatmap(
            z=z, x=labels[:-1], y=labels[1:], zmin=0, zmax=1,
            colorscale=[[0.0, "#2C242A"], [0.5, "#8C6A75"], [1.0, b.primary]],
            text=text, texttemplate="%{text}",
            textfont={"size": 12, "color": "#DED8CE"},
            hoverongaps=False, showscale=False,
            hovertemplate="%{y} x %{x}: %{z:.2f}<extra></extra>",
        )
    )
    fig.update_layout(
        height=460, margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"family": b.serif, "color": "#DED8CE"},
        yaxis={"autorange": "reversed"},
    )
    st.plotly_chart(fig, use_container_width=True)
    st.markdown(
        '<p class="note">Monthly returns in base currency, computed pairwise so two '
        "holdings with different inception dates are compared over the history they "
        "share. Darker is more diversifying.</p>",
        unsafe_allow_html=True,
    )


def _risk(cfg: PortfolioConfig, result: LedgerResult | None) -> None:
    """Risk and return metrics for the book, against the configured benchmark."""
    from desk.analytics.risk import risk_stats

    st.subheader("Risk and return metrics")
    if result is None or not result.positions:
        st.markdown('<p class="note">No positions yet.</p>', unsafe_allow_html=True)
        return
    symbols, currencies = _instrument_maps(cfg, result)
    if not symbols:
        st.markdown('<p class="note">No quotable holdings.</p>', unsafe_allow_html=True)
        return

    base = cfg.locale.base_currency
    bench_symbol = cfg.benchmarks.risk
    fetch = dict(symbols)
    fetch_ccy = dict(currencies)
    if bench_symbol:
        fetch["__benchmark__"] = bench_symbol
        fetch_ccy["__benchmark__"] = base
    with st.spinner("Loading price history…"):
        history = _cached_history(tuple(fetch.items()), tuple(fetch_ccy.items()), base, "5y")
    if history.empty:
        st.markdown('<p class="note">Price history unavailable.</p>', unsafe_allow_html=True)
        return

    benchmark = history.pop("__benchmark__") if "__benchmark__" in history.columns else None
    units = {p.ticker: p.quantity for p in result.positions}
    columns = [c for c in history.columns if c in units]
    if not columns:
        st.markdown('<p class="note">No matching history.</p>', unsafe_allow_html=True)
        return
    # Current units held constant over history: a like-for-like backtest of the
    # book as it stands today, not a replay of when each lot was bought.
    values = (history[columns] * pd.Series({c: units[c] for c in columns})).sum(axis=1)
    stats = risk_stats(values.dropna(), benchmark)

    if stats.periods < 6:
        st.markdown(
            f'<p class="note">Only {stats.periods} monthly periods available — too few '
            "for reliable statistics.</p>",
            unsafe_allow_html=True,
        )
        return

    pct = {
        "Arithmetic mean", "Geometric mean", "Volatility", "Downside deviation",
        "Maximum drawdown", "Alpha", "Active return", "Tracking error",
        "Historical VaR (5%)", "Analytical VaR (5%)", "Conditional VaR (5%)",
        "Up capture", "Down capture", "Positive periods",
    }
    rows = [
        ("Arithmetic mean", stats.arithmetic_mean),
        ("Geometric mean", stats.geometric_mean),
        ("Volatility", stats.volatility),
        ("Downside deviation", stats.downside_deviation),
        ("Maximum drawdown", stats.max_drawdown),
        ("Sharpe ratio", stats.sharpe),
        ("Sortino ratio", stats.sortino),
        ("Calmar ratio", stats.calmar),
        ("Beta", stats.beta),
        ("Alpha", stats.alpha),
        ("R squared", stats.r_squared),
        ("Treynor ratio", stats.treynor),
        ("Tracking error", stats.tracking_error),
        ("Information ratio", stats.information_ratio),
        ("Active return", stats.active_return),
        ("Skewness", stats.skew),
        ("Excess kurtosis", stats.excess_kurtosis),
        ("Historical VaR (5%)", stats.var_historical),
        ("Analytical VaR (5%)", stats.var_analytical),
        ("Conditional VaR (5%)", stats.cvar),
        ("Up capture", stats.up_capture),
        ("Down capture", stats.down_capture),
        ("Positive periods", stats.positive_periods),
        ("Gain/loss ratio", stats.gain_loss_ratio),
    ]
    formatted = [
        {
            "Metric": label,
            "Value": (
                "—"
                if value is None
                else (f"{value:.2%}" if label in pct else f"{value:.2f}")
            ),
        }
        for label, value in rows
    ]
    half = (len(formatted) + 1) // 2
    left, right = st.columns(2)
    left.dataframe(pd.DataFrame(formatted[:half]), width="stretch", hide_index=True)
    right.dataframe(pd.DataFrame(formatted[half:]), width="stretch", hide_index=True)
    bench_note = f" against {bench_symbol}" if bench_symbol else " (no benchmark configured)"
    st.markdown(
        f'<p class="note">Monthly returns over {stats.periods} periods{bench_note}, in '
        f"{base}, holding current units constant. Statistics that need more history than "
        "is available are left blank rather than estimated.</p>",
        unsafe_allow_html=True,
    )


main()
