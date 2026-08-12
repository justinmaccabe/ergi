# desk

A portfolio analytics library, with a dashboard on top.

The library is the product. The math layer takes arrays and returns value
objects — no configuration, no database, no network — so you can import it in a
notebook, and so that **a function cannot contain somebody's portfolio**. That
second property is what makes this safe to publish, and it is enforced by CI
rather than by good intentions.

```python
from desk.analytics.positions import build_ledger
from desk.analytics.risk import risk_stats     # phase 3
```

## The one rule

> **Config = policy. Database = state. Environment = secrets.**
>
> Would you want it in a diffable pull request? YAML.
> Would you change it from your phone at 9pm? Database.
> Does leaking it cost money? Environment.

Your positions are state. They live in your database. **No holding, balance,
contribution or personal parameter ever belongs in a source file, a commit
message, or git history** — and several automated checks exist to make sure they
do not get there by accident.

## Quick start

Install the package first — the `desk` command and the `desk.*` imports both
depend on it:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,app,prices]'
```

Then look around with synthetic data. No configuration and no secrets needed:

```bash
.venv/bin/desk demo
```

```bash
DESK_AUTH_MODE=demo .venv/bin/streamlit run streamlit_app.py
```

When you want it to be yours:

```bash
cp config/portfolio.example.yaml config/portfolio.yaml   # gitignored
cp .pii-denylist.example .pii-denylist                   # gitignored
.venv/bin/desk doctor        # says exactly what is still missing
```

To put it on GitHub and Streamlit Cloud, see **[DEPLOY.md](DEPLOY.md)** — written
so someone who has never seen this codebase can follow it.

## Layout

```
config/     your policy — one YAML file, validated with pydantic
data/       reference data: contribution limits, block taxonomy, broker layouts
src/desk/
  domain/       frozen value types crossing the purity boundary
  analytics/    the math. numpy/pandas/scipy only, enforced by import-linter
  config/       schema and loader
  store/        SQLAlchemy models and migrations
  data/         price and FX providers behind a protocol
  jurisdictions/ contribution-room rules as plugins
  intake/       statement parsing with a mandatory review gate
  services/     composition root — the only layer that knows all of the above
  app/          Streamlit. no math, no SQL
  cli/          desk doctor | demo | serve | hash-passcode
tools/      pii_scan.py — the data-hygiene gate
```

## Why it is built this way

This is a clean-room rebuild. An earlier system solved the same problem well
enough to be worth learning from and had four structural faults worth naming,
because most of the design here is a response to one of them.

**Personal data was in the code.** Its ledger was a list of Python constants —
share counts, cost bases to six decimals, cash to the cent — so `.gitignore`
excluding the database file achieved nothing. Dollar figures were also in about
ten commit subjects, which no later scrub commit can retract. Here: positions
only ever exist in the database, and `tools/pii_scan.py` runs as a pre-commit
hook, a commit-msg hook, and a required CI check over both the tree and the
entire commit log.

**Its auth gate failed open.** If the passcode secret was unset, the gate
returned early and served the portfolio to anyone with the URL. Here,
`DESK_AUTH_MODE` is a required enum with no default, every mode validates its
own prerequisites at construction, and there is no code path from a missing
secret to a served page. There is a named regression test.

**Sells did not reduce book value.** Cost basis averaged purchases only, so
disposing of a position left its full original cost on the books forever. Here
the ledger engine handles sells, splits and return of capital, and is tested
against a worked example in the shape of the CRA's own guidance.

**Accounts were columns.** Positions came out with one hardcoded column per
account the author happened to hold. Here accounts are configuration, positions
are long format, and two accounts at different custodians can share one
contribution limit via `room_group` — which the original could only handle by
merging them and losing per-account reporting.

Two smaller ones, both fixed: a stale exchange rate was invented as a constant
when the network failed, and currency was inferred from a ticker suffix with a
hardcoded override for the holding that broke the rule. Prices here carry their
source and age, and currency is declared.

## Checks

Everything below runs in CI on every pull request, and most of it locally.

| Check | What it protects |
|---|---|
| `python tools/pii_scan.py --all` | no portfolio data in files or commit messages |
| `lint-imports` | the math layer cannot reach config, the database, or the network |
| `pytest` | known-answer tests for the financial math |
| `ruff` / `mypy` | style and types, strictest on `analytics/` |
| committer-identity check | no personal email in `git log` |
| tracked-data-file check | no `.db`, `.csv`, `.xlsx`, `.pdf` ever committed |
| secrets-access check | only `desk/settings.py` reads the environment |
| fail-closed check | the app refuses to boot without auth configured |
| `gitleaks`, `pip-audit` | credentials and known vulnerabilities |

Install the hooks with `pre-commit install --hook-type pre-commit --hook-type commit-msg`.

Commit messages describe the change to the code, never the data that motivated
it. "Freeze cost basis at trade-date FX" is fine. "True up a named holding to a
quoted mark" is the mistake this project exists to avoid — and the scanner will
stop you, as it stopped an earlier draft of this very paragraph.

## Status

Usable. It holds a real book, values it at market, and records its own history.

**Working**

- Configuration schema with cross-field validation, and a loader that rejects
  typos rather than ignoring them
- Fail-closed settings and auth: argon2id passcodes, rate limiting, signed
  sessions with absolute and idle expiry
- The cost-basis engine — buys, sells, splits, return of capital, trade-date FX,
  per-account pooling
- Canadian registered-account contribution room, covering the three main plan
  types, with shared room groups and a generic fallback for anywhere else
- yfinance price and FX providers, with source and staleness on every quote
- Market value, unrealized gain, and gain attribution decomposed into the
  security's own return versus the currency move
- Performance history from recorded snapshots, open and close per trading day,
  with price coverage stored alongside each point
- **A scheduled snapshot job** — `desk fetch-prices`, run by
  `.github/workflows/snapshot.yml` twice each weekday. DST-aware: each local
  target is registered as two crons and only the one matching today's offset
  records
- Risk and return suite against a configurable benchmark, and a correlation
  matrix computed pairwise
- **Factor exposure** — developed-markets Fama-French five factors plus
  momentum, per holding and weighted to portfolio level, with prices converted
  into the factors' currency first
- **Holdings X-Ray** — funds resolved to the companies inside them, with overlap
  across funds made visible. Needs composition data; see
  [docs/lookthrough.md](docs/lookthrough.md)
- `desk doctor`, `demo`, `backfill`, `fetch-prices`, `build-lookthrough`,
  `push-config`, `serve`, `hash-passcode`
- The full hygiene and CI apparatus

**Not built yet**

- **No statement import.** `intake/` parses fund-composition files but not
  broker statements. Holdings are loaded with `desk backfill` from a YAML file.
- **No trade entry form.** The Manage tab is a placeholder.
- **No TWR or XIRR.** Snapshot history exists; the money-weighted and
  time-weighted return calculations on top of it do not.
- **No optimizers, frontier, or policy benchmark.** The Policy tab is a
  placeholder.
- **No IPS.** `reporting/` is empty.
- **No Alembic migration files.** Tables come from `create_all`, which is fine
  for a first deployment and not for schema changes afterwards.

Two of the six dashboard tabs still say so on their face rather than pretending
otherwise.
