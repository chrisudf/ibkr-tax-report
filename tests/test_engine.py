"""Engine unit tests. Scenarios are built with the demo statement builder;
FX uses the bundled RBA table, so all synthetic dates stay within 2023-2026."""
import datetime as dt

import pytest

from demo.make_demo import OPT, opos, statement, trade
from engine.cgt import (Options, compute_tax_report, detect_fy, fy_label, fy_of,
                        held_12_months, parse_option_symbol)
from engine.fx import RbaRates
from engine.outputs import build_workpaper_csv, safe_cell
from engine.parser import ParseError, merge_statements, parse_statement, occ_to_display

FX = RbaRates()


def run(trades, fy=2026, period="July 1, 2025 - June 30, 2026", entity="individual",
        carried=0.0, **kw):
    text = statement(period, "2026-07-05, 09:00:00 AEST", trades=trades, **kw)
    stmt = parse_statement(text, "test.csv")
    return compute_tax_report(stmt, Options(fy_end_year=fy, entity=entity,
                                            carried_losses=carried), FX)


# ---------------------------------------------------------------- helpers

def test_fy_helpers():
    assert fy_of(dt.date(2025, 6, 30)) == 2025
    assert fy_of(dt.date(2025, 7, 1)) == 2026
    assert fy_label(2026) == "FY2025-26"


def test_held_12_months_boundary():
    a = dt.date(2024, 8, 15)
    assert not held_12_months(a, dt.date(2025, 8, 15))   # exactly 12 months: not >
    assert held_12_months(a, dt.date(2025, 8, 16))
    leap = dt.date(2024, 2, 29)
    assert not held_12_months(leap, dt.date(2025, 2, 28))
    assert held_12_months(leap, dt.date(2025, 3, 1))


def test_option_symbol_parsing():
    assert parse_option_symbol("NVDL 17JUL26 26.67 P") == ("NVDL", "17JUL26", 26.67, "P")
    assert parse_option_symbol("AAPL") is None
    assert occ_to_display("SATS  260710P00105000") == "SATS 10JUL26 105 P"
    assert occ_to_display("NVDL  260717P00026670") == "NVDL 17JUL26 26.67 P"
    assert occ_to_display("AAPL") == "AAPL"


# ---------------------------------------------------------------- FIFO core

def test_fifo_multi_lot_order():
    rep = run([
        trade("Stocks", "XYZ", "2025-08-01, 10:00:00", 10, -1000, -1, code="O"),
        trade("Stocks", "XYZ", "2025-09-01, 10:00:00", 10, -1200, -1, code="O"),
        trade("Stocks", "XYZ", "2026-02-01, 10:00:00", -15, 1800, -1, code="C"),
    ])
    lots = rep["closed_lots"]
    assert [l["open_date"] for l in lots] == ["2025-08-01", "2025-09-01"]
    assert [l["qty"] for l in lots] == [10, 5]
    # remaining 5 shares carried at cost 1201/2
    cf = [r for r in rep["carry_forward"] if r["symbol"] == "XYZ"]
    assert len(cf) == 1 and cf[0]["qty"] == 5
    assert cf[0]["cost_native"] == pytest.approx(600.5, abs=0.01)


def test_short_flip_in_one_row():
    rep = run([
        trade("Stocks", "XYZ", "2025-08-01, 10:00:00", 10, -1000, 0, code="O"),
        trade("Stocks", "XYZ", "2025-09-01, 10:00:00", -25, 2500, 0, code="C;O"),
    ])
    assert len(rep["closed_lots"]) == 1
    cf = [r for r in rep["carry_forward"] if r["symbol"] == "XYZ"]
    assert cf[0]["qty"] == -15
    assert cf[0]["cost_native"] == pytest.approx(-1500, abs=0.01)


# ---------------------------------------------------------------- options

def test_d2_strict_vs_deferred():
    rep = run([trade(OPT, "ABC 17JUL26 50 P", "2026-05-10, 10:00:00", -1, 300, -1, code="O")])
    s = rep["summary"]
    assert s["d2_open_written"] > 0
    assert s["deferred_alternative"] == 0.0
    assert s["strict_subtotal"] == pytest.approx(s["d2_open_written"])
    assert len(rep["d2_open"]) == 1
    assert rep["d2_open"][0]["premium_native"] == 299.0


