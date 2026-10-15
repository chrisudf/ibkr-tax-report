"""Div 775 on foreign-currency cash (engine/forex.py and its use in the report)."""
import io
import random
import zipfile
from datetime import date, timedelta

import pytest

from demo.make_demo import fy25, fy26, statement, trade
from engine import (Options, build_pdf, build_workpaper_csv, build_zip, compute_tax_report,
                    merge_statements, parse_statement)
from engine import forex
from engine.forex import Flow
from engine.fx import RbaRates

FX = RbaRates()


class FakeRates:
    """A$1 = rate units, one rate per date — like RbaRates.to_aud."""

    def __init__(self, rates):
        self.rates = rates

    def to_aud(self, amount, ccy, d):
        return amount / self.rates[d], self.rates[d]


# ---------------------------------------------------------------- the ATO's example

def test_reproduces_the_ato_fifo_worked_example():
    """ATO, "Use of first-in first-out method for fungible assets, rights and
    obligations" (QC18069): a US$ account that goes into debit and is repaid."""
    D = date
    rates = {D(2004, 9, 1): 0.72, D(2004, 9, 15): 0.70, D(2004, 10, 30): 0.73,
             D(2004, 12, 31): 0.75, D(2005, 3, 15): 0.78, D(2005, 5, 30): 0.76}
    flows = [Flow(D(2004, 9, 1), "USD", 1000, None, "deposit"),
             Flow(D(2004, 9, 15), "USD", 2500, None, "deposit"),
             Flow(D(2004, 10, 30), "USD", -1000, None, "withdrawal"),
             Flow(D(2004, 12, 31), "USD", -3000, None, "withdrawal"),
             Flow(D(2005, 3, 15), "USD", -1600, None, "withdrawal"),
             Flow(D(2005, 5, 30), "USD", 1200, None, "deposit")]
    for i, f in enumerate(flows):
        f.seq = i
    rows, lots = forex.realise(flows, FakeRates(rates))
    got = [(r["date"], r["event"][:5], r["units"], r["aud_value"], r["aud_cost"], r["gain_aud"])
           for r in rows]
    # Calculation 1: FRE 2, loss A$19.03
    assert got[0] == ("2004-10-30", "FRE 2", 1000, 1369.86, 1388.89, -19.03)
    # Calculation 2: only US$2,500 was a right; the other US$500 opens an obligation
    assert got[1] == ("2004-12-31", "FRE 2", 2500, 3333.33, 3571.43, -238.10)
    # Calculation 3: FRE 4 repaying 500 @0.75 then 700 @0.78; the ATO rounds each
    # leg to the cent first (A$1,564.11), unrounded it is A$1,564.10 — same loss
    d4, ev, units, paid, proceeds, gain = got[2]
    assert (d4, ev, units, paid, gain) == ("2005-05-30", "FRE 4", 1200, 1578.95, -14.84)
    assert proceeds == pytest.approx(1564.11, abs=0.011)
    assert len(rows) == 3
    # left: a debit balance of US$900 from the 15 March withdrawal
    assert [round(l.units, 2) for l in lots["USD"]] == [-900.0]


def test_identity_realised_equals_aud_out_minus_in_plus_closing_cost():
    """Whatever the order of flows, every AUD amount in or out is either realised
    or still sitting in an open lot: R = AUD out - AUD in + signed closing cost."""
    rnd = random.Random(775)
    for trial in range(200):
        d0 = date(2024, 1, 1)
        days = [d0 + timedelta(days=rnd.randrange(400)) for _ in range(rnd.randrange(1, 40))]
        rates = {d: rnd.uniform(0.6, 0.75) for d in days}
        flows = []
        for i, d in enumerate(sorted(days)):
            amt = round(rnd.uniform(-5000, 5000), 2) or 1.0
            exact = round(abs(amt) / rnd.uniform(0.6, 0.75), 2) if rnd.random() < 0.3 else None
            flows.append(Flow(d, "USD", amt, exact, "x", i))
        rows, lots = forex.realise(flows, FakeRates(rates))

        def aud(f):
            return f.aud if f.aud is not None else abs(f.amount) / rates[f.d]
        out_aud = sum(aud(f) for f in flows if f.amount < 0)
        in_aud = sum(aud(f) for f in flows if f.amount > 0)
        closing = sum(l.units * l.aud_per_unit for l in lots.get("USD", []))
        realised = sum(r["gain_aud"] for r in rows)
        assert realised == pytest.approx(out_aud - in_aud + closing, abs=0.01 * (len(rows) + 1))
        units_left = sum(l.units for l in lots.get("USD", []))
        assert units_left == pytest.approx(sum(f.amount for f in flows), abs=1e-6)
        # one-sided pool: never credit and debit lots at the same time
        assert len({l.units > 0 for l in lots.get("USD", [])}) <= 1


