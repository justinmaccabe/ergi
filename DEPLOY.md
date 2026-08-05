# Deploying

Written for whoever is putting this on GitHub and Streamlit Cloud. You do not
need to know the codebase to follow it, and you do not need anyone's portfolio
data — the app runs on generated synthetic data until real holdings are added.

**Time:** about 20 minutes, most of it waiting for the first build.
**Cost:** nothing. Streamlit Community Cloud and Neon both have free tiers.

---

## 0. Before you start

Check the repository is clean. This should print two clean lines:

```bash
python tools/pii_scan.py --all
```

That scans every tracked file **and every commit message** for portfolio data,
account numbers, custodian names and personal identifiers. If it reports a
finding, stop and resolve it before pushing anywhere — the assumption behind
this whole project is that the repository will eventually be public, whether or
not you intend that.

---

## 1. Run it locally first

Confirm it works on your machine before involving any hosting.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,app,prices]'
```

```bash
.venv/bin/desk demo
```

That prints a synthetic portfolio. Nothing there belongs to anybody — it is
generated from a fixed random seed.

To see the dashboard:

```bash
DESK_AUTH_MODE=demo .venv/bin/streamlit run streamlit_app.py
```

Note `streamlit_app.py` at the repository root — that is the entry point
Streamlit Cloud uses, and running it locally exercises the same path.

---

## 2. Push to GitHub

Make the repository **private**. It contains no portfolio data by design, but
private is the right default and costs nothing.

```bash
gh repo create <name> --private --source=. --push
```

Two settings worth changing while you are there:

- **Settings → Actions → General → Workflow permissions**: read-only. The
  workflows here need nothing more.
- **Settings → Secrets and variables → Actions**: add `PII_DENYLIST` if the
  owner wants their name, handle and employer checked against commits. One term
  per line. The list is itself sensitive, which is why it lives in a secret
  rather than in the repository.

CI runs on the first push: lint, types, tests, the architecture contract, the
data-hygiene scan, a dependency audit, and a check that the app refuses to boot
without authentication configured. All of it should pass on a fresh clone.

---

## 3. Create the database

Streamlit Cloud wipes local disk on every restart, so the app needs Postgres.
SQLite is deliberately **refused** in production for exactly this reason —
otherwise a restart looks identical to having lost all your history.

1. Sign up at [neon.tech](https://neon.tech) (or Supabase) and create a project.
2. Copy the connection string. It looks like
   `postgresql://user:password@host/dbname?sslmode=require`.
3. If the provider lets you create a scoped role, use one with
   SELECT/INSERT/UPDATE rather than the database owner.

Tables are created automatically on first run. There is no seed data to load —
that is the point.

---

## 4. Generate the secrets

Run this on your machine, not on any server:

```bash
.venv/bin/desk hash-passcode
```

It prompts for a passcode (so it never enters your shell history), and prints
an argon2id hash. **The passcode itself is never stored anywhere** — not in the
repository, not in the secret store, not in this file. Whoever will use the app
should choose it and keep it in their password manager.

Then generate a session signing secret:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## 5. Deploy

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with
   GitHub.
2. **Create app** → pick the repository → branch `main` → main file
   **`streamlit_app.py`**.
3. Open **Advanced settings → Secrets** *before* the first deploy, and paste the
   contents of `.streamlit/secrets.toml.example` with real values filled in.
4. Deploy. The first build takes a few minutes.

If a secret is missing or contradictory, the app renders a short message saying
what is wrong and stops. It does not fall back to running without a login. That
behaviour is deliberate and there is a test named after it.

---

## 6. Confirm it is actually locked

Do not skip this. Open the app URL in a private browsing window:

- You should see a **sign-in form and nothing else** — no tabs, no figures, no
  configuration paths.
- A wrong passcode should say so and count down remaining attempts.
- Five wrong attempts should lock further tries for fifteen minutes.
- The correct passcode should let you in.

If you see anything other than a sign-in form before authenticating, stop and
report it.

---

## 7. Hand it over

The owner then adds their own configuration. This is the only step that touches
real data, and it happens on their machine:

```bash
cp config/portfolio.example.yaml config/portfolio.yaml   # gitignored
cp .pii-denylist.example .pii-denylist                   # gitignored
desk doctor
```

`desk doctor` lists exactly what is still missing — accounts, instruments,
benchmarks, contribution-room inputs. Holdings are entered through statement
import or the trade form; **they never go into a file in this repository.**

---

## Deploying somewhere else

Streamlit Cloud is the easy path, not the only one. The app is a normal
Streamlit application reading its secrets from environment variables, so it runs
anywhere. Two notes if you move it:

- The strongest option is a small VPS reachable only over a private network such
  as Tailscale, which removes the public listener entirely. There is then no
  login form to attack.
- Scheduled snapshots are best served by a systemd timer next to the app, which
  understands time zones properly. GitHub Actions cron is time-zone blind and is
  silently disabled after 60 days of repository inactivity.

## Troubleshooting

**`ModuleNotFoundError: No module named 'desk'`** — the package is not
installed. Run `pip install -e .`, or launch through `streamlit_app.py`, which
adds `src` to the path itself.

**"The application will not start"** — a required secret is missing. The message
names it. This is the app working correctly.

**"no configuration found"** — expected until someone creates
`config/portfolio.yaml`. Use `DESK_AUTH_MODE=demo` to look around without one.

**Build fails on Streamlit Cloud** — check the main file is `streamlit_app.py`
and that `requirements.txt` is unmodified. It is pinned exactly, so builds are
reproducible.

**A completely blank page** — this means an unexpected exception. The app is
configured with `showErrorDetails = "none"`, so tracebacks are never rendered in
the browser: a stack frame can carry a dataframe, and a dataframe carries
positions. That is the right trade for a financial app, but it does mean an
unhandled error looks like nothing at all.

The detail is in the server log, not the page. On Streamlit Cloud: **Manage app
→ Logs**, bottom right of the deployed app. Locally, it is on the terminal
running Streamlit.

Note the difference between a blank page and a short message such as "The
application will not start" or "no configuration found". Those are deliberate,
and mean the app is working correctly and telling you what it needs.
