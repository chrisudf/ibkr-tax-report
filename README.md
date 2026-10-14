# ibkr-tax-report

Australian capital gains tax (CGT) working papers from Interactive Brokers (IBKR)
CSV statements.

It is for **Australian tax residents who hold an IBKR account — individuals and
SMSF trustees — and the accountants and tax agents who prepare their returns.**
Drop in your IBKR CSV exports and get a reviewable working paper as **CSV + PDF**
(plus a ZIP of per-table CSVs and a JSON summary): FIFO parcel matching, every
leg translated at the RBA daily rate, written options as CGT event D2, the CGT
discount by entity type, and a reconciliation against IBKR's own figures.

Everything runs locally. No data leaves the machine, and no IBKR login or API
token is involved — only CSV files you export yourself.

> **Not tax advice.** This is a record-keeping and calculation tool. It does not
> decide whether you hold investments on capital or revenue account, your
> residency, or anything else that turns on your circumstances, and it can be
> wrong. Have a registered tax agent check the figures before you lodge.
> Provided "as is", without warranty — see [LICENSE](LICENSE).
> 本工具仅作记录与计算用途，不构成税务建议；数字须经注册税务代理复核后方可申报。

## The three CSV files

For one financial year, upload all three. Each supplies something the others do
not, and leaving one out changes the figures:

| | File | Period | What it supplies | If it is missing |
|---|---|---|---|---|
| **(a)** | **Activity Statement** for the year | 1 July – 30 June | Trades, open positions, dividends, interest, fees — the main data | Nothing to report on. |
| **(b)** | **Realized Summary** for the same year | 1 July – 30 June | The only export with a **Forex P/L Details** section: realised FX gains and losses (Div 775 — ordinary income, not CGT) | Realised FX gains/losses are counted as **0**. The report warns about it. |
| **(c)** | **Activity Statement that runs past 30 June** | Any start date; must **end after the expiry of every option you had written that was still open at 30 June** (e.g. 11 Nov – 14 Aug). Overlap with (a) is fine. | What happened to those options after year end: one assigned or exercised in July has its D2 gain disregarded (s 104-40(5)). Also its own **Financial Instrument Information** section — the conid mapping used to unify tickers renamed mid-life. | Every written option open at 30 June is taxed in full as a D2 gain (the strict view), including ones assigned in July — the net capital gain can be **overstated**. The report warns, naming each contract and its D2 amount. Not needed if no written options were open at 30 June. |

**(b) and (c) cannot stand in for each other.** The Realized Summary has no
Financial Instrument Information section and stops at 30 June; activity
statements have no Forex P/L Details section.

If you still hold parcels bought before the reported year, also upload the
earlier years' activity statements: they supply the FIFO history (the
acquisition date and cost of each parcel). Overlapping periods are
de-duplicated row by row. One IBKR account per run — files from different
accounts are refused.

