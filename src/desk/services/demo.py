"""A synthetic portfolio, generated from a fixed seed.

This exists so that no real person's holdings are ever the fixture. Screenshots,
first-run experience, and any shareable deployment all run on invented data. It
is the structural alternative to masking real figures — masking is
allowlist-by-omission, and every new chart or export is a fresh chance to
forget one.

Tickers are real because tickers are not secret; the weights, sizes and dates
are fabricated. Nothing here corresponds to anybody's book, and the notional is
a deliberately round number so it cannot be mistaken for one.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass

from desk.analytics.positions import LedgerEntry
from desk.domain.types import Action

SEED = 20260101
NOTIONAL = 250_000.0

# A plausible globally diversified book. Weights are invented.
UNIVERSE: tuple[tuple[str, str, str, float], ...] = (
    # ticker, quote symbol, currency, target weight
    ("VEQT", "VEQT.TO", "CAD", 0.34),
    ("XIC", "XIC.TO", "CAD", 0.12),
    ("VTI", "VTI", "USD", 0.24),
    ("VEA", "VEA", "USD", 0.16),
    ("VWO", "VWO", "USD", 0.08),
    ("HOLDCO", "", "CAD", 0.06),  # private, manually marked
)

# id, label, type, room group. The taxable account has no room group, which is
# the whole reason that field is optional: a limit is a property of some
# account types, not of accounts in general.
DEMO_ACCOUNTS: tuple[tuple[str, str, str, str | None], ...] = (
    ("demo_tfsa_a", "Demo Registered A", "tfsa", "ca:tfsa"),
    ("demo_tfsa_b", "Demo Registered B", "tfsa", "ca:tfsa"),
    ("demo_fhsa", "Demo First-Home", "fhsa", "ca:fhsa"),
    ("demo_taxable", "Demo Taxable", "taxable", None),
)


@dataclass(frozen=True)
class DemoBook:
    entries: tuple[LedgerEntry, ...]
    contributions: tuple[tuple[dt.date, str, float], ...]
    cash: tuple[tuple[str, str, float], ...]
    marks: tuple[tuple[str, dt.date, float], ...]


def generate(*, today: dt.date, years: int = 5) -> DemoBook:
    """Build a deterministic five-year history.

    Deterministic matters: the demo has to look the same in a screenshot taken
    today and one taken next week, and tests need a stable fixture.
    """
    rng = random.Random(SEED)
    start = dt.date(today.year - years, 1, 15)

    entries: list[LedgerEntry] = []
    contributions: list[tuple[dt.date, str, float]] = []
    marks: list[tuple[str, dt.date, float]] = []

    # Opening prices, invented but in a believable range per listing currency.
    price: dict[str, float] = {
        ticker: (30.0 if currency == "CAD" else 60.0) * rng.uniform(0.8, 1.4)
        for ticker, _symbol, currency, _w in UNIVERSE
    }

    fx = 1.32

    # The private holding is a single early subscription, not a monthly
    # purchase — that is how an illiquid position usually arrives, and it gives
    # the valuation time series something to revalue.
    entries.append(
        LedgerEntry(
            date=start,
            ticker="HOLDCO",
            account_id="demo_taxable",
            action=Action.BUY,
            quantity=1000.0,
            price=15.0,
            fx_rate=1.0,
            currency="CAD",
        )
    )

    month = start
    while month <= today:
        # Quarterly contributions into rotating registered accounts, plus a
        # monthly purchase, so both TWR and XIRR have something to chew on.
        drift = rng.gauss(0.006, 0.035)
        fx = max(1.15, min(1.50, fx * (1 + rng.gauss(0.0, 0.012))))

        for ticker, _symbol, currency, weight in UNIVERSE:
            price[ticker] = max(1.0, price[ticker] * (1 + drift + rng.gauss(0, 0.02)))
            if ticker == "HOLDCO":
                continue  # subscribed once above; marked twice a year below
            account = DEMO_ACCOUNTS[rng.randrange(len(DEMO_ACCOUNTS))][0]
            amount = NOTIONAL * weight * 0.02
            unit = price[ticker]
            quantity = round(amount / unit, 4)
            if quantity <= 0:
                continue
            entries.append(
                LedgerEntry(
                    date=month,
                    ticker=ticker,
                    account_id=account,
                    action=Action.BUY,
                    quantity=quantity,
                    price=round(unit, 2),
                    fees=0.0,
                    fx_rate=round(fx, 4) if currency == "USD" else 1.0,
                    currency=currency,
                )
            )

        if month.month in (1, 4, 7, 10):
            contributions.append((month, "demo_tfsa_a", 1750.0))
            contributions.append((month, "demo_fhsa", 2000.0))

        # A rebalancing sale each year, so the realized-gain path is exercised
        # and the cost-basis engine has sells in the demo history.
        if month.month == 11:
            held = [e for e in entries if e.ticker == "VTI" and e.action is Action.BUY]
            if held:
                total = sum(e.quantity for e in held)
                entries.append(
                    LedgerEntry(
                        date=month,
                        ticker="VTI",
                        account_id=held[0].account_id,
                        action=Action.SELL,
                        quantity=round(total * 0.05, 4),
                        price=round(price["VTI"], 2),
                        fx_rate=round(fx, 4),
                        currency="USD",
                    )
                )

        # The private holding is marked twice a year — a time series, so a
        # historical snapshot uses the mark that was current then.
        if month.month in (6, 12):
            marks.append(("HOLDCO", month, round(price["HOLDCO"], 2)))

        month = _add_month(month)

    cash = (
        ("demo_tfsa_a", "CAD", 1250.0),
        ("demo_tfsa_b", "CAD", 480.0),
        ("demo_fhsa", "CAD", 2100.0),
        ("demo_taxable", "CAD", 950.0),
    )
    return DemoBook(
        entries=tuple(entries),
        contributions=tuple(contributions),
        cash=cash,
        marks=tuple(marks),
    )


def _add_month(d: dt.date) -> dt.date:
    return dt.date(d.year + 1, 1, d.day) if d.month == 12 else dt.date(d.year, d.month + 1, d.day)
