# ibkr-tax-report

Australian capital gains tax (CGT) working papers from Interactive Brokers (IBKR)
CSV statements.

It is for **Australian tax residents who hold an IBKR account — individuals and
SMSF trustees — and the accountants and tax agents who prepare their returns.**
Drop in your IBKR CSV exports and get a reviewable working paper as **CSV + PDF**
(plus a ZIP of per-table CSVs and a JSON summary): FIFO parcel matching, every
leg translated at the RBA daily rate, written options as CGT event D2, the CGT
discount by entity type, and a reconciliation against IBKR's own figures.

Run it on your own machine (`python app.py`) and nothing leaves it; or use the
hosted copy at **https://cgt.nomad403.cc**, which works the same way but
receives your files over HTTPS — see [Privacy](#privacy) for exactly what it
does with them. Either way no IBKR login or API token is involved — only CSV
files you export yourself. **Try with sample data** on the start page runs a
fictional account end to end without uploading anything.

> **Not tax advice.** This is a record-keeping and calculation tool. It does not
> decide whether you hold investments on capital or revenue account, your
> residency, or anything else that turns on your circumstances, and it can be
> wrong. Have a registered tax agent check the figures before you lodge.
> Provided "as is", without warranty — see [LICENSE](LICENSE).

## Privacy

**Local** (`python app.py`): the server binds 127.0.0.1 only and sends nothing
anywhere.

**Hosted** (https://cgt.nomad403.cc, deployed from [`deploy/`](deploy/README.md)):

- Files travel over HTTPS to the server and are parsed **in memory**. The
  uploaded CSVs are never written to disk: the app keeps every upload in RAM
  rather than letting the web framework spool large ones to a temp file, and
  the container has no swap.
- The generated report — workpaper CSV, PDF, ZIP — is kept in a RAM-backed
  directory for **at most 15 minutes** so you can download it, then deleted
  automatically by a background sweeper; expired links stop working at once.
  **Delete my results now** removes it immediately.
- **No file contents are logged.** There is no access log, no analytics, no
  tracking and no cookies. Your IP address is held in memory for up to an hour
  after your last report, for rate limiting (10 reports a minute, 60 an hour),
  and nowhere else.
- Uploads are limited to **10 MB per report**. A year's Activity Statement CSV
  is usually 100–300 KB even for an active options trader, so the limit only
  catches the wrong kind of file.

Both modes run the same code: the hosted mode is `IBKR_TAX_HOSTED=1` plus the
settings listed at the top of [`app.py`](app.py).

## The three CSV files

For one financial year, upload all three (file (b) only for an AUD
base-currency account). Each supplies something the others do not, and
leaving one out changes the figures:

| | File | Period | What it supplies | If it is missing |
|---|---|---|---|---|
| **(a)** | **Activity Statement** for the year | 1 July – 30 June | Trades, open positions, dividends, interest, fees, deposits, conversions and the Cash Report — the main data | Nothing to report on. |
| **(b)** | **Realized Summary** for the same year — **AUD base-currency accounts only** | 1 July – 30 June | The only export with a **Forex P/L Details** section. In an AUD-base account that is IBKR's own figure for realised FX gains and losses on the foreign cash (Div 775 — ordinary income, not CGT). In a USD-base (or any non-AUD-base) account the section measures the *AUD* cash in USD, which is not a Div 775 amount, so it is ignored and Div 775 is worked out from the cash flows instead — see [Foreign currency](#foreign-currency-div-775). | AUD base: realised FX gains/losses are counted as **0**, and the report warns. Other base currencies: no effect. |
| **(c)** | **Activity Statement that runs past 30 June** | Must **start on or before 1 July** and **end after the expiry of every option you had written that was still open at 30 June** (e.g. 11 Nov – 14 Aug). Overlap with (a) is fine; a gap after 30 June is not — an assignment dated before the file starts is simply missing. | What happened to those options after year end: one assigned or exercised in July has its D2 gain disregarded (s 104-40(5)). Also its own **Financial Instrument Information** section — the conid mapping used to unify tickers renamed mid-life. | Every written option open at 30 June is taxed in full as a D2 gain (the strict view), including ones assigned in July — the net capital gain can be **overstated**. The report warns, naming each contract and its D2 amount. Not needed if no written options were open at 30 June. |

**(b) and (c) cannot stand in for each other.** The Realized Summary has no
Financial Instrument Information section and stops at 30 June; activity
statements have no Forex P/L Details section.

### Foreign currency (Div 775)

For an Australian resident every non-AUD cash balance in the account is
foreign currency, whatever IBKR's base currency is. In a USD-base account the
real exposure is the USD cash itself, and IBKR's Forex P/L Details does not
cover it at all. So for any non-AUD base account the tool works out Div 775
from the statements' own cash flows: trades, currency conversions, dividends,
withholding tax, interest, fees, deposits and withdrawals.

- Spending, converting or withdrawing USD is forex realisation event 2
  (s 775-45). The gain or loss is the AUD value then less the AUD forex cost
  base of the units used; units are matched first-in first-out
  (s 775-145(1)). Paying back a USD debit balance is FRE 4 (s 775-55). The
  method reproduces the ATO's worked FIFO example to the cent.
- AUD values are RBA daily rates, except for a conversion to or from AUD,
  where the AUD side of the trade is the exact value.
- Each statement's Cash Report checks the result: starting cash plus the
  flows read must equal IBKR's ending cash. A gap of more than 10 units is
  flagged as a warning. Small gaps are normal, because GST charged on fees is
  not itemised anywhere in the statement.
- An opening balance whose history is not in the uploaded files is costed at
  the RBA rate on the first day, with a warning. Upload earlier years'
  statements to avoid that.
- **Not applied:** the $250,000 balance election (s 775-230), under which these
  FRE 2 and FRE 4 amounts on a qualifying forex account are disregarded while
  its balance test is met. Whether an IBKR account qualifies, and whether the
  election was made, are questions for your tax agent. The retranslation
  election (s 775-270) and the weighted-average alternative to FIFO are not
  applied either.

Every realisation is listed in the report's Div 775 schedule (CSV, PDF and
ZIP), with the AUD value, the FIFO cost and the dates the units were acquired.

If you still hold parcels bought before the reported year, or had written
options still open at the start of it, also upload the earlier years'
activity statements: they supply the FIFO history (the acquisition date and
cost of each parcel, the write date and premium of each option). Overlapping
periods are de-duplicated row by row. One IBKR account per run — files from
different accounts are refused.

Without that history the tool still knows what was open when the data starts,
from the statement's Mark-to-Market Performance Summary, and closes those
positions first. Their opening cash comes from IBKR's cost basis on each
closing trade, but their dates do not. So shares and bought options are
translated at the sale-date rate with no CGT discount. Written options are
treated as written in an earlier year (only the buy-back counts, an expiry
counts nothing). An assignment is flagged for amending the year the option was
written, without an amount. The report warns, listing the positions.

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

### Local and hosted mode

`python app.py` starts in **local mode** (the default): the page says nothing
leaves the machine, and there is no rate limit. Setting `IBKR_TAX_HOSTED=1`
switches the same app to **hosted mode**, which is what
https://cgt.nomad403.cc runs: the page carries the hosted privacy notes
instead, and reports are rate-limited per address (10 a minute, 60 an hour).
Use it to preview the hosted page before deploying; it still binds
127.0.0.1:5173 only.

| | macOS / Linux | Windows (PowerShell) |
|---|---|---|
| local mode | `.venv/bin/python app.py` | `.\.venv\Scripts\python.exe app.py` |
| hosted mode | `IBKR_TAX_HOSTED=1 .venv/bin/python app.py` | `$env:IBKR_TAX_HOSTED = "1"; .\.venv\Scripts\python.exe app.py` |
| back to local | (the variable applied to that command only) | `Remove-Item Env:IBKR_TAX_HOSTED`, then run again |

In PowerShell the variable stays set for the rest of that terminal session,
so remove it (or open a new terminal) to go back to local mode. The other
settings (`IBKR_TAX_RESULT_TTL_MINUTES`, `IBKR_TAX_RATE_LIMIT`, …) are listed
at the top of [`app.py`](app.py) and work the same way. The real server runs
the hosted mode under gunicorn in a container — see
[`deploy/README.md`](deploy/README.md).

CLI (same engine) — shown for macOS/Linux; on Windows swap the interpreter
path for `.venv\Scripts\python`:

```bash
.venv/bin/python cli.py activity_fy2026.csv realized_fy2026.csv activity_to_aug2026.csv \
    --fy 2026 --entity smsf --carried-losses 1944 -o out/
```

Demo data (fictional account U9999999, covers every lifecycle path) — the
web UI's **Try with sample data** button runs these two files:

```bash
.venv/bin/python demo/make_demo.py
.venv/bin/python cli.py demo/demo_fy2025.csv demo/demo_fy2026.csv --fy 2026 -o out/
```

The demo files stop at 30 June 2026 while a written NVDA put is still open, so
the run shows the D2 warning described under file (c).

### RBA exchange rates (`data/rba_f11.csv`)

Every leg is translated at the RBA daily rate for its own date, from the
bundled copy of RBA table F11.1. Its date range is a **hard limit**:

- **What needs a rate is the date of every leg the run translates**, not the
  statement period: trades and income inside the reported year, the purchase
  date of every parcel sold in the year or still held at the end of the
  uploaded data, and the write date of options taxed as D2. Legs outside the
  year count too — a position opened late in file (c) and still held when it
  ends, or a parcel bought years ago, needs a rate as much as a trade in the
  year does. A leg outside the table stops the whole run, with
  `no RBA USD rate for <date>: the rate table ends <last date>` after its end
  or `no RBA USD rate within 10 days on or before <date>` before its start.
  Rates are never extrapolated: after the table's last day only the weekend
  straight after it takes that day's rate; inside the table, weekends and
  holidays fall back to the latest published rate within 10 days.
- A file whose period merely ends after the table's last date is fine as long
  as none of those legs do. Keeping the table at least as recent as your latest
  file's end date avoids having to check.
- The bundled table covers **3 January 2023 – 14 October 2026** (RBA publication
  of 14 October 2026). The RBA's current F11.1 file starts in 2023; parcels
  acquired earlier cannot be translated yet (see Limitations).

Refresh it from the RBA when a leg falls after its last date — or simply
whenever your files end after it:

```bash
.venv/bin/python rba_refresh.py
```

It downloads the current F11.1 file and replaces `data/rba_f11.csv` only if
the new file parses, covers a longer range (starting no later, ending later),
and matches the existing table row for row wherever the two overlap;
otherwise it keeps the old table and says why. A plain download also works,
without those checks:
`curl -o data/rba_f11.csv https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv`
(on Windows PowerShell use `curl.exe`, and `data\rba_f11.csv`). The hosted copy
runs the validated refresh daily. The report shows the coverage it used
("FX source") on every run, and the start page's footer shows it too.

### Troubleshooting

- **"… is a PDF, not a CSV file"** (or an Excel workbook, ZIP or HTML page).
  Run the statement again with CSV as the format — see
  [Exporting from IBKR](#exporting-from-ibkr).
- **"D2 OUTCOME NOT IN UPLOADED DATA"** warning. Add file (c), starting on or
  before 1 July and ending after the latest expiry the warning names. For a
  contract that has not expired yet the strict figure stands; an assignment or
  exercise later means amending the return.
- **"no 'Forex P/L Details' section was found"** warning (AUD base-currency
  accounts only). Add file (b).
- **"GAP IN THE UPLOADED DATA"** or **"POSITIONS DO NOT ADD UP"** warning. Trades
  are missing between the files (for example a year left out), so a later sale
  can be matched against a parcel that was already sold in the missing period.
  The second check compares each statement's opening positions (IBKR's
  Mark-to-Market summary) with what the earlier data adds up to. Upload the
  missing statement.
- **"DIV 775 MAY BE INCOMPLETE"** warning. A statement's cash flows do not add up
  to its Cash Report by more than 10 units: some cash movement is not in the
  sections the tool reads (for example cash from a corporate action, or a
  transfer between accounts). Find it in the statement before relying on the
  FX figure.
- **"DIV 775 OPENING BALANCE"** warning. The earliest file starts with foreign
  cash whose history is not uploaded, so it is costed at the RBA rate on that
  day. Upload the earlier years' statements.
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
- **Dates are IBKR's, in Sydney time** (AEST/AEDT), as the CSV gives them:
  most US trades carry the next calendar day. IBKR's statement day ends at
  10:00 Sydney, so a statement "to 30 June" can hold rows dated 1 July — counted
  in the year of their date and listed in a warning, because next year's
  statement may not repeat them. The ATO sets no rule on the time zone; the
  Acts Interpretation Act 1901 s 37 points to the legal time of the
  taxpayer's State, which differs from Sydney time in Queensland, WA, SA and
  the NT. Whichever convention is used, keep it for the whole return and from
  year to year. Income counts when IBKR credits it: June's
  interest, credited in July, falls in the next year (a fund keeping accruals
  accounts may bring it back — the accountant's choice).
- **Withholding**: foreign tax withheld is the foreign income tax offset
  candidate, less any US withholding above the 15% treaty rate: that excess is
  shown separately (reclaim it from the IRS; it is not part of the offset), as
  is any tax withheld in AUD, which is Australian tax, not foreign.
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
  ("strict" view); a deferred/closed-basis figure, taxing each option in the
  year it is closed (premium included; for an option written before the
  uploaded data, at the close-date rate, and left out where IBKR's basis does
  not give it), is shown for comparison. Buying a
  written option back is a capital loss equal to the buy-back cost: the
  prevailing view, on which the ATO has published no ruling (a private ruling
  gives certainty).
  When the uploaded data stops before such an option's fate is known, the
  strict figure stands and a warning lists the contracts and amounts.
- **Assignment/exercise**: D2 disregarded (s 104-40(5)); premium folded into
  the share parcel (ss 134-1 / 116-65). Assignments cancelling a prior-year D2
  gain raise an **amendment flag** (no time limit: s 170(10AA) item 50 ITAA 1936).
  The flag says the amendment is needed only if that return counted the gain —
  not if it was prepared after the assignment with a statement showing it. The
  folded premium is translated at the rate on the day the option was written.
- Purchased options expiring worthless: capital loss at expiry (C2, s 104-25).
- 12-month CGT discount by entity (individual/trust 50%, complying SMSF 33⅓%,
  company nil), losses applied against non-discountable gains first. Leaving
  out the day of acquisition and the day of the event (the ATO's approach), the
  12 months are counted as months under the Acts Interpretation Act 1901 s 2G:
  bought 28 February 2023, the first discountable day is 1 March 2024. The
  workpaper's `discount_from` column
  gives the first day a parcel's sale is discountable, since a day count can
  mislead (365 days is not always 12 months).
- **Each CGT event counts on its own** in total gains (18H) and losses. An
  option written and bought back in the same year is a D2 gain (the premium)
  plus a separate capital loss (the buy-back), as when the two fall in
  different years. 18A is the same as with one net figure per contract, which
  the workpaper shows for comparison. Current-year losses are applied before
  prior-year ones (s 102-5), and the split is shown (for entities that lodge
  a CGT schedule, as item 2 labels B and C).
- Corporate actions (splits, incl. option contract renames) parsed from the
  statement's Corporate Actions section; anything unrecognised becomes a
  warning, never a silent guess. Ticker changes are unified via IBKR conids.
- Multiple statements merged with row-level de-duplication (overlapping
  periods are safe); consecutive years give exact FIFO history. Closing trades
  with no visible opening lot fall back to IBKR's Realized P/L with loud
  warnings.
- Other amounts: dividends, foreign tax withheld (FITO candidate, less any US
  excess over the treaty rate), interest (IBKR nets borrow fees into it — the
  borrow-fee detail is informational only), account fees, and realised FX
  gains/losses (Div 775 ordinary income, not CGT). For a non-AUD base account
  Div 775 is worked out FIFO from the cash flows; for an AUD base account it
  comes from file (b) — see [Foreign currency](#foreign-currency-div-775).
- Reconciliation: per-row vs IBKR Realized P/L, year-end parcels vs the Open
  Positions snapshot, with expected differences (premium folds) auto-annotated.

## Layout

```
app.py            Flask app: local (binds 127.0.0.1) or hosted mode
rba_refresh.py    validated RBA table refresh (CLI, and the hosted daily job)
setup.sh          one-shot environment setup (macOS / Linux)
setup.ps1         one-shot environment setup (Windows)
cli.py            command-line runner
engine/parser.py  IBKR multi-section CSV parser (header-name mapped)
engine/fx.py      RBA F11.1 daily rates
engine/cgt.py     FIFO + CGT engine (the tax logic lives here)
engine/forex.py   Div 775 on foreign-currency cash (FIFO, checked against the Cash Report)
engine/outputs.py workpaper CSV + ZIP (formula-injection guarded)
engine/pdf.py     PDF report (reportlab)
data/rba_f11.csv  bundled RBA F11.1 rate table
templates/        start page (local and hosted variants)
static/           drag & drop frontend (no external dependencies)
demo/             synthetic demo statements and their generator
tests/            pytest suite
Dockerfile        hosted image (gunicorn, non-root)
deploy/           compose file, gunicorn config, deployment notes
LESSONS.md        problems met so far, their causes, and what we do now
```

## Hosting

[`deploy/README.md`](deploy/README.md) describes the hosted deployment: the
hardened container (read-only root, tmpfs for results, no capabilities, no
volumes), the Caddy site block, updating, and how to put it behind a
password later.

## Limitations / read before relying on it

- Supports Stocks and Equity/Index Options in the CGT computation. Other asset
  categories (futures, CFDs, bonds, warrants) are excluded with a warning.
- Assumes capital account (investor). Revenue-account traders need different
  treatment — the tool takes no position on characterisation. For an individual
  writing options often, whether that is a business is a question of fact with
  no statutory answer; for a complying SMSF the CGT rules are the primary code
  (s 295-85), and ATO ID 2009/110 and 2009/111 apply that to options a fund
  writes and buys.
- **Individuals and trusts, FY2027-28 onwards: not supported.** From 1 July 2027
  the 50% discount no longer applies to their CGT events (s 115-100(aa), (ab),
  as amended by Act No 49 of 2026); assets held at 30 June 2027 are deemed sold
  and reacquired at market value, or by choice an amount apportioned under
  s 112-185 (individuals s 112-155; trusts s 112-165, if a beneficiary would
  get the discount), and later growth is indexed. The
  tool refuses those years rather than give a wrong figure. For FY2026-27 it
  reminds you to keep the 30 June 2027 market values. SMSFs and companies are
  unaffected.
- **SMSF**: assumes the fund is wholly in accumulation phase — exempt current
  pension income (segregated assets, s 118-320, or the actuarial proportion at
  SMSF annual return item 11 label Y) is not applied.
- **Company / trust**: a company's prior-year losses are applied without
  checking the continuity of ownership or business continuity test; a trust's
  figures are the trust's net capital gain, before anything about its
  beneficiaries. The report labels each figure with the entity's own return
  item (18H/18A/18V are the individual return's) and notes when a CGT schedule
  is required (gains or losses over $10,000 for non-individuals).
- Checked so far only against real statements from **USD base-currency**
  accounts. AUD base-currency accounts are untested; check the figures closely
  and [report](https://github.com/chrisudf/ibkr-tax-report/issues) anything
  odd. The issue tracker is public: describe the problem, or reproduce it with
  the demo files, rather than attaching your statements.
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