def test_written_closed_same_fy_nets_out_no_d2():
    rep = run([
        trade(OPT, "ABC 17JUL26 50 P", "2025-08-10, 10:00:00", -1, 300, -1, code="O"),
        trade(OPT, "ABC 17JUL26 50 P", "2025-11-10, 10:00:00", 1, -100, -1, code="C"),
    ])
    assert rep["summary"]["d2_open_written"] == 0.0
    assert len(rep["closed_lots"]) == 1
    assert rep["closed_lots"][0]["gain_native"] == pytest.approx(198.0)
    assert rep["closed_lots"][0]["discount_eligible"] == ""   # short: never discountable


def test_prior_fy_written_buyback_is_close_leg_only():
    rep = run([
        trade(OPT, "ABC 19DEC25 50 P", "2025-06-10, 10:00:00", -1, 500, -1, code="O"),
        trade(OPT, "ABC 19DEC25 50 P", "2025-09-15, 10:00:00", 1, -200, -1, code="C"),
    ], period="July 1, 2024 - June 30, 2026")
    row = rep["closed_lots"][0]
    aud_close, _ = FX.to_aud(-201.0, "USD", dt.date(2025, 9, 15))
    assert row["gain_aud"] == pytest.approx(round(aud_close, 2))
    assert "D2 gain in FY2024-25" in row["note"]


def test_long_option_expiry_c2_loss():
    rep = run([
        trade(OPT, "ABC 16JAN26 200 C", "2025-08-04, 10:00:00", 1, -400, -1, code="O"),
        trade(OPT, "ABC 16JAN26 200 C", "2026-01-17, 16:20:00", -1, 0, 0, code="C;Ep"),
    ])
    row = rep["closed_lots"][0]
    assert row["expiry"] == "Y"
    assert row["gain_native"] == pytest.approx(-401.0)
    assert rep["summary"]["closed_losses"] < 0


def test_assignment_fold_reduces_stock_cost_and_cancels_d2():
    rep = run([
        trade(OPT, "PFE 21NOV25 25 P", "2025-10-06, 10:00:00", -1, 80, -1, code="O"),
        trade(OPT, "PFE 21NOV25 25 P", "2025-11-22, 16:20:00", 1, 0, 0, code="A;C"),
        trade("Stocks", "PFE", "2025-11-22, 16:20:00", 100, -2500, 0, code="A;O"),
    ])
    assert rep["summary"]["d2_open_written"] == 0.0
    assert rep["closed_lots"] == []            # no realised CGT event at all
    assert len(rep["transfers"]) == 1
    assert rep["transfers"][0]["folded_into"] == "PFE"
    cf = [r for r in rep["carry_forward"] if r["symbol"] == "PFE"][0]
    assert cf["cost_native"] == pytest.approx(2500 - 79.0, abs=0.01)


def test_assignment_of_prior_year_written_flags_amendment():
    rep = run([
        trade(OPT, "PFE 21NOV25 25 P", "2025-06-06, 10:00:00", -1, 80, -1, code="O"),
        trade(OPT, "PFE 21NOV25 25 P", "2025-11-22, 16:20:00", 1, 0, 0, code="A;C"),
        trade("Stocks", "PFE", "2025-11-22, 16:20:00", 100, -2500, 0, code="A;O"),
    ], period="July 1, 2024 - June 30, 2026")
    assert len(rep["amendment_flags"]) == 1
    assert "104-40(5)" in rep["amendment_flags"][0]
    assert "FY2024-25" in rep["amendment_flags"][0]


def test_short_call_assignment_adds_premium_to_stock_proceeds():
    rep = run([
        trade("Stocks", "XYZ", "2025-08-01, 10:00:00", 100, -5000, 0, code="O"),
        trade(OPT, "XYZ 21NOV25 60 C", "2025-10-06, 10:00:00", -1, 150, -1, code="O"),
        trade(OPT, "XYZ 21NOV25 60 C", "2025-11-22, 16:20:00", 1, 0, 0, code="A;C"),
        trade("Stocks", "XYZ", "2025-11-22, 16:20:00", -100, 6000, 0, code="A;C"),
    ])
    assert rep["summary"]["d2_open_written"] == 0.0
    stock_rows = [r for r in rep["closed_lots"] if r["category"] == "Stocks"]
    assert len(stock_rows) == 1
    # proceeds 6000 + premium 149 - cost 5000
    assert stock_rows[0]["gain_native"] == pytest.approx(1149.0)


