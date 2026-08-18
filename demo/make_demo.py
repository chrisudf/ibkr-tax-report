"""Generate the synthetic two-year demo statements (fictional account).

Covers every lifecycle path the engine handles: plain gains/losses, >12-month
discount, written options open at 30 June (D2), a prior-year written option
bought back (cross-year loss), an assignment with premium fold, a long option
expiring worthless (C2), a 10:1 stock split, dividends/WHT/interest/fees, and
an Open Positions snapshot for reconciliation.

Run:  python demo/make_demo.py
"""
from __future__ import annotations

import csv
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))

TRADES_HDR = ["Trades", "Header", "DataDiscriminator", "Asset Category", "Currency",
              "Symbol", "Date/Time", "Quantity", "T. Price", "C. Price", "Proceeds",
              "Comm/Fee", "Basis", "Realized P/L", "MTM P/L", "Code"]


def trade(cat, sym, dt, qty, proceeds, comm, basis="", rpl="", code=""):
    return ["Trades", "Data", "Order", cat, "USD", sym, dt, qty, "", "", proceeds,
            comm, basis, rpl, "", code]


def statement(period, when, trades, open_positions=(), corporate_actions=(),
              dividends=(), wht=(), interest=(), fees=()):
    rows = [
        ["Statement", "Header", "Field Name", "Field Value"],
        ["Statement", "Data", "BrokerName", "Interactive Brokers Australia Pty Ltd. (DEMO)"],
        ["Statement", "Data", "Title", "Activity Statement"],
        ["Statement", "Data", "Period", period],
        ["Statement", "Data", "WhenGenerated", when],
        ["Account Information", "Header", "Field Name", "Field Value"],
        ["Account Information", "Data", "Account", "U9999999"],
        ["Account Information", "Data", "Name", "Demo Client"],
        ["Account Information", "Data", "Base Currency", "USD"],
        TRADES_HDR, *trades,
    ]
    if corporate_actions:
        rows.append(["Corporate Actions", "Header", "Asset Category", "Currency",
                     "Report Date", "Date/Time", "Description", "Quantity", "Proceeds",
                     "Value", "Realized P/L", "Code"])
        rows.extend(corporate_actions)
    if open_positions:
        rows.append(["Open Positions", "Header", "DataDiscriminator", "Asset Category",
                     "Currency", "Symbol", "Quantity", "Mult", "Cost Price", "Cost Basis",
                     "Close Price", "Value", "Unrealized P/L", "Code"])
        rows.extend(open_positions)
    for name, items in (("Dividends", dividends), ("Withholding Tax", wht),
                        ("Interest", interest)):
        if items:
            rows.append([name, "Header", "Currency", "Date", "Description", "Amount"])
            rows.extend(items)
    if fees:
        rows.append(["Fees", "Header", "Subtitle", "Currency", "Date", "Description",
                     "Amount"])
        rows.extend(fees)
    rows += [
        ["Codes", "Header", "Code", "Meaning"],
        ["Codes", "Data", "A", "Assignment"],
        ["Codes", "Data", "C", "Closing Trade"],
        ["Codes", "Data", "Ep", "Resulted from an Expired Position"],
        ["Codes", "Data", "Ex", "Exercise"],
        ["Codes", "Data", "O", "Opening Trade"],
    ]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def opos(cat, sym, qty, basis):
    return ["Open Positions", "Data", "Summary", cat, "USD", sym, qty, "", "", basis,
            "", "", "", ""]


OPT = "Equity and Index Options"

# ---------------- FY2024-25 file ----------------
fy25 = statement(
    "July 1, 2024 - June 30, 2025", "2025-07-05, 09:00:00 AEST",
    trades=[
        # AAPL parcel held into FY26 (>12 months by sale) — discount demo
        trade("Stocks", "AAPL", "2024-08-15, 10:00:00", 100, -15000, -1, code="O"),
        # TSLA loss realised inside FY25
        trade("Stocks", "TSLA", "2025-03-03, 10:00:00", 50, -10000, -1, code="O"),
        trade("Stocks", "TSLA", "2025-05-20, 10:00:00", -50, 9000, -1, "-10001", "-1002",
              code="C"),
        # MSFT put written near year end, open at 30 Jun 2025 (D2 in FY25;
        # bought back in FY26 -> cross-year loss demo)
        trade(OPT, "MSFT 19DEC25 400 P", "2025-06-10, 11:00:00", -1, 500, -0.66, code="O"),
    ],
    open_positions=[
        opos("Stocks", "AAPL", 100, 15001),
        opos(OPT, "MSFT 19DEC25 400 P", -1, -499.34),
    ],
)

