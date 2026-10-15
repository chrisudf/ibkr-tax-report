"""CSV / ZIP outputs for the tax report.

Two shapes are produced from the same report dict:
- one sectioned "workpaper" CSV that opens cleanly in Excel, and
- a ZIP of per-table CSVs for import into other systems.

All free-text cells are guarded against CSV/formula injection because these
files are opened in Excel by the target users.
"""
from __future__ import annotations

import csv
import io
import zipfile

_FORMULA_PREFIXES = ("=", "+", "@", "\t", "\r")


def safe_cell(v):
    """Neutralise spreadsheet formula injection in text cells. Numbers pass
    through; strings starting with a formula trigger get a leading apostrophe."""
    if isinstance(v, str) and v and (v[0] in _FORMULA_PREFIXES
                                     or (v[0] == "-" and not _is_numberish(v))):
        return "'" + v
    return v


def _is_numberish(s: str) -> bool:
    try:
        float(s.replace(",", ""))
        return True
    except ValueError:
        return False


TABLES = {
    "closed_lots": (
        ["category", "symbol", "currency", "qty", "open_date", "close_date", "days_held",
         "discount_from", "open_cash", "close_cash", "open_fx", "close_fx", "gain_native",
         "gain_aud",
         "short", "expiry", "discount_eligible", "note"],
        "Closed parcels — CGT events realised in the financial year (FIFO matched)"),
    "d2_open": (
        ["symbol", "write_date", "qty", "premium_native", "currency", "fx", "premium_aud",
         "expires", "after_fy_end"],
        "CGT event D2 — options written in the FY and not closed by year end "
        "(taxable at grant date; no discount, s 104-40(2), s 115-25(3))"),
    "transfers": (
        ["option", "kind", "date", "option_written", "qty", "premium_native", "currency",
         "folded_into"],
        "Assignments / exercises — D2 disregarded (s 104-40(5)); premium folded into the "
        "share parcel (ss 134-1, 116-65)"),
    "carry_forward": (
        ["category", "symbol", "qty", "acquired", "cost_native", "currency", "fx",
         "cost_aud"],
        "Open parcels held at the end of the financial year (30 June), carried forward — "
        "cost base locked at acquisition-date RBA rate"),
    "unmatched": (
        ["category", "symbol", "currency", "close_date", "qty", "close_cash",
         "ib_realized_pl", "gain_aud", "note"],
        "Closing trades whose opening parcels predate the uploaded data — REVIEW"),
    "fx_realisations": (
        ["date", "currency", "event", "units", "aud_value", "aud_cost", "gain_aud",
         "acquired", "source"],
        "Div 775 — forex realisations on foreign-currency cash in the FY: ordinary income or "
        "deduction, NOT capital gains. FIFO (s 775-145(1)); aud_value = AUD value of the "
        "units at the event, aud_cost = their forex cost base (FRE 2) or the proceeds of "
        "assuming the obligation (FRE 4)"),
}

OTHER_INCOME_TABLES = {
    "dividends": "Dividends (gross)",
    "withholding_tax": "Withholding tax (foreign income tax offset)",
    "australian_withholding": "Australian tax withheld in AUD (NOT a foreign income tax "
                              "offset)",
    "interest": "Interest (IBKR nets borrow fees into this section — do not deduct "
                "Borrow Fee Details again)",
    "fees": "Account fees (possible deduction — verify nature)",
    "borrow_fees": "Borrow fee details (INFORMATIONAL ONLY — already netted in Interest)",
    "forex_pl": "Realised FX gains/losses (ordinary income, Div 775 — NOT capital gains)",
}
_OI_COLS = ["currency", "date", "description", "amount", "fx", "aud"]


def _w(writer, row):
    writer.writerow([safe_cell(c) for c in row])


def _table_rows(rows: list[dict], cols: list[str]):
    for r in rows:
        yield [r.get(c, "") for c in cols]