def test_receipts_before_payments_on_the_same_day():
    # IBKR's automatic conversion (AFx) lands a second after the purchase it pays
    # for; it must not read as an overdraft repaid
    d = date(2025, 3, 3)
    flows = [Flow(d, "USD", -1000, None, "buy", 0), Flow(d, "USD", 1000, 1600.0, "AFx", 1)]
    rows, lots = forex.realise(flows, FakeRates({d: 0.625}))
    assert [r["event"][:5] for r in rows] == ["FRE 2"]
    assert rows[0]["aud_cost"] == 1600.0 and rows[0]["aud_value"] == 1600.0   # 1000 / 0.625
    assert lots["USD"] == []


# ---------------------------------------------------------------- parsing


def _stmt(trades, extra="", base="USD", **kw):
    text = statement("July 1, 2025 - June 30, 2026", "2026-07-05, 09:00:00 AEST",
                     trades=trades, **kw)
    text = text.replace("Base Currency,USD", f"Base Currency,{base}")
    return parse_statement(text + extra, "t.csv")


def test_cash_report_and_deposits_are_parsed():
    st = _stmt([trade("Forex", "AUD.USD", "2025-08-01, 10:00:00", -1000, 650, -2)],
               deposits=[["Deposits & Withdrawals", "Data", "AUD", "2025-07-31",
                          "Electronic Fund Transfer", 1000]],
               cash_start={"AUD": 0.0, "USD": 100.0})
    assert [(d.currency, d.d, d.amount) for d in st.deposits] == [("AUD", date(2025, 7, 31), 1000)]
    (cr,) = st.cash_reports
    assert cr.start == {"AUD": 0.0, "USD": 100.0}
    assert cr.end == {"AUD": 0.0, "USD": 748.0}
    assert (cr.period_start, cr.period_end) == (date(2025, 7, 1), date(2026, 6, 30))


def test_forex_commission_column_named_after_any_base_currency():
    extra = ("Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,"
             "Quantity,T. Price,,Proceeds,Comm in AUD,,,MTM in AUD,Code\n"
             'Trades,Data,Order,Forex,USD,AUD.USD,"2025-08-01, 10:00:00",-1000,0.65,,650,-3,,,,\n')
    st = _stmt([], extra=extra, base="AUD")
    (fx_trade,) = [t for t in st.trades if t.category == "Forex"]
    assert fx_trade.comm == -3


def test_merge_keeps_every_cash_report():
    a = _stmt([], cash_start={"USD": 0.0})
    b = parse_statement(statement("July 1, 2024 - June 30, 2025", "x", trades=[],
                                  cash_start={"USD": 5.0}), "b.csv")
    merged = merge_statements([a, b])
    assert [r.start["USD"] for r in merged.cash_reports] == [5.0, 0.0]   # by period


# ---------------------------------------------------------------- in the report


def _report(stmt, fy=2026):
    return compute_tax_report(stmt, Options(fy_end_year=fy), FX)


def test_usd_base_does_not_count_ibkr_forex_pl_details():
    """The audit's finding: IBKR's Forex P/L Details for a USD-base account is the
    AUD cash measured in USD — not a Div 775 amount for an Australian resident."""
    fxpl = ("Forex P/L Details,Header,Asset Category,Currency,Description,Date/Time,"
            "FX Currency,Quantity,Proceeds in USD,Basis in USD,Realized P/L in USD,Code\n"
            'Forex P/L Details,Data,Forex,USD,Forex -1000 AUD.USD,"2025-08-01, 10:00:00",'
            "AUD,-1000,650,-640,500,\n")
    st = _stmt([], extra=fxpl, cash_start={"AUD": 0.0, "USD": 0.0})
    rep = _report(st)
    oi = rep["summary"]["other_income"]
    assert oi["forex_pl_aud"] == 0.0                     # was A$500 / RBA rate before the fix
    assert oi["forex_method"].startswith("computed from cash flows")
    assert any("'Forex P/L Details'" in n and "not used" in n for n in rep["forex_notes"])
    assert rep["other_income"]["forex_pl"] == []