# ---------------- FY2025-26 file ----------------
fy26 = statement(
    "July 1, 2025 - June 30, 2026", "2026-07-05, 09:00:00 AEST",
    trades=[
        # AAPL sold after 523 days -> discount-eligible gain
        trade("Stocks", "AAPL", "2026-01-20, 10:00:00", -100, 19000, -1, "-15001", "3998",
              code="C"),
        # MSFT put (written FY25) bought back -> capital loss this FY
        trade(OPT, "MSFT 19DEC25 400 P", "2025-09-15, 10:00:00", 1, -200, -0.66,
              "499.34", "298.68", code="C"),
        # NVDA puts written, still open at 30 Jun 2026 -> strict D2
        trade(OPT, "NVDA 17JUL26 100 P", "2026-05-10, 10:00:00", -2, 600, -1.32, code="O"),
        # PFE put written then assigned -> premium folds into stock cost base
        trade(OPT, "PFE 21NOV25 25 P", "2025-10-06, 10:00:00", -1, 80, -0.66, code="O"),
        trade(OPT, "PFE 21NOV25 25 P", "2025-11-22, 16:20:00", 1, 0, 0, code="A;C"),
        trade("Stocks", "PFE", "2025-11-22, 16:20:00", 100, -2500, 0, code="A;O"),
        # AMD call bought, expires worthless -> C2 loss
        trade(OPT, "AMD 16JAN26 200 C", "2025-08-04, 10:00:00", 1, -400, -0.66, code="O"),
        trade(OPT, "AMD 16JAN26 200 C", "2026-01-17, 16:20:00", -1, 0, 0, "-400.66",
              "-400.66", code="C;Ep"),
        # GOOG bought, 10:1 split, half sold post-split
        trade("Stocks", "GOOG", "2025-07-10, 10:00:00", 20, -3600, -1, code="O"),
        trade("Stocks", "GOOG", "2026-05-01, 10:00:00", -100, 2100, -1, "-1800.5", "298.5",
              code="C"),
    ],
    corporate_actions=[
        ["Corporate Actions", "Data", "Stocks", "USD", "2026-03-16",
         "2026-03-15, 20:25:00",
         "GOOG(US02079K3059) Split 10 for 1 (GOOG, ALPHABET INC-CL C, US02079K3059)",
         180, 0, 0, 0, ""],
    ],
    open_positions=[
        opos("Stocks", "GOOG", 100, 1800.5),
        opos("Stocks", "PFE", 100, 2500),          # IBKR does not fold the premium
        opos(OPT, "NVDA 17JUL26 100 P", -2, -598.68),
    ],
    dividends=[
        ["Dividends", "Data", "USD", "2025-11-14",
         "AAPL(US0378331005) Cash Dividend USD 0.25 per Share (Ordinary Dividend)", 25.00],
    ],
    wht=[
        ["Withholding Tax", "Data", "USD", "2025-11-14",
         "AAPL(US0378331005) Cash Dividend USD 0.25 per Share - US Tax", -3.75],
    ],
    interest=[
        ["Interest", "Data", "USD", "2026-01-05", "USD Credit Interest for Dec-2025", 12.34],
    ],
    fees=[
        ["Fees", "Data", "Other Fees", "USD", "2025-12-01", "Market data subscription", -10.00],
    ],
)

if __name__ == "__main__":
    for name, text in (("demo_fy2025.csv", fy25), ("demo_fy2026.csv", fy26)):
        path = os.path.join(HERE, name)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        print("wrote", path)
