"""Dashboard shell.

Contains no financial math and no SQL. Everything it renders comes from
`desk.services` or `desk.analytics`, which is what makes a second frontend a
leaf-node swap rather than a rewrite.

Branding is read from configuration — the monogram letters, the palette, the
name. Nothing identifying is hardcoded anywhere in this package.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from desk.analytics.positions import aggregate_by_ticker, build_ledger
from desk.config.loader import ConfigError, load
from desk.config.schema import PortfolioConfig
from desk.services import demo as demo_service
from desk.settings import AuthMode, SettingsError, get_settings

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

    try:
        cfg = load(settings.config_path, allow_example=is_demo)
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

    with tabs[0]:
        _overview(cfg, is_demo=is_demo)
    later = ["Accounts", "Analytics", "Risk", "Manage", "Policy"]
    for tab, name in zip(tabs[1:], later, strict=True):
        with tab:
            st.markdown(
                f'<p class="note">{name} arrives in a later phase. '
                "The shell, the gate and the ledger engine are in place.</p>",
                unsafe_allow_html=True,
            )

    with st.sidebar:
        st.caption(f"signed in as {subject}")
        if settings.auth_mode is AuthMode.PASSCODE:
            st.button("Sign out", on_click=sign_out)


def _overview(cfg: PortfolioConfig, *, is_demo: bool) -> None:
    if is_demo:
        book = demo_service.generate(today=dt.date.today())
        result = build_ledger(book.entries)
    else:
        st.info(
            "No positions yet. Import a statement with `desk import`, or add trades "
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

    st.markdown(
        '<p class="note">Market value needs a price feed, which arrives with the '
        "provider layer. Every figure below comes from the ledger alone, so it is "
        "exact rather than estimated.</p>",
        unsafe_allow_html=True,
    )

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


main()