def test_cash_settled_assignment_without_stock_leg_falls_back():
    rep = run([
        trade(OPT, "SPX 21NOV25 5000 P", "2025-10-06, 10:00:00", -1, 800, -1, code="O"),
        trade(OPT, "SPX 21NOV25 5000 P", "2025-11-21, 16:20:00", 1, -1000, -1, code="A;C"),
    ])
    assert any("no matching share trade" in w for w in rep["warnings"])
    assert len(rep["closed_lots"]) == 1
    assert rep["closed_lots"][0]["gain_native"] == pytest.approx(799 - 1001)


# ---------------------------------------------------------------- corporate actions

def test_stock_split_adjusts_lots():
    ca = [["Corporate Actions", "Data", "Stocks", "USD", "2026-03-16",
           "2026-03-15, 20:25:00",
           "GOOG(US02079K3059) Split 10 for 1 (GOOG, ALPHABET INC-CL C, US02079K3059)",
           180, 0, 0, 0, ""]]
    rep = run([
        trade("Stocks", "GOOG", "2025-07-10, 10:00:00", 20, -3600, -1, code="O"),
        trade("Stocks", "GOOG", "2026-05-01, 10:00:00", -100, 2100, -1, code="C"),
    ], corporate_actions=ca)
    assert rep["closed_lots"][0]["gain_native"] == pytest.approx(298.5)
    cf = [r for r in rep["carry_forward"] if r["symbol"] == "GOOG"][0]
    assert cf["qty"] == 100


def test_option_split_renames_contract():
    ca = [["Corporate Actions", "Data", "Equity and Index Options", "USD", "2026-06-26",
           "2026-06-26, 10:25:00",
           "NVDL(US38747R8271) Split 3 for 1 (NVDL  260717P00026670, NVDL 17JUL26 26.67 P, )",
           -2, 0, 0, 0, ""]]
    rep = run([
        trade(OPT, "NVDL 17JUL26 80 P", "2026-05-10, 10:00:00", -1, 300, -1, code="O"),
    ], corporate_actions=ca)
    assert len(rep["d2_open"]) == 1
    assert rep["d2_open"][0]["symbol"] == "NVDL 17JUL26 26.67 P"
    assert rep["d2_open"][0]["qty"] == -3


def test_unknown_corporate_action_warns():
    ca = [["Corporate Actions", "Data", "Stocks", "USD", "2026-03-16",
           "2026-03-15, 20:25:00",
           "ABC(US000) Merged (Acquisition) with XYZ for 1.5 shares", 10, 0, 0, 0, ""]]
    rep = run([trade("Stocks", "ABC", "2025-07-10, 10:00:00", 10, -100, 0, code="O")],
              corporate_actions=ca)
    assert any("NOT auto-processed" in w for w in rep["warnings"])


# ---------------------------------------------------------------- gaps in data

def test_unmatched_close_falls_back_to_ibkr_rpl():
    rep = run([trade("Stocks", "OLD", "2025-09-01, 10:00:00", -10, 1500, -1,
                     basis="-1000", rpl="499", code="C")])
    assert len(rep["unmatched"]) == 1
    assert rep["unmatched"][0]["gain_aud"] != "UNRESOLVED"
    aud, _ = FX.to_aud(499.0, "USD", dt.date(2025, 9, 1))
    assert rep["summary"]["closed_net"] == pytest.approx(round(aud, 2), abs=0.01)
    assert any("no opening lots" in w for w in rep["warnings"])


def test_unmatched_close_without_rpl_is_excluded():
    rep = run([trade("Stocks", "OLD", "2025-09-01, 10:00:00", -10, 1500, -1, code="C")])
    assert rep["unmatched"][0]["gain_aud"] == "UNRESOLVED"
    assert rep["summary"]["closed_net"] == 0.0
    assert any("EXCLUDED" in w for w in rep["warnings"])


# ---------------------------------------------------------------- entities & losses

ENTITY_CASE = [
    trade("Stocks", "AAPL", "2024-08-15, 10:00:00", 100, -15000, -1, code="O"),
    trade("Stocks", "AAPL", "2026-01-20, 10:00:00", -100, 19000, -1, code="C"),
]


@pytest.mark.parametrize("entity,rate", [("individual", 0.5), ("trust", 0.5),
                                         ("smsf", 1 / 3), ("company", 0.0)])
def test_entity_discount_rates(entity, rate):
    rep = run(list(ENTITY_CASE), period="July 1, 2024 - June 30, 2026", entity=entity)
    s = rep["summary"]
    gross = s["closed_gains_discountable"]
    assert gross > 0
    assert s["discount_applied"] == pytest.approx(gross * rate, abs=0.01)
    assert s["net_capital_gain_18A"] == pytest.approx(gross - gross * rate, abs=0.01)


