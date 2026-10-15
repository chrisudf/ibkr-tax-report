"""PDF report generation (reportlab / platypus).

Landscape A4 working paper aimed at professional accountants: headline
figures, full detail schedules, reconciliation against IBKR's own numbers,
methodology with legislative references, and a disclaimer.
"""
from __future__ import annotations

import io
from html import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, PageBreak, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

from .outputs import TABLES, OTHER_INCOME_TABLES, _OI_COLS

INK = colors.HexColor("#1a2433")
MUTED = colors.HexColor("#5b6878")
RULE = colors.HexColor("#c9d2dd")
BAND = colors.HexColor("#eef2f7")
ACCENT = colors.HexColor("#0f4c81")
WARN_BG = colors.HexColor("#fdf3e3")
WARN_BORDER = colors.HexColor("#d9a441")


def _styles():
    base = dict(fontName="Helvetica", textColor=INK, alignment=TA_LEFT)
    return {
        "title": ParagraphStyle("title", fontSize=17, leading=21,
                                fontName="Helvetica-Bold", textColor=INK),
        "subtitle": ParagraphStyle("subtitle", fontSize=9.5, leading=13,
                                   textColor=MUTED, **{k: v for k, v in base.items()
                                                       if k != "textColor"}),
        "h2": ParagraphStyle("h2", fontSize=12, leading=15, spaceBefore=10, spaceAfter=4,
                             fontName="Helvetica-Bold", textColor=ACCENT),
        "body": ParagraphStyle("body", fontSize=8.5, leading=11.5, **base),
        "small": ParagraphStyle("small", fontSize=7.2, leading=9.4, **base),
        "cell": ParagraphStyle("cell", fontSize=7.2, leading=9.0, **base),
        "warn": ParagraphStyle("warn", fontSize=8.5, leading=11.5,
                               textColor=colors.HexColor("#7a5410"),
                               fontName="Helvetica", alignment=TA_LEFT),
    }


def _rate_label(rate: float) -> str:
    if abs(rate - 1 / 3) < 1e-9:
        return "33 1/3%"
    return f"{rate:.0%}"


def _money(v) -> str:
    if v in ("", None):
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"({abs(f):,.2f})" if f < 0 else f"{f:,.2f}"


_NUMERIC_COLS = {"qty", "open_cash", "close_cash", "gain_native", "gain_aud",
                 "premium_native", "premium_aud", "cost_native", "cost_aud",
                 "amount", "aud", "ib_realized_pl", "close_cash_alloc",
                 "days_held", "open_fx", "close_fx", "fx", "units", "aud_value", "aud_cost"}


def _fmt(col: str, v):
    if col in ("open_fx", "close_fx", "fx"):
        return f"{v:.4f}" if isinstance(v, (int, float)) and v else str(v or "")
    if col in ("days_held",):
        return str(v)
    if col in _NUMERIC_COLS:
        return _money(v)
    return str(v if v is not None else "")


def _data_table(cols, rows, styles, col_widths=None, note_col="note"):
    header = [c.replace("_", " ") for c in cols]
    data = [header]
    for r in rows:
        line = []
        for c in cols:
            v = _fmt(c, r.get(c, ""))
            if c in (note_col, "description", "source") and v:
                # Paragraph parses markup: statement text such as "AT&T" must be escaped
                line.append(Paragraph(escape(v), styles["cell"]))
            else:
                line.append(v)
        data.append(line)
    t = Table(data, repeatRows=1, colWidths=col_widths, hAlign="LEFT")
    style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.0),
        ("LEADING", (0, 0), (-1, -1), 8.6),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, 0), 0.7, RULE),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, RULE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    for i, c in enumerate(cols):
        if c in _NUMERIC_COLS:
            style.append(("ALIGN", (i, 0), (i, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def _kv_table(pairs, styles, width=180 * mm, bold_keys=()):
    data, style_extra = [], []
    for i, (k, v) in enumerate(pairs):
        data.append([k, _money(v) if isinstance(v, (int, float)) else str(v)])
        if k in bold_keys:
            style_extra += [("FONTNAME", (0, i), (-1, i), "Helvetica-Bold"),
                            ("BACKGROUND", (0, i), (-1, i), BAND)]
    t = Table(data, colWidths=[width * 0.62, width * 0.38], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.6),
        ("LEADING", (0, 0), (-1, -1), 11),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 2.6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
    ] + style_extra))
    return t