def test_aud_base_still_uses_ibkr_forex_pl_details():
    fxpl = ("Forex P/L Details,Header,Asset Category,Currency,Description,Date/Time,"
            "FX Currency,Quantity,Proceeds in AUD,Basis in AUD,Realized P/L in AUD,Code\n"
            'Forex P/L Details,Data,Forex,AUD,Forex -650 USD.AUD,"2025-08-01, 10:00:00",'
            "USD,-650,1000,-990,10,\n")
    rep = _report(_stmt([], extra=fxpl, base="AUD"))
    oi = rep["summary"]["other_income"]
    assert oi["forex_pl_aud"] == 10.0
    assert oi["forex_method"].startswith("IBKR Forex P/L Details")
    assert rep["fx_realisations"] == []


def test_demo_div775_snapshot():
    st = merge_statements([parse_statement(fy25, "demo_fy2025.csv"),
                           parse_statement(fy26, "demo_fy2026.csv")])
    r25, r26 = _report(st, 2025), _report(st, 2026)
    # FY25 checked by hand: AAPL bought with USD converted at 0.6630 (A$25,000 exact)
    # but valued at the RBA 0.6620 -> +34.18; TSLA paid from the rest of that lot
    # plus the AFx conversion (A$14,000 exact) -> +145.39
    by_date = {r["date"]: r for r in r25["fx_realisations"]}
    assert by_date["2024-08-15"]["gain_aud"] == 34.18
    assert by_date["2025-03-03"]["gain_aud"] == 145.39
    assert by_date["2025-03-03"]["acquired"] == "2024-08-14 to 2025-03-03"
    assert r25["summary"]["other_income"]["forex_pl_aud"] == 179.57
    # FY26: GOOG paid from the last AFx lot and the TSLA sale proceeds -> -127.69
    first = r26["fx_realisations"][0]
    assert (first["date"], first["gain_aud"]) == ("2025-07-10", -127.69)
    assert r26["summary"]["other_income"]["forex_pl_aud"] == -1028.44
    assert len(r26["fx_realisations"]) == 8
    # the schedule foots: the headline is the sum of the rows as printed
    for r in (r25, r26):
        assert round(sum(x["gain_aud"] for x in r["fx_realisations"]), 2) ==             r["summary"]["other_income"]["forex_pl_aud"]
    assert all(r["event"].startswith("FRE 2") for r in r26["fx_realisations"])
    # both demo files reconcile exactly; no Div 775 warning
    assert sum("reconciles." in n for n in r26["forex_notes"]) == 2
    assert not any("DIV 775" in w or "Cash Report" in w for w in r26["warnings"])
    # CGT figures are untouched by the cash side
    assert r26["summary"]["net_capital_gain_18A"] == 2842.74


def test_overdraft_then_repayment_is_fre4():
    st = _stmt([trade("Stocks", "XYZ", "2025-08-01, 10:00:00", 10, -1000, 0, code="O"),
                trade("Stocks", "XYZ", "2026-02-02, 10:00:00", -10, 1100, 0, code="C")],
               cash_start={"USD": 0.0})
    (row,) = _report(st)["fx_realisations"]
    assert row["event"].startswith("FRE 4") and row["units"] == 1000
    rate_out = FX.rate("USD", date(2025, 8, 1))[0]
    rate_in = FX.rate("USD", date(2026, 2, 2))[0]
    assert row["gain_aud"] == pytest.approx(1000 / rate_out - 1000 / rate_in, abs=0.01)


def test_cash_report_gap_warns():
    text = statement("July 1, 2025 - June 30, 2026", "x", cash_start={"USD": 0.0},
                     trades=[trade("Stocks", "XYZ", "2025-08-01, 10:00:00", 10, -1000, 0,
                                   code="O")])
    text = text.replace("Ending Cash,USD,-1000.0", "Ending Cash,USD,-1100.0")
    rep = _report(parse_statement(text, "gap.csv"))
    (w,) = [w for w in rep["warnings"] if w.startswith("DIV 775 MAY BE INCOMPLETE")]
    assert "gap of -100.00 USD" in w and "gap.csv" in w