Every date in every file must fall inside the bundled RBA rate table, or the
run stops — see [RBA exchange rates](#rba-exchange-rates-datarba_f11csv).

## Exporting from IBKR

Steps follow IBKR's Client Portal guide ([How to Run a
Statement](https://www.ibkrguides.com/clientportal/performanceandstatements/runstatement.htm),
[Types of Statements](https://www.ibkrguides.com/clientportal/performanceandstatements/typestatement.htm));
labels can differ slightly between portal versions and regions.

1. Log in to the IBKR portal and open **Performance & Reports > Statements**
   (or **Menu > Reporting > Statements**).
2. Click the blue **Run** arrow next to the statement: **Activity** for files
   (a) and (c), **Realized Summary** for file (b).
3. Set **Period** to **Custom Date Range** and enter the From and To dates from
   the table above, so the period matches the Australian 1 July – 30 June year.
4. Set the format to **CSV** — not PDF or HTML. The tool reads only the CSV
   export; a PDF upload is refused with a message saying so. (IBKR's reference
   page for the Realized Summary mentions only HTML and PDF, but CSV is offered
   when you run it.)
5. Run it and save the file. Upload it exactly as downloaded — opening and
   re-saving it in Excel can change its layout.

## Run

Requires Python 3.11 or newer. Runs on macOS, Linux and Windows.

**macOS / Linux**

```bash
./setup.sh                       # creates .venv, installs dependencies
.venv/bin/python app.py          # web UI on http://127.0.0.1:5173
```

**Windows (PowerShell)**

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\.venv\Scripts\python.exe app.py
```

Doing it by hand instead of the setup script:

| | macOS / Linux | Windows |
|---|---|---|
| create venv | `python3 -m venv .venv` | `py -3 -m venv .venv` |
| install | `.venv/bin/pip install -r requirements.txt` | `.venv\Scripts\pip install -r requirements.txt` |
| run web UI | `.venv/bin/python app.py` | `.venv\Scripts\python app.py` |
| run tests | `.venv/bin/python -m pytest tests/` | `.venv\Scripts\python -m pytest tests\` |

CLI (same engine) — shown for macOS/Linux; on Windows swap the interpreter
path for `.venv\Scripts\python`:

```bash
.venv/bin/python cli.py activity_fy2026.csv realized_fy2026.csv activity_to_aug2026.csv \
    --fy 2026 --entity smsf --carried-losses 1944 -o out/
```

Demo data (fictional account, covers every lifecycle path):

```bash
.venv/bin/python demo/make_demo.py
.venv/bin/python cli.py demo/demo_fy2025.csv demo/demo_fy2026.csv --fy 2026 -o out/
```

The demo files stop at 30 June 2026 while a written NVDA put is still open, so
the run shows the D2 warning described under file (c).

### RBA exchange rates (`data/rba_f11.csv`)

Every leg is translated at the RBA daily rate for its own date, from the
bundled copy of RBA table F11.1. Its date range is a **hard limit**:

- **Legs outside the reported year are translated too.** Positions still open
  at the end of the uploaded data are carried forward at their acquisition-date
  rate, and parcels sold in the year are costed at theirs — so a late trade in
  file (c), or a parcel bought years ago, needs a rate as much as a trade in
  the year does. A leg outside the table stops the whole run with
  `no RBA USD rate within 10 days on or before <date>`. Rates are never
  extrapolated; weekends and holidays fall back to the latest published rate
  within 10 days.
- So the table must reach the **end date of your latest file** — usually file
  (c) — and go back to the purchase date of the **oldest parcel** involved.
- The bundled table covers **3 January 2023 – 17 August 2026** (RBA publication
  of 17 August 2026). The RBA's current F11.1 file starts in 2023; parcels
  acquired earlier cannot be translated yet (see Limitations).

Refresh it from the RBA when your files run past its last date:

```bash
curl -o data/rba_f11.csv https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv
```

On Windows PowerShell use `curl.exe` (plain `curl` is an alias there):
`curl.exe -o data\rba_f11.csv https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv`.
The report shows the coverage it used ("FX source") on every run.

### Troubleshooting

- **"… is a PDF, not a CSV file"** (or an Excel workbook, ZIP or HTML page).
  Run the statement again with CSV as the format — see
  [Exporting from IBKR](#exporting-from-ibkr).
- **"D2 OUTCOME NOT IN UPLOADED DATA"** warning. Add file (c), ending after the
  latest expiry the warning names. For a contract that has not expired yet the
  strict figure stands; an assignment or exercise later means amending the
  return.
- **"no 'Forex P/L Details' section was found"** warning. Add file (b).
- **`no RBA USD rate within 10 days on or before <date>`.** Refresh the rate
  table — see [RBA exchange rates](#rba-exchange-rates-datarba_f11csv).
- **macOS: `library load disallowed by system policy` on import.** The folder
  was copied from another machine (AirDrop, a zip download) and macOS flagged
  it, which blocks the ad-hoc-signed C extensions inside `.venv`. Clear the
  flag and rebuild the environment:
  `xattr -dr com.apple.quarantine . && rm -rf .venv && ./setup.sh`
- **A `.venv` never survives being copied between machines** — it hardcodes
  interpreter paths. Delete it and re-run the setup script.
- **Windows: `setup.ps1 cannot be loaded because running scripts is
  disabled`.** Launch it as shown above with `-ExecutionPolicy Bypass`, which
  applies to that one invocation only.

## What it computes

- FIFO lot matching per symbol (matches IBKR's default, enabling per-row
  reconciliation against their Realized P/L column).
- Every leg translated at the **RBA daily rate for its own date** (s 960-50
  ITAA 1997): cost base at the acquisition-date rate, capital proceeds at the
  CGT-event-date rate.
- **Financial year defaults to the latest one that has ended** and that the
  uploaded data covers — not the year the statement happens to end in.
  Statements are pulled after 30 June, so keying off the end date alone lands
  on a barely-started year. The detected value is shown in the form and can be
  overridden (`--fy` on the CLI).
- **Only the reported FY is counted.** Uploading extra periods is expected and
  safe: earlier data supplies FIFO history, later data shows what happened to
  positions open at 30 June, and both CGT events and income rows (dividends,
  withholding tax, interest, fees, FX) are windowed to 1 July – 30 June before
  anything is totalled. Rows falling outside are reported as an excluded-row
  warning, never silently added.
- **Written options: CGT event D2 at grant** (s 104-40(2)), never discountable
  (s 115-25(3)). Options still open at 30 June are assessable that year
  ("strict" view); a deferred/closed-basis figure is shown for comparison.
  When the uploaded data stops before such an option's fate is known, the
  strict figure stands and a warning lists the contracts and amounts.
- **Assignment/exercise**: D2 disregarded (s 104-40(5)); premium folded into
  the share parcel (ss 134-1 / 116-65). Assignments cancelling a prior-year D2
  gain raise an **amendment flag** (s 170(10) ITAA 1936: unlimited period).
- Purchased options expiring worthless: capital loss at expiry (C2, s 104-25).
- 12-month CGT discount by entity (individual/trust 50%, complying SMSF 33⅓%,
  company nil), losses applied against non-discountable gains first.
- Corporate actions (splits, incl. option contract renames) parsed from the
  statement's Corporate Actions section; anything unrecognised becomes a
  warning, never a silent guess. Ticker changes are unified via IBKR conids.
- Multiple statements merged with row-level de-duplication (overlapping
  periods are safe); consecutive years give exact FIFO history. Closing trades
  with no visible opening lot fall back to IBKR's Realized P/L with loud
  warnings.
- Other amounts: dividends, withholding tax (FITO), interest (IBKR nets borrow
  fees into it — the borrow-fee detail is informational only), account fees,
  and realised FX P/L (Div 775 ordinary income; needs file (b)).
- Reconciliation: per-row vs IBKR Realized P/L, year-end parcels vs the Open
  Positions snapshot, with expected differences (premium folds) auto-annotated.

## Layout

```
app.py            Flask app (binds 127.0.0.1 only)
setup.sh          one-shot environment setup (macOS / Linux)
setup.ps1         one-shot environment setup (Windows)
cli.py            command-line runner
engine/parser.py  IBKR multi-section CSV parser (header-name mapped)
engine/fx.py      RBA F11.1 daily rates
engine/cgt.py     FIFO + CGT engine (the tax logic lives here)
engine/outputs.py workpaper CSV + ZIP (formula-injection guarded)
engine/pdf.py     PDF report (reportlab)
data/rba_f11.csv  bundled RBA F11.1 rate table
static/           drag & drop frontend (no external dependencies)
demo/             synthetic demo statement generator
tests/            pytest suite
```

## Limitations / read before relying on it

- Supports Stocks and Equity/Index Options in the CGT computation. Other asset
  categories (futures, CFDs, bonds, warrants) are excluded with a warning.
- Assumes capital account (investor). Revenue-account traders need different
  treatment — the tool takes no position on characterisation.
- Translation needs RBA rates from 3 January 2023 onward (the RBA's current
  F11.1 file). Parcels acquired before then stop the run; older rates are
  published by the RBA as separate historical files, which the tool does not
  read yet.
- Return-of-capital and similar basis adjustments IBKR applies out-of-band
  surface as annotated cost differences in the reconciliation, not automatic
  adjustments.
- Not tax advice. Output must be reviewed by a registered tax agent.

## License

[MIT](LICENSE) — Copyright (c) 2026 chrisudf.
