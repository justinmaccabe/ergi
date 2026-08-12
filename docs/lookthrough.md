# Holdings X-Ray: getting the composition data

The X-Ray tab resolves every fund you hold to the individual companies inside it,
so a name carried by three of your funds shows up once with its contributions
summed. That is the concentration a fund-level allocation chart cannot show.

It needs one thing this repository cannot fetch for you: each fund's published
holdings file.

## Why this is manual

There is no free, stable, machine-readable feed of ETF constituents. Every
provider publishes a file on its own website in its own format and changes it
without notice. A scraper would be a permanent maintenance liability that fails
silently — and a silently stale look-through is worse than none, because it looks
current.

So the flow is: you download, the code normalises.

## 1. Download each file into `inbox/`

`inbox/` is gitignored. That is deliberate and not only about tidiness: the set of
funds whose holdings files you keep is itself a disclosure of what you own.

| Ticker | What to download | Notes |
|---|---|---|
| ZEB | Its own holdings file | Provider's Canadian asset-management site → ZEB → Holdings |
| TEC | Its own holdings file | |
| VFV | **VOO's** holdings file | VFV holds VOO, not the 500 companies directly. One extra hop. |
| SPMO | Its own holdings file | |
| VONG | Its own holdings file | |
| VTWO | Its own holdings file | |
| HXT | **Nothing** | Swap-based — see below |
| QQC | **Check first** | See below |
| FBTC | **Nothing** | Holds bitcoin — see below |

CSV is easiest. XLSX exports need saving as CSV first; the parser reads delimited
text, not spreadsheets.

## 2. The three that cannot be looked through

This is the part worth understanding, because it is not a gap in the tooling.

**HXT is a total-return swap.** It does not hold sixty Canadian companies. It
holds a contract with a bank that pays the index return. Listing the S&P/TSX 60
constituents as though they were held would be a fiction, and it would hide the
thing that is actually different about this holding: there is counterparty risk
and there are no shares. It is reported as synthetic index exposure, with its
region counted and its securities not invented.

**FBTC holds bitcoin.** No companies, no sectors, no country of domicile. It sits
outside every equity breakdown and is reported separately.

**QQC needs checking.** The provider has offered both swap-based and physically
held versions of Nasdaq-100 exposure. Look at the current fund facts. If the
units held are the physical version, change `resolution` to `securities` in the
manifest and supply the holdings file; if not, leave it as `synthetic`.

Between them these are roughly a third of the book, so the tab prints its
coverage figure prominently. Every percentage on it is a share of the resolved
sleeve, and the reader is told how big that sleeve is.

A provider can switch a fund between synthetic and physical replication. When one
does, the manifest is where you record it.

## 3. Write the manifest

```bash
cp data/lookthrough/manifest.example.yaml inbox/lookthrough.yaml
```

The example is filled in for the current nine holdings. Edit the `file:` names to
match what you actually downloaded.

## 4. Build

```bash
desk build-lookthrough
```

It prints a line per fund — row count, equities, distinct countries, and the
share of the fund its published weights cover — then writes
`data/lookthrough/composition.json.gz` and reads it back to prove it round-trips.

A file it cannot parse is an error naming the file, not a partial import. A
look-through quietly missing three of nine funds is not a look-through, and
nothing downstream could detect it.

If a provider's column headings are ones the parser has not seen, the error lists
the columns it found; add the heading to the relevant `*_KEYS` tuple in
`src/desk/intake/lookthrough.py`.

## 5. Commit the output

```bash
git add data/lookthrough/composition.json.gz
```

Safe to commit: it contains fund compositions and nothing about who holds them —
no position, no balance, no account. The PII scanner runs over it like everything
else.

Gzipped JSON rather than CSV because the data-hygiene gate refuses to let any
`.csv` be tracked. That rule is a good one and should not have an exception
carved into it for convenience.

## Refreshing

Providers update holdings daily; monthly is plenty for a look-through. Re-download,
re-run `desk build-lookthrough --as-of YYYY-MM-DD`, commit.

The tab reports the **oldest** composition date in play, not the newest. A blended
figure is only as current as its stalest input, and showing the freshest date
would overstate how up to date the picture is.