def test_rows_dated_outside_the_period_still_reconcile():
    # IBKR's ending cash includes rows a statement lists outside its own period
    # (found on real statements: June interest posted in early July)
    text = statement("July 1, 2025 - June 30, 2026", "x", cash_start={"USD": 0.0},
                     trades=[trade("Stocks", "XYZ", "2025-08-01, 10:00:00", 10, -1000, 0,
                                   code="O")],
                     interest=[["Interest", "Data", "USD", "2026-07-03",
                                "USD Credit Interest for Jun-2026", 18.16]])
    a = parse_statement(text, "a.csv")
    b = parse_statement(statement("July 1, 2025 - June 30, 2026", "x", trades=[]), "b.csv")
    rep = _report(merge_statements([a, b]))
    assert not any("MAY BE INCOMPLETE" in w for w in rep["warnings"])
    assert any("a.csv" in n and "reconciles." in n for n in rep["forex_notes"])


def test_opening_balance_without_history_warns_and_uses_rba_rate():
    st = _stmt([trade("Stocks", "XYZ", "2025-08-01, 10:00:00", 10, -1000, 0, code="O")],
               cash_start={"USD": 5000.0})
    rep = _report(st)
    assert any(w.startswith("DIV 775 OPENING BALANCE") and "5,000.00" in w
               for w in rep["warnings"])
    (row,) = rep["fx_realisations"]
    assert row["acquired"] == "2025-07-01"
    open_rate = FX.rate("USD", date(2025, 7, 1))[0]
    assert row["aud_cost"] == pytest.approx(1000 / open_rate, abs=0.01)


def test_no_cash_report_warns():
    rep = _report(_stmt([trade("Stocks", "XYZ", "2025-08-01, 10:00:00", 10, -1000, 0,
                               code="O")]))
    assert any(w.startswith("No Cash Report section") for w in rep["warnings"])


def test_flows_after_fy_end_are_not_needed():
    # a file running past 30 June must not demand rates beyond it: a cash flow
    # after the end of the RBA table would otherwise stop the whole run
    beyond = FX.coverage("USD")[1] + timedelta(days=40)
    text = statement(f"July 1, 2025 - {beyond:%B} {beyond.day}, {beyond.year}", "x",
                     trades=[], cash_start={"USD": 0.0},
                     dividends=[["Dividends", "Data", "USD", "2025-11-14", "XYZ Dividend", 10.0],
                                ["Dividends", "Data", "USD", str(beyond), "XYZ Dividend", 10.0]])
    rep = _report(parse_statement(text, "t.csv"))
    assert rep["fx_realisations"] == []
    assert rep["summary"]["other_income"]["dividends_aud"] > 0


def test_div775_reaches_every_output():
    st = merge_statements([parse_statement(fy25, "demo_fy2025.csv"),
                           parse_statement(fy26, "demo_fy2026.csv")])
    rep = _report(st)
    wp = build_workpaper_csv(rep)
    assert "Div 775 — forex realisations on foreign-currency cash" in wp
    assert "method,computed from cash flows" in wp
    assert "s 775-230" in wp
    pdf = build_pdf(rep)
    assert pdf.startswith(b"%PDF")
    z = zipfile.ZipFile(io.BytesIO(build_zip(rep, wp, pdf)))
    name = next(n for n in z.namelist() if n.endswith("_fx_realisations.csv"))
    assert len(z.read(name).decode().strip().splitlines()) == 1 + 8
    assert b"forex_notes" in z.read(next(n for n in z.namelist() if n.endswith(".json")))


def test_pdf_survives_markup_characters_in_statement_text():
    st = _stmt([], dividends=[["Dividends", "Data", "USD", "2025-11-14",
                               "AT&T <T> Cash Dividend", 25.0]],
               cash_start={"USD": 0.0})
    st.dividends[0].description = "AT&T <T> Cash Dividend"
    assert build_pdf(_report(st)).startswith(b"%PDF")
