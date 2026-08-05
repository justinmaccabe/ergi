"""Command line entry points.

desk doctor          validate everything before you rely on it
desk hash-passcode   produce the argon2 hash to put in your environment
desk demo            build the synthetic portfolio and summarise it
desk serve           run the dashboard
"""

from __future__ import annotations

import datetime as dt
import getpass
import sys

import typer
from rich.console import Console
from rich.table import Table

from desk.analytics.positions import aggregate_by_ticker, build_ledger
from desk.config.loader import ConfigError, load, load_example, resolve_path
from desk.jurisdictions.ca import get_jurisdiction
from desk.security import hash_passcode
from desk.services import demo as demo_service

app = typer.Typer(add_completion=False, help="Portfolio analytics.")
console = Console()

OK = "[green]ok[/green]"
WARN = "[yellow]warn[/yellow]"
BAD = "[red]fail[/red]"


@app.command()
def doctor(
    config: str = typer.Option(None, "--config", "-c", help="path to portfolio.yaml"),
) -> None:
    """Validate configuration, settings and reference data.

    Runs in CI against the shipped example on every commit, so the example
    cannot rot into a file that documents a schema the code no longer accepts.
    """
    table = Table(title="desk doctor", show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("result")
    table.add_column("detail", overflow="fold")
    problems = 0

    # -- configuration --
    path = resolve_path(config)
    try:
        cfg = load(config) if path else load_example()
        source = str(path) if path else "config/portfolio.example.yaml (no config yet)"
        table.add_row("configuration", OK, source)
    except ConfigError as exc:
        table.add_row("configuration", BAD, str(exc))
        console.print(table)
        raise typer.Exit(1) from exc

    # -- accounts and room groups --
    if not cfg.accounts:
        table.add_row("accounts", WARN, "none declared — run `desk init`")
        problems += 1
    else:
        groups = cfg.room_groups()
        shared = {g: a for g, a in groups.items() if len(a) > 1}
        detail = f"{len(cfg.accounts)} account(s)"
        if shared:
            detail += "; sharing one limit: " + ", ".join(
                f"{g} <- {len(a)} accounts" for g, a in shared.items()
            )
        table.add_row("accounts", OK, detail)

    # -- jurisdiction --
    jur = get_jurisdiction(cfg.jurisdiction.id)
    params = cfg.jurisdiction.params
    if cfg.jurisdiction.id == "ca":
        missing = []
        kinds = {a.type.value for a in cfg.accounts}
        if "tfsa" in kinds and params.birth_year is None:
            missing.append("birth_year")
        if "fhsa" in kinds and params.fhsa_open_year is None:
            missing.append("fhsa_open_year")
        if missing:
            table.add_row(
                "jurisdiction", WARN, f"room cannot be computed without: {', '.join(missing)}"
            )
            problems += 1
        else:
            table.add_row("jurisdiction", OK, f"{jur.id}, {len(jur.room_group_labels())} groups")
    else:
        table.add_row("jurisdiction", OK, f"{jur.id} (contribution room reported as unlimited)")

    # -- instruments --
    if not cfg.instruments:
        table.add_row("instruments", WARN, "none declared")
        problems += 1
    else:
        unquotable = [i.ticker for i in cfg.instruments if not i.symbol and i.kind != "private"]
        if unquotable:
            table.add_row("instruments", BAD, f"no quote symbol: {', '.join(unquotable)}")
            problems += 1
        else:
            table.add_row("instruments", OK, f"{len(cfg.instruments)} declared")

    # -- benchmarks: the field the reference conflated --
    if cfg.benchmarks.daily and cfg.benchmarks.risk:
        same = cfg.benchmarks.daily == cfg.benchmarks.risk
        table.add_row(
            "benchmarks",
            WARN if same else OK,
            "daily and risk benchmarks are the same symbol — intended?"
            if same
            else f"daily {cfg.benchmarks.daily}, risk {cfg.benchmarks.risk}",
        )
        problems += int(same)
    else:
        table.add_row("benchmarks", WARN, "not configured")
        problems += 1

    # -- policy --
    if cfg.policy.source.value == "sleeves":
        total = sum(s.weight for s in cfg.policy.sleeves) + cfg.policy.cash_target
        table.add_row(
            "policy", OK, f"{len(cfg.policy.sleeves)} sleeves, weights sum to {total:.4f}"
        )
    else:
        table.add_row("policy", OK, f"source is '{cfg.policy.source.value}'")

    # -- settings and auth --
    try:
        from desk.settings import get_settings

        settings = get_settings()
        table.add_row("settings", OK, f"env {settings.app_env.value}, auth {settings.auth_mode}")
    except Exception as exc:
        first = str(exc).splitlines()[0]
        table.add_row("settings", WARN, f"{first} (fine for CLI use; required to serve)")

    console.print(table)
    if problems:
        console.print(f"\n[yellow]{problems} item(s) need attention before this is trustworthy.")
    else:
        console.print("\n[green]All checks passed.")


@app.command("hash-passcode")
def hash_passcode_cmd() -> None:
    """Hash a passcode for DESK_PASSCODE_HASH.

    Prompted, never taken as an argument: a passcode passed on the command line
    lands in your shell history.
    """
    entered = getpass.getpass("Passcode (at least 12 characters): ")
    again = getpass.getpass("Again: ")
    if entered != again:
        console.print("[red]They do not match.")
        raise typer.Exit(1)
    try:
        digest = hash_passcode(entered)
    except ValueError as exc:
        console.print(f"[red]{exc}")
        raise typer.Exit(1) from exc
    console.print("\nAdd to your environment (or your host's secret store):\n")
    console.print(f"  DESK_AUTH_MODE=passcode\n  DESK_PASSCODE_HASH='{digest}'\n")
    console.print("[dim]The passcode itself is not stored anywhere.")


@app.command()
def demo(
    years: int = typer.Option(5, help="years of synthetic history"),
) -> None:
    """Generate the synthetic portfolio and summarise it.

    No real holdings are ever used as a fixture, which is what makes the demo
    safe to share and the screenshots safe to publish.
    """
    today = dt.date.today()
    book = demo_service.generate(today=today, years=years)
    result = build_ledger(book.entries)
    rolled = aggregate_by_ticker(result.positions)

    table = Table(
        title=f"Synthetic portfolio — {len(book.entries)} ledger entries", show_header=True
    )
    table.add_column("ticker")
    table.add_column("units", justify="right")
    table.add_column("avg cost", justify="right")
    table.add_column("book value", justify="right")
    for position in rolled:
        table.add_row(
            position.ticker,
            f"{position.quantity:,.2f}",
            f"{position.acb_base:,.2f}",
            f"{position.book_value_base:,.0f}",
        )
    console.print(table)

    total = sum(p.book_value_base for p in rolled)
    realized = sum(r.gain_base for r in result.realized)
    console.print(f"\nbook value      {total:>14,.0f}")
    console.print(f"realized gains  {realized:>14,.0f}  ({len(result.realized)} disposals)")
    console.print(f"accounts        {len({p.account_id for p in result.positions}):>14}")
    console.print(f"contributions   {len(book.contributions):>14}")
    console.print(f"private marks   {len(book.marks):>14}  (a time series, not one scalar)")
    console.print("\n[dim]Generated from a fixed seed. Nothing here belongs to anybody.")


@app.command()
def serve() -> None:
    """Run the dashboard."""
    try:
        from streamlit.web import cli as stcli
    except ImportError as exc:
        console.print("[red]Streamlit is not installed. Try: pip install -e '.[app]'")
        raise typer.Exit(1) from exc

    from pathlib import Path

    entry = Path(__file__).resolve().parent.parent / "app" / "main.py"
    sys.argv = ["streamlit", "run", str(entry)]
    stcli.main()


if __name__ == "__main__":
    app()