def build_workpaper_csv(report: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    meta, s = report["meta"], report["summary"]

    _w(w, ["IBKR AUSTRALIAN CGT WORKPAPER", "", "", ""])
    for k, v in [("Account", meta["account"]), ("Name", meta["name"]),
                 ("Statement period", meta["period"]),
                 ("Financial year", meta["fy"]),
                 ("Entity type", s["entity"]),
                 ("Source files", meta["source_files"]),
                 ("Statement generated", meta["when_generated"]),
                 ("FX source", meta["fx_source"]),
                 ("Report generated", meta["generated"]),
                 ("Tool", f"ibkr-tax-report {meta['tool_version']}")]:
        _w(w, [k, v])
    _w(w, [])

    lb = s.get("labels") or {}
    _w(w, ["SECTION", "SUMMARY (all amounts AUD)"])
    for k, label in [
        ("closed_gains_discountable", "Closed gains — discount-eligible (held > 12 months)"),
        ("closed_gains_nondiscountable", "Closed gains — not discountable"),
        ("closed_losses", "Closed losses"),
        ("closed_net", "Closed parcels net"),
        ("d2_open_written", "D2: written options open at 30 June (premiums at grant date)"),
        ("strict_subtotal", "Current-year gains subtotal (strict ATO view)"),
        ("deferred_alternative", "Alternative: closed-basis only (deferred view, for "
                                 "comparison) — options taxed in the year closed, premium "
                                 "included"),
        ("current_year_losses", "Current-year capital losses (component)"),
        ("carried_losses_input", "Prior-year losses carried in (user input)"),
        ("losses_applied_total", "Losses applied"),
        ("current_year_losses_applied",
         f"  of which {lb.get('losses_current', 'current-year losses')}"),
        ("carried_losses_applied", f"  of which {lb.get('losses_prior', 'prior-year net losses')}"),
        ("discount_applied", "CGT discount applied"),
        ("total_capital_gains_18H",
         f"Total current-year capital gains ({lb.get('gains', 'label 18H')})"),
        ("total_capital_gains_net_basis",
         "  for comparison: total gains with written options netted against their buy-backs"),
        ("net_capital_gain_18A", f"NET CAPITAL GAIN ({lb.get('net', 'label 18A')})"),
        ("losses_carried_forward_18V",
         f"Losses carried forward ({lb.get('carried', 'label 18V')})"),
    ]:
        _w(w, [label, s[k]])
        if k == "d2_open_written" and s.get("d2_outcome_unknown"):
            _w(w, ["  ... of which: fate after 30 June NOT in uploaded data — may be "
                   "overstated, see WARNINGS", s["d2_outcome_unknown"]])
    for note in report.get("entity_notes") or []:
        _w(w, ["NOTE", note])
    oi = s["other_income"]
    _w(w, [])
    _w(w, ["SECTION", "OTHER AMOUNTS (AUD)"])
    _w(w, ["Dividends (gross) — foreign income", oi["dividends_aud"]])
    if oi.get("withholding_above_treaty_aud"):
        _w(w, ["Foreign tax withheld", oi["withholding_tax_aud"]])
        _w(w, ["  of which above the 15% US treaty rate (reclaim from the IRS, not FITO)",
               -oi["withholding_above_treaty_aud"]])
        _w(w, ["Foreign tax withheld less that excess (FITO candidate)",
               oi["fito_candidate_aud"]])
    else:
        _w(w, ["Foreign tax withheld (FITO candidate)", oi["withholding_tax_aud"]])
    if oi.get("australian_withholding_aud"):
        _w(w, ["Australian tax withheld (not FITO)", oi["australian_withholding_aud"]])
    _w(w, ["Interest (net; IBKR includes borrow fees here)", oi["interest_aud"]])
    _w(w, ["Account fees (review deductibility)", oi["fees_aud"]])
    _w(w, ["Realised FX gain/loss (Div 775 ordinary income)", oi["forex_pl_aud"]])
    _w(w, ["  method", oi.get("forex_method", "")])
    for note in report.get("forex_notes") or []:
        _w(w, ["  note", note])

    for key, (cols, title) in TABLES.items():
        rows = report.get(key) or []
        _w(w, [])
        _w(w, ["SECTION", f"{title} ({len(rows)} rows)"])
        _w(w, cols)
        for row in _table_rows(rows, cols):
            _w(w, row)

    if report.get("amendment_flags") or report.get("cross_year_notes"):
        _w(w, [])
        _w(w, ["SECTION", "CROSS-YEAR FLAGS — amendments / tracking"])
        for x in report.get("amendment_flags", []):
            _w(w, ["AMEND", x])
        for x in report.get("cross_year_notes", []):
            _w(w, ["TRACK", x])

    _w(w, [])
    _w(w, ["SECTION", "OTHER INCOME DETAIL"])
    for key, title in OTHER_INCOME_TABLES.items():
        rows = report["other_income"].get(key) or []
        if not rows:
            continue
        _w(w, [f"-- {title} ({len(rows)} rows)"])
        _w(w, _OI_COLS)
        for row in _table_rows(rows, _OI_COLS):
            _w(w, row)

    rec = report["reconciliation"]
    _w(w, [])
    _w(w, ["SECTION", "RECONCILIATION AGAINST IBKR"])
    _w(w, ["Closing rows checked against IBKR Realized P/L", rec["rows_checked"]])
    _w(w, ["... matching within $0.02", rec["rows_ok"]])
    _w(w, ["... mismatched (listed below)", rec["rows_mismatched"]])
    if rec["row_mismatches"]:
        _w(w, ["symbol", "date", "mine", "ibkr", "diff"])
        for r in rec["row_mismatches"]:
            _w(w, [r["symbol"], r["date"], r["mine"], r["ibkr"], r["diff"]])
    if rec["positions_applicable"]:
        _w(w, ["Year-end positions agreeing with statement", rec["positions_ok"]])
        if rec["position_diffs"]:
            _w(w, ["symbol", "my_qty", "stmt_qty", "my_cost", "stmt_cost", "note"])
            for r in rec["position_diffs"]:
                _w(w, [r["symbol"], r["my_qty"], r["stmt_qty"], r["my_cost"],
                       r["stmt_cost"], r["note"]])
    else:
        _w(w, ["Year-end position check", "not applicable (no Open Positions snapshot "
                                          "at the selected FY end)"])

    if report["warnings"]:
        _w(w, [])
        _w(w, ["SECTION", "WARNINGS — READ BEFORE RELYING ON FIGURES"])
        for x in report["warnings"]:
            _w(w, ["WARNING", x])
    return buf.getvalue()


def _single_table_csv(cols: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    _w(w, cols)
    for row in _table_rows(rows, cols):
        _w(w, row)
    return buf.getvalue()


def build_zip(report: dict, workpaper_csv: str, pdf_bytes: bytes | None) -> bytes:
    buf = io.BytesIO()
    fy = report["meta"]["fy"].replace("/", "-")
    acct = report["meta"]["account"] or "account"
    stem = f"{acct}_{fy}"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{stem}_workpaper.csv", workpaper_csv)
        if pdf_bytes:
            z.writestr(f"{stem}_report.pdf", pdf_bytes)
        for key, (cols, _title) in TABLES.items():
            z.writestr(f"{stem}_{key}.csv", _single_table_csv(cols, report.get(key) or []))
        for key in OTHER_INCOME_TABLES:
            rows = report["other_income"].get(key) or []
            if rows:
                z.writestr(f"{stem}_income_{key}.csv", _single_table_csv(_OI_COLS, rows))
        import json
        z.writestr(f"{stem}_summary.json", json.dumps(
            dict(meta=report["meta"], summary=report["summary"],
                 warnings=report["warnings"],
                 forex_notes=report.get("forex_notes", []),
                 amendment_flags=report.get("amendment_flags", []),
                 cross_year_notes=report.get("cross_year_notes", [])),
            indent=1, default=str))
    return buf.getvalue()