def test_losses_applied_to_nondiscountable_first():
    rep = run(list(ENTITY_CASE) + [
        # non-discountable gain (short hold)
        trade("Stocks", "XYZ", "2025-09-01, 10:00:00", 10, -1000, 0, code="O"),
        trade("Stocks", "XYZ", "2025-10-01, 10:00:00", -10, 1400, 0, code="C"),
        # loss
        trade("Stocks", "LOSS", "2025-09-01, 10:00:00", 10, -2000, 0, code="O"),
        trade("Stocks", "LOSS", "2025-10-01, 10:00:00", -10, 1800, 0, code="C"),
    ], period="July 1, 2024 - June 30, 2026")
    s = rep["summary"]
    nd = s["closed_gains_nondiscountable"]
    loss = -s["closed_losses"]
    assert 0 < loss < nd
    # all loss absorbed by non-discountable gain; discount base untouched
    assert s["discount_applied"] == pytest.approx(s["closed_gains_discountable"] * 0.5, abs=0.01)
    assert s["net_capital_gain_18A"] == pytest.approx(
        (nd - loss) + s["closed_gains_discountable"] * 0.5, abs=0.02)


def test_carried_losses_and_18v():
    rep = run([
        trade("Stocks", "XYZ", "2025-09-01, 10:00:00", 10, -1000, 0, code="O"),
        trade("Stocks", "XYZ", "2025-10-01, 10:00:00", -10, 1400, 0, code="C"),
    ], carried=10000.0)
    s = rep["summary"]
    assert s["net_capital_gain_18A"] == 0.0
    assert s["losses_carried_forward_18V"] == pytest.approx(10000.0 - s["closed_net"], abs=0.02)


# ---------------------------------------------------------------- parser / merge

def test_merge_dedupes_overlapping_files():
    text = statement("July 1, 2025 - June 30, 2026", "2026-07-05, 09:00:00",
                     trades=list(ENTITY_CASE[1:]))
    s1 = parse_statement(text, "a.csv")
    s2 = parse_statement(text, "b.csv")
    merged = merge_statements([s1, s2])
    assert len(merged.trades) == 1
    assert any("overlapping" in w for w in merged.warnings)


def test_same_second_duplicate_fills_survive_dedupe():
    t = trade("Stocks", "XYZ", "2025-09-01, 10:00:00", 10, -1000, 0, code="O")
    text = statement("July 1, 2025 - June 30, 2026", "2026-07-05, 09:00:00",
                     trades=[t, list(t)])
    merged = merge_statements([parse_statement(text, "a.csv"),
                               parse_statement(text, "b.csv")])
    assert len(merged.trades) == 2


def test_account_mismatch_rejected():
    a = statement("July 1, 2025 - June 30, 2026", "x", trades=[])
    b = a.replace("U9999999", "U8888888")
    with pytest.raises(ParseError, match="different accounts"):
        merge_statements([parse_statement(a, "a.csv"), parse_statement(b, "b.csv")])


def test_non_ibkr_csv_rejected():
    with pytest.raises(ParseError, match="does not look like an IBKR"):
        parse_statement("name,age\nalice,3\n", "junk.csv")


# ---------------------------------------------------------------- outputs

def test_csv_injection_guarded():
    assert safe_cell("=HYPERLINK('evil')") == "'=HYPERLINK('evil')"
    assert safe_cell("+SUM(A1)") == "'+SUM(A1)"
    assert safe_cell("-1200.5") == "-1200.5"       # numeric-looking strings untouched
    assert safe_cell(-5.0) == -5.0
    assert safe_cell("-not a number") == "'-not a number"
    rep = run([
        trade("Stocks", "XYZ", "2025-09-01, 10:00:00", 10, -1000, 0, code="O"),
    ])
    rep["warnings"].append("=cmd|dangerous")
    text = build_workpaper_csv(rep)
    assert "'=cmd|dangerous" in text


def test_fx_aud_passthrough_and_weekend_fallback():
    aud, rate = FX.to_aud(100.0, "AUD", dt.date(2025, 8, 10))
    assert (aud, rate) == (100.0, 1.0)
    # 2025-08-09/10 is a weekend: falls back to Friday's rate
    r_sat, used = FX.rate("USD", dt.date(2025, 8, 9))
    r_fri, _ = FX.rate("USD", dt.date(2025, 8, 8))
    assert r_sat == r_fri and used == dt.date(2025, 8, 8)


