# ibkr-tax-report

Australian CGT working papers from IBKR statements, for professional accountants.
Drag and drop one or more IBKR CSV exports (annual **Activity Statement**, plus the
**Realized Summary** export if available), get a reviewable working paper as
**CSV + PDF** (and a ZIP with per-table CSVs and a JSON summary).

Everything runs locally. No data leaves the machine.

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
.venv/bin/python cli.py statement_fy2025.csv statement_fy2026.csv \
    --fy 2026 --entity individual --carried-losses 1944 -o out/
```

Demo data (fictional account, covers every lifecycle path):

```bash
.venv/bin/python demo/make_demo.py
.venv/bin/python cli.py demo/demo_fy2025.csv demo/demo_fy2026.csv --fy 2026 -o out/
```

### Troubleshooting

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
- **`no RBA USD rate within 10 days on or before <date>`.** The bundled rate
  table stops before that date. Rates are never extrapolated — a run aborts
  rather than translate a leg at a stale rate. Refresh the table:
  `curl -o data/rba_f11.csv https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv`
  Note this bites on any leg in the uploaded statement, not just legs inside
  the reported FY: the whole file is translated to build FIFO history before
  the year is windowed. A statement running past 30 June therefore needs rates
  up to its own end date.

## What it computes

- FIFO lot matching per symbol (matches IBKR's default, enabling per-row
  reconciliation against their Realized P/L column).
- Every leg translated at the **RBA daily rate for its own date** (s 960-50
  ITAA 1997); rates bundled in `data/rba_f11.csv` (RBA table F11.1 — replace
  with a fresh download to extend coverage). Non-trading days fall back up to
  10 days; beyond that the run aborts rather than guess.
- **Only the reported FY is counted.** Uploading extra periods is expected and
  safe: earlier data supplies FIFO history, and both CGT events and income
  rows (dividends, withholding tax, interest, fees, FX) are windowed to
  1 July – 30 June before anything is totalled. Rows falling outside are
  reported as an excluded-row warning, never silently added.
- **Written options: CGT event D2 at grant** (s 104-40(2)), never discountable
  (s 115-25(3)). Options still open at 30 June are assessable that year
  ("strict" view); a deferred/closed-basis figure is shown for comparison.
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
  and realised FX P/L (Div 775 ordinary income; needs the Realized Summary
  export).
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
static/           drag & drop frontend (no external dependencies)
demo/             synthetic demo statement generator
tests/            pytest suite
```

## Limitations / read before relying on it

- Supports Stocks and Equity/Index Options in the CGT computation. Other asset
  categories (futures, CFDs, bonds, warrants) are excluded with a warning.
- Assumes capital account (investor). Revenue-account traders need different
  treatment — the tool takes no position on characterisation.
- Return-of-capital and similar basis adjustments IBKR applies out-of-band
  surface as annotated cost differences in the reconciliation, not automatic
  adjustments.
- Not tax advice. Output must be reviewed by a registered tax agent.
  本工具仅作记录与计算用途，不构成税务建议。