METHOD_POINTS = [
    ("Translation", "Every leg is translated to AUD at the RBA daily rate for its own "
     "transaction date (s 960-50 ITAA 1997): cost base at acquisition-date rate, capital "
     "proceeds at CGT-event-date rate. Weekend/holiday dates fall back to the most recent "
     "prior published rate; a date after the end of the rate table stops the run."),
    ("Dates", "Dates are those of IBKR's CSV, in Sydney time (AEST/AEDT): most US trades "
     "carry the next calendar day. IBKR's statement day ends at 10:00 Sydney, so a statement "
     "ending 30 June can hold rows dated 1 July; they are counted in the year of their date "
     "and listed in a warning. The ATO gives no rule on which time zone to use; the same "
     "convention should be kept from year to year."),
    ("Lot matching", "First-in-first-out per symbol, matching IBKR's default. Realised "
     "gain per parcel = opening cashflow + closing cashflow (both signed, inclusive of "
     "commissions), valid for long and short parcels."),
    ("Written options", "CGT event D2 at the date of grant (s 104-40(2)). Premiums on "
     "written options still open at 30 June are assessable in this year; the discount "
     "never applies to D2 gains (s 115-25(3)). Buying one back is a capital loss equal "
     "to the buy-back cost — the prevailing view, on which the ATO has published no "
     "ruling (a private ruling gives certainty). A deferred/closed-basis alternative, "
     "taxing each option in the year it is closed, is shown for comparison only."),
    ("Assignment / exercise", "The D2 gain is disregarded when a written option is "
     "exercised or assigned (s 104-40(5)); the premium is instead folded into the share "
     "parcel — reducing cost base of shares acquired under a put (s 134-1) or increasing "
     "capital proceeds of shares delivered under a call (s 116-65). Assignments that "
     "cancel a D2 gain returned in an earlier year are flagged for amendment "
     "(no time limit for that amendment: s 170(10AA) item 50 ITAA 1936). The premium "
     "is translated at the rate on the day the option was written."),
    ("Expiries", "Purchased options expiring worthless are a capital loss at expiry "
     "(CGT event C2, s 104-25)."),
    ("Discount", "Applied only to long parcels acquired at least 12 months before the "
     "CGT event (s 115-25(1)) at the entity's statutory rate, after applying losses "
     "against non-discountable gains first. Neither the acquisition day nor the event "
     "day counts (Acts Interpretation Act 1901 s 2G): bought 28 February 2023, the "
     "first discountable day is 1 March 2024."),
    ("Gains and losses", "Each CGT event is counted on its own in total gains and "
     "losses: an option written and bought back in the same year is a D2 gain (the "
     "premium) and a separate capital loss (the buy-back), as when the two fall in "
     "different years. The net capital gain is the same as with one net figure per "
     "contract, which is "
     "shown for comparison. Current-year losses are applied before prior-year ones "
     "(s 102-5)."),
    ("Foreign currency (Div 775)", "Non-AUD cash is a right to receive (credit balance) "
     "or an obligation to pay (debit balance) foreign currency. Spending, converting or "
     "withdrawing it is forex realisation event 2 (s 775-45); paying back a debit balance "
     "is FRE 4 (s 775-55). Units are matched first-in first-out (s 775-145(1)) using the "
     "cash flows in the statements, checked against each statement's Cash Report. For an "
     "AUD base-currency account IBKR's own Forex P/L Details is used instead. The "
     "$250,000 balance election (s 775-230) and the retranslation election (s 775-270) "
     "are not applied."),
    ("Corporate actions", "Splits are read from the statement's Corporate Actions "
     "section and applied chronologically. Unrecognised actions are listed as warnings "
     "and are never silently guessed."),
    ("Reconciliation", "Per-row realised P/L is compared with IBKR's Realized P/L "
     "column and year-end parcels with the statement's Open Positions section; "
     "differences are listed, with expected differences (e.g. premium folds) annotated."),
]

DISCLAIMER_EN = (
    "This document is a record-keeping and calculation working paper generated from the "
    "Interactive Brokers statement(s) listed above. It is not tax, financial or legal "
    "advice, and it does not determine whether the taxpayer holds securities on capital "
    "or revenue account, nor their residency or entity characterisation. Figures must be "
    "reviewed by a registered tax agent before lodgment. The producer of this document "
    "holds no AFSL and makes no recommendation about any financial product.")