# ---------------------------------------------------------------- FY windowing

def _div(d, amt, desc="XYZ Cash Dividend"):
    return ["Dividends", "Data", "USD", d, desc, amt]


def test_cash_items_outside_fy_excluded():
    """Statements often span more than the reported FY (extra FIFO history, or a
    period running past 30 June). Only in-FY income belongs in this return."""
    rep = run([trade("Stocks", "XYZ", "2025-09-01, 10:00:00", 10, -1000, 0, code="O")],
              period="July 1, 2024 - August 14, 2026",
              dividends=[_div("2024-09-02", 1000.0),    # prior FY
                         _div("2025-11-14", 25.0),      # in FY
                         _div("2026-07-15", 500.0)],    # next FY
              interest=[["Interest", "Data", "USD", "2026-08-03", "USD Credit Interest", 40.0]])
    oi = rep["summary"]["other_income"]
    assert oi["dividends_by_ccy"] == {"USD": 25.0}
    assert oi["interest_aud"] == 0.0
    assert any("dated outside FY2025-26" in w for w in rep["warnings"])
    assert len(rep["other_income"]["dividends"]) == 1


def test_cash_items_on_fy_boundaries_included():
    rep = run([trade("Stocks", "XYZ", "2025-09-01, 10:00:00", 10, -1000, 0, code="O")],
              period="July 1, 2025 - June 30, 2026",
              dividends=[_div("2025-07-01", 10.0), _div("2026-06-30", 20.0)])
    assert rep["summary"]["other_income"]["dividends_by_ccy"] == {"USD": 30.0}
    assert not any("dated outside" in w for w in rep["warnings"])


def test_statement_past_fy_end_warns():
    rep = run([trade("Stocks", "XYZ", "2025-09-01, 10:00:00", 10, -1000, 0, code="O")],
              period="July 1, 2025 - August 14, 2026")
    assert any("past the end of FY2025-26" in w for w in rep["warnings"])


def test_bundled_rba_table_covers_reportable_years():
    """A stale rate table raises FxError on any leg past its last published date,
    which blocks the whole run — keep the bundle ahead of the latest closed FY."""
    _, last = FX.coverage("USD")
    assert last >= dt.date(2026, 6, 30)


# ---------------------------------------------------------------- FY detection

def _stmt(period, trades, **kw):
    return parse_statement(statement(period, "2026-08-18, 09:00:00 AEST", trades=trades, **kw),
                           "test.csv")


def test_detect_fy_prefers_the_latest_finished_year():
    """Statements are pulled after year end, so they routinely run past 30 June.
    The barely-started year is not the one being lodged."""
    st = _stmt("November 11, 2025 - August 14, 2026", [
        trade("Stocks", "XYZ", "2026-02-01, 10:00:00", 10, -1000, 0, code="O"),
        trade("Stocks", "XYZ", "2026-07-20, 10:00:00", -10, 1100, 0, code="C"),
    ])
    assert detect_fy(st) == 2026
    assert fy_of(st.period_end) == 2027          # what the old rule would have picked


def test_detect_fy_breaks_whole_year_ties_toward_the_latest():
    """Day-count majority cannot separate two complete years; uploading consecutive
    years for FIFO history is the recommended workflow, so this must not tie."""
    st = _stmt("July 1, 2024 - June 30, 2026", [
        trade("Stocks", "XYZ", "2024-09-02, 10:00:00", 10, -1000, 0, code="O"),
        trade("Stocks", "XYZ", "2025-11-03, 10:00:00", -10, 1100, 0, code="C"),
    ])
    assert detect_fy(st) == 2026


def test_detect_fy_falls_back_when_no_year_has_finished():
    st = _stmt("July 2, 2026 - August 14, 2026", [
        trade("Stocks", "XYZ", "2026-07-02, 10:00:00", 10, -1000, 0, code="O"),
    ])
    assert detect_fy(st) == 2027


def test_detect_fy_uses_income_rows_when_there_are_no_trades():
    st = _stmt("July 1, 2025 - August 14, 2026", [],
               dividends=[_div("2026-03-13", 32.76)])
    assert detect_fy(st) == 2026


def test_detect_fy_without_a_period_is_undetermined():
    st = _stmt("July 1, 2025 - June 30, 2026", [])
    st.period_end = None
    assert detect_fy(st) is None