def build_pdf(report: dict) -> bytes:
    styles = _styles()
    meta, s = report["meta"], report["summary"]
    buf = io.BytesIO()
    page = landscape(A4)

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(14 * mm, 12 * mm,
                          f"IBKR AU CGT report — {meta['account']} — {meta['fy']} — "
                          f"generated {meta['generated']} — ibkr-tax-report v{meta['tool_version']}")
        canvas.drawRightString(page[0] - 14 * mm, 12 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(RULE)
        canvas.line(14 * mm, 15 * mm, page[0] - 14 * mm, 15 * mm)
        canvas.restoreState()

    doc = BaseDocTemplate(buf, pagesize=page,
                          leftMargin=14 * mm, rightMargin=14 * mm,
                          topMargin=13 * mm, bottomMargin=18 * mm,
                          title=f"IBKR AU CGT report {meta['fy']} {meta['account']}",
                          author="ibkr-tax-report")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=on_page)])

    el = []
    el.append(Paragraph("Australian CGT working paper — Interactive Brokers", styles["title"]))
    el.append(Spacer(1, 2 * mm))
    el.append(Paragraph(
        f"Account <b>{meta['account']}</b> ({meta['name']}) &nbsp;•&nbsp; statement period "
        f"{meta['period']} &nbsp;•&nbsp; financial year <b>{meta['fy']}</b> &nbsp;•&nbsp; "
        f"entity: {s['entity']} &nbsp;•&nbsp; {meta['fx_source']}", styles["subtitle"]))
    el.append(Paragraph(f"Source: {meta['source_files']}", styles["subtitle"]))
    el.append(Spacer(1, 4 * mm))

    el.append(Paragraph("Headline figures (AUD)", styles["h2"]))
    lb = s.get("labels") or {}
    gains_label = f"Total current-year capital gains — {lb.get('gains', 'label 18H')}"
    net_label = f"NET CAPITAL GAIN — {lb.get('net', 'label 18A')}"
    d2_unknown = ([("    of which: fate after 30 June not in uploaded data (see warnings)",
                    s["d2_outcome_unknown"])]
                  if s.get("d2_outcome_unknown") else [])
    el.append(_kv_table([
        ("Closed gains — discount-eligible", s["closed_gains_discountable"]),
        ("Closed gains — not discountable", s["closed_gains_nondiscountable"]),
        ("Closed losses", s["closed_losses"]),
        ("Closed parcels net", s["closed_net"]),
        ("D2: written options open at 30 June (premiums at grant date)", s["d2_open_written"]),
        *d2_unknown,
        ("Current-year gains subtotal — strict ATO view", s["strict_subtotal"]),
        ("Prior-year losses carried in", s["carried_losses_input"]),
        ("Losses applied (non-discountable gains first)", s["losses_applied_total"]),
        ("    of which current-year losses — CGT schedule item 2, label B",
         s["current_year_losses_applied"]),
        ("    of which prior-year net losses — item 2, label C", s["carried_losses_applied"]),
        (f"CGT discount applied ({_rate_label(s['discount_rate'])} of remaining eligible gains)",
         s["discount_applied"]),
        (gains_label, s["total_capital_gains_18H"]),
        ("    for comparison: written options netted against their buy-backs",
         s["total_capital_gains_net_basis"]),
        (net_label, s["net_capital_gain_18A"]),
        (f"Capital losses carried forward — {lb.get('carried', 'label 18V')}",
         s["losses_carried_forward_18V"]),
        ("For comparison only: closed-basis (deferred) net", s["deferred_alternative"]),
    ], styles, width=200 * mm,
        bold_keys=(net_label, gains_label)))
    for note in report.get("entity_notes") or []:
        el.append(Paragraph("• " + escape(note), styles["small"]))
    el.append(Spacer(1, 3 * mm))

    oi = s["other_income"]
    el.append(Paragraph("Other amounts (AUD)", styles["h2"]))
    el.append(_kv_table([
        ("Dividends, gross (foreign income)", oi["dividends_aud"]),
        ("Foreign withholding tax (FITO candidate)", oi["withholding_tax_aud"]),
        *([("    of which above the 15% US treaty rate — reclaim from the IRS, not FITO",
            oi["withholding_above_treaty_aud"])] if oi.get("withholding_above_treaty_aud")
          else []),
        *([("Australian tax withheld in AUD (not FITO)", oi["australian_withholding_aud"])]
          if oi.get("australian_withholding_aud") else []),
        ("Interest, net (IBKR nets borrow fees into this figure)", oi["interest_aud"]),
        ("Account fees (review deductibility)", oi["fees_aud"]),
        ("Realised FX gain/loss — ordinary income, Div 775 (not CGT)", oi["forex_pl_aud"]),
    ], styles, width=200 * mm))
    if oi.get("forex_method"):
        el.append(Spacer(1, 1.5 * mm))
        el.append(Paragraph(f"<b>Div 775 method:</b> {escape(oi['forex_method'])}.",
                            styles["small"]))
    for note in report.get("forex_notes") or []:
        el.append(Paragraph("• " + escape(note), styles["small"]))

    if report["warnings"]:
        el.append(Spacer(1, 3 * mm))
        el.append(Paragraph("Warnings — read before relying on the figures", styles["h2"]))
        wtab = Table([[Paragraph(escape(w), styles["warn"])] for w in report["warnings"]],
                     colWidths=[250 * mm], hAlign="LEFT")
        wtab.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), WARN_BG),
            ("BOX", (0, 0), (-1, -1), 0.8, WARN_BORDER),
            ("LINEBELOW", (0, 0), (-1, -2), 0.3, WARN_BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        el.append(wtab)

    flags = report.get("amendment_flags", []) + report.get("cross_year_notes", [])
    if flags:
        el.append(Spacer(1, 3 * mm))
        el.append(Paragraph("Cross-year flags — amendments and tracking", styles["h2"]))
        for f in flags:
            el.append(Paragraph("• " + escape(f), styles["body"]))

    # Detail schedules
    for key, (cols, title) in TABLES.items():
        rows = report.get(key) or []
        if not rows and key in ("transfers", "unmatched", "fx_realisations"):
            continue
        # the landscape page has no room for it; the CSV keeps it
        cols = [c for c in cols if c != "discount_from"]
        el.append(PageBreak())
        el.append(Paragraph(f"{title} — {len(rows)} rows", styles["h2"]))
        if rows:
            el.append(_data_table(cols, rows, styles))
        else:
            el.append(Paragraph("None.", styles["body"]))

    # Other income detail
    el.append(PageBreak())
    el.append(Paragraph("Other income detail", styles["h2"]))
    for key, title in OTHER_INCOME_TABLES.items():
        rows = report["other_income"].get(key) or []
        if not rows:
            continue
        el.append(Paragraph(title, styles["body"]))
        el.append(Spacer(1, 1.2 * mm))
        el.append(_data_table(_OI_COLS, rows, styles,
                              col_widths=[16 * mm, 20 * mm, 150 * mm, 22 * mm,
                                          16 * mm, 22 * mm]))
        el.append(Spacer(1, 3 * mm))

    # Reconciliation
    rec = report["reconciliation"]
    el.append(PageBreak())
    el.append(Paragraph("Reconciliation against IBKR", styles["h2"]))
    el.append(Paragraph(
        f"Closing rows checked against IBKR's Realized P/L column: "
        f"<b>{rec['rows_ok']} of {rec['rows_checked']}</b> agree within $0.02.",
        styles["body"]))
    if rec["row_mismatches"]:
        el.append(Spacer(1, 1.5 * mm))
        el.append(_data_table(["symbol", "date", "mine", "ibkr", "diff"],
                              rec["row_mismatches"], styles))
        el.append(Paragraph(
            "Small same-second allocation differences net to ~zero and are timing only; "
            "investigate anything material.", styles["small"]))
    el.append(Spacer(1, 2 * mm))
    if rec["positions_applicable"]:
        el.append(Paragraph(
            f"Year-end open positions agreeing with the statement snapshot: "
            f"<b>{rec['positions_ok']}</b>. Differences listed below.", styles["body"]))
        if rec["position_diffs"]:
            el.append(Spacer(1, 1.5 * mm))
            el.append(_data_table(["symbol", "my_qty", "stmt_qty", "my_cost", "stmt_cost",
                                   "note"], rec["position_diffs"], styles,
                                  col_widths=[35 * mm, 20 * mm, 20 * mm, 24 * mm, 24 * mm,
                                              120 * mm]))
    else:
        el.append(Paragraph("Year-end position check not applicable for this data set.",
                            styles["body"]))

    # Methodology + disclaimer
    el.append(PageBreak())
    el.append(Paragraph("Methodology and legislative references", styles["h2"]))
    for name, text in METHOD_POINTS:
        el.append(Paragraph(f"<b>{name}.</b> {text}", styles["body"]))
        el.append(Spacer(1, 1.2 * mm))
    el.append(Spacer(1, 3 * mm))
    el.append(Paragraph("Disclaimer", styles["h2"]))
    el.append(Paragraph(DISCLAIMER_EN, styles["body"]))

    doc.build(el)
    return buf.getvalue()
