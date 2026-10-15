"""Engine unit tests. Scenarios are built with the demo statement builder;
FX uses the bundled RBA table, so all synthetic dates stay within 2023-2026."""
import datetime as dt

import pytest

from demo.make_demo import OPT, instrument, opos, statement, trade
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


def test_assignment_after_fy_end_is_noted_not_returned_as_d2():
    """Written in-FY, assigned in the next one: s 104-40(5) disregards the D2
    gain, so nothing is returned — but the contract was open at 30 June and its
    absence from the D2 list has to be explained, or the list silently
    disagrees with the year-end position snapshot."""
    rep = run([
        trade(OPT, "ECHO 02JUL26 115 P", "2026-06-05, 10:00:00", -1, 906, -1, code="O"),
        trade(OPT, "ECHO 02JUL26 115 P", "2026-07-02, 06:20:00", 1, 0, 0, code="A;C"),
        trade("Stocks", "ECHO", "2026-07-02, 06:20:00", 100, -11500, 0, code="A;O"),
    ], period="July 1, 2025 - August 14, 2026")
    assert rep["summary"]["d2_open_written"] == 0.0
    assert rep["d2_open"] == []
    assert rep["amendment_flags"] == []        # return not lodged yet: nothing to amend
    assert rep["transfers"] == []              # the fold itself lands in the next FY
    note = [n for n in rep["cross_year_notes"] if n.startswith("ECHO 02JUL26 115 P")]
    assert len(note) == 1
    assert "104-40(5)" in note[0]
    assert "assignment on 2026-07-02" in note[0]
    assert "still open at 2026-06-30" in note[0]


def test_buyback_after_fy_end_keeps_d2_unlike_assignment():
    """The mirror case: a buy-back is not an exercise, so s 104-40(5) does not
    apply and the D2 gain stays in the year the option was written."""
    rep = run([
        trade(OPT, "ECHO 02JUL26 110 P", "2026-06-15, 10:00:00", -1, 552, -1, code="O"),
        trade(OPT, "ECHO 02JUL26 110 P", "2026-07-03, 10:00:00", 1, -300, -1, code="C"),
    ], period="July 1, 2025 - August 14, 2026")
    assert len(rep["d2_open"]) == 1
    assert rep["d2_open"][0]["symbol"] == "ECHO 02JUL26 110 P"
    note = [n for n in rep["cross_year_notes"] if n.startswith("ECHO 02JUL26 110 P")]
    assert len(note) == 1
    assert "capital loss in FY2026-27" in note[0]


def _d2_warnings(rep):
    return [w for w in rep["warnings"] if w.startswith("D2 OUTCOME NOT IN UPLOADED DATA")]


def test_d2_open_without_post_fy_data_warns_but_keeps_strict_total():
    """Only the FY statement uploaded: a put still open at 30 June may well have
    been assigned in July, which s 104-40(5) would disregard. The strict figure
    must not move, but the run has to say it cannot see the outcome."""
    rep = run([trade(OPT, "ECHO 02JUL26 115 P", "2026-06-05, 10:00:00", -1, 906, -1, code="O")])
    s = rep["summary"]
    prem = rep["d2_open"][0]["premium_aud"]
    assert s["d2_open_written"] == pytest.approx(prem)            # strict view unchanged
    assert s["strict_subtotal"] == pytest.approx(prem)
    assert s["d2_outcome_unknown"] == pytest.approx(prem)
    assert rep["d2_open"][0]["expires"] == "2026-07-02"
    assert rep["d2_open"][0]["after_fy_end"] == "NOT IN UPLOADED DATA"
    w = _d2_warnings(rep)
    assert len(w) == 1 and rep["warnings"][0] == w[0]             # leads the list
    assert "ECHO 02JUL26 115 P (written 2026-06-05, expires 2026-07-02" in w[0]
    assert f"A${prem:,.2f}" in w[0]
    assert "104-40(5)" in w[0]
    assert "starts on or before 2026-07-01 and ends after 2026-07-02" in w[0]  # the fix
    text = build_workpaper_csv(rep)
    assert "NOT in uploaded data" in text
    assert "D2 OUTCOME NOT IN UPLOADED DATA" in text


def test_d2_warning_lists_every_unresolved_contract_with_total():
    rep = run([
        trade(OPT, "ECHO 02JUL26 115 P", "2026-06-05, 10:00:00", -1, 906, -1, code="O"),
        trade(OPT, "NVDA 17JUL26 100 P", "2026-05-10, 10:00:00", -2, 600, -1.32, code="O"),
        # closed inside the FY: not part of D2 at all
        trade(OPT, "ABC 17JUL26 50 P", "2025-08-10, 10:00:00", -1, 300, -1, code="O"),
        trade(OPT, "ABC 17JUL26 50 P", "2025-11-10, 10:00:00", 1, -100, -1, code="C"),
    ])
    total = sum(r["premium_aud"] for r in rep["d2_open"])
    assert rep["summary"]["d2_outcome_unknown"] == pytest.approx(total, abs=0.01)
    w = _d2_warnings(rep)[0]
    assert "2 written option(s)" in w
    assert "ECHO 02JUL26 115 P" in w and "NVDA 17JUL26 100 P" in w and "ABC" not in w
    assert "ends after 2026-07-17" in w                           # the later expiry


def test_d2_post_fy_data_past_expiry_shows_outcome_no_warning():
    """With a statement running past expiry the outcome is visible: assigned
    (D2 disregarded), bought back (D2 stays) or expired (D2 stays)."""
    rep = run([
        trade(OPT, "ECHO 02JUL26 115 P", "2026-06-05, 10:00:00", -1, 906, -1, code="O"),
        trade(OPT, "ECHO 02JUL26 115 P", "2026-07-02, 06:20:00", 1, 0, 0, code="A;C"),
        trade("Stocks", "ECHO", "2026-07-02, 06:20:00", 100, -11500, 0, code="A;O"),
        trade(OPT, "ECHO 02JUL26 110 P", "2026-06-15, 10:00:00", -1, 552, -1, code="O"),
        trade(OPT, "ECHO 02JUL26 110 P", "2026-07-01, 10:00:00", 1, -300, -1, code="C"),
        trade(OPT, "NVDA 17JUL26 100 P", "2026-05-10, 10:00:00", -1, 300, -1, code="O"),
        trade(OPT, "NVDA 17JUL26 100 P", "2026-07-17, 16:20:00", 1, 0, 0, code="C;Ep"),
    ], period="November 11, 2025 - August 14, 2026")
    assert _d2_warnings(rep) == []
    assert rep["summary"]["d2_outcome_unknown"] == 0.0
    fates = {r["symbol"]: r["after_fy_end"] for r in rep["d2_open"]}
    assert fates == {"ECHO 02JUL26 110 P": "closed 2026-07-01",
                     "NVDA 17JUL26 100 P": "expired 2026-07-17"}
    assert "NOT in uploaded data" not in build_workpaper_csv(rep)


def test_d2_post_fy_file_starting_after_the_assignment_still_warns():
    """A post-year-end statement that starts after the July assignment cannot show
    it, however late it ends — the lot stays open and the run must say so."""
    fy = statement("July 1, 2025 - June 30, 2026", "2026-07-05, 09:00:00 AEST", trades=[
        trade(OPT, "ECHO 02JUL26 115 P", "2026-06-05, 10:00:00", -1, 906, -1, code="O")])
    late = statement("July 15, 2026 - August 14, 2026", "2026-08-15, 09:00:00 AEST",
                     trades=[])
    merged = merge_statements([parse_statement(fy, "fy.csv"), parse_statement(late, "c.csv")])
    rep = compute_tax_report(merged, Options(fy_end_year=2026), FX)
    w = _d2_warnings(rep)
    assert len(w) == 1
    assert "runs past the expiry of ECHO 02JUL26 115 P" in w[0]
    assert "no gap after 30 June" in w[0]
    assert rep["summary"]["d2_outcome_unknown"] == rep["summary"]["d2_open_written"]


def test_d2_post_fy_data_ending_before_expiry_still_warns():
    rep = run([trade(OPT, "MSFT 20NOV26 380 P", "2026-02-24, 10:00:00", -1, 3570, -1.7,
                     code="O")],
              period="November 11, 2025 - August 14, 2026")
    w = _d2_warnings(rep)
    assert len(w) == 1
    assert "uploaded data ends 2026-08-14" in w[0]
    assert "ends after 2026-11-20" in w[0]
    assert rep["summary"]["d2_outcome_unknown"] == rep["summary"]["d2_open_written"]


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


def test_ticker_rename_unified_via_conid():
    info = [instrument("Stocks", "ECHO, SATS", "47965865")]
    rep = run([
        trade("Stocks", "SATS", "2025-08-01, 10:00:00", 10, -1000, -1, code="O"),
        trade("Stocks", "ECHO", "2026-02-01, 10:00:00", -10, 1500, -1, code="C"),
    ], instrument_info=info)
    assert len(rep["closed_lots"]) == 1
    assert rep["closed_lots"][0]["symbol"] == "ECHO"
    assert not rep["unmatched"]
    assert any("SATS -> ECHO" in n for n in rep["warnings"])


def test_rename_direction_comes_from_underlying_not_list_order():
    """IBKR pairs the two names in Symbol in no dependable order — the same
    statement carries both "ECHO, SATS" and "SATG, ECHX". Underlying decides."""
    for symbols, old, new in (("ECHO, SATS", "SATS", "ECHO"),
                              ("SATG, ECHX", "SATG", "ECHX")):
        info = [instrument("Stocks", symbols, "47965865", underlying=new)]
        rep = run([
            trade("Stocks", old, "2025-08-01, 10:00:00", 10, -1000, -1, code="O"),
            trade("Stocks", new, "2026-02-01, 10:00:00", -10, 1500, -1, code="C"),
        ], instrument_info=info)
        assert rep["closed_lots"][0]["symbol"] == new
        assert not rep["unmatched"]


def test_option_underlying_rename_matches_open_and_close():
    """An option keeps its own conid across a rename of the underlying, so the
    instrument info never pairs the two names. Without re-pointing the option
    symbol the open leg is left carried as a written position at 30 June while
    the close falls back to IBKR's realized P/L — the premium counted twice."""
    info = [instrument("Stocks", "ECHO, SATS", "47965865", underlying="ECHO"),
            instrument(OPT, "SATS  260702P00110000", "885851383", underlying="SATS")]
    rep = run([
        trade(OPT, "SATS 02JUL26 110 P", "2026-06-15, 10:00:00", -1, 552, -1, code="O"),
        trade(OPT, "ECHO 02JUL26 110 P", "2026-06-27, 10:00:00", 1, -1189, -1,
              basis="551.74", rpl="-637.51", code="C"),
    ], instrument_info=info)
    assert not rep["unmatched"]
    assert not rep["d2_open"]
    lots = rep["closed_lots"]
    assert len(lots) == 1
    assert lots[0]["symbol"] == "ECHO 02JUL26 110 P"
    # premium in less buy-back out, each leg on its own date: a net loss
    assert lots[0]["gain_native"] == pytest.approx(552 - 1189 - 2)
    assert lots[0]["gain_aud"] < 0


def test_option_underlying_rename_keeps_open_position_as_d2():
    """The same re-pointing must not swallow a contract that is still open at
    30 June — it stays a D2 written position, just under the new name."""
    info = [instrument("Stocks", "ECHO, SATS", "47965865", underlying="ECHO"),
            instrument(OPT, "SATS  260702P00115000", "885851445", underlying="SATS")]
    rep = run([
        trade(OPT, "SATS 02JUL26 115 P", "2026-06-05, 10:00:00", -1, 906, -1, code="O"),
    ], instrument_info=info)
    assert len(rep["d2_open"]) == 1
    assert rep["d2_open"][0]["symbol"] == "ECHO 02JUL26 115 P"
    assert rep["d2_open"][0]["qty"] == -1


def test_ticker_rename_leaves_unrelated_options_alone():
    info = [instrument("Stocks", "ECHO, SATS", "47965865", underlying="ECHO")]
    rep = run([
        trade(OPT, "NVDA 02JUL26 185 P", "2026-06-11, 10:00:00", -1, 400, -1, code="O"),
    ], instrument_info=info)
    assert rep["d2_open"][0]["symbol"] == "NVDA 02JUL26 185 P"


XYZ_SPLIT = ["Corporate Actions", "Data", OPT, "USD", "2026-06-26", "2026-06-26, 10:25:00",
             "XYZ(US0000000001) Split 3 for 1 (XYZ   260710P00016670, XYZ 10JUL26 16.67 P, )",
             -4, 0, 0, 0, ""]
XYZ_WRITE = trade(OPT, "XYZ 10JUL26 50 P", "2026-06-23, 10:00:00", -2, 400, -2, code="O")
XYZ_PRE, XYZ_POST = "XYZ   260710P00050000", "XYZ   260710P00016670"   # instrument-info names


def _split_files(fy_name, cross_name, closing):
    """An FY Activity Statement and a cross-30-June one whose instrument info
    names the same option conid by different terms, as IBKR's exports can."""
    fy = statement("July 1, 2025 - June 30, 2026", "2026-07-05, 09:00:00 AEST",
                   trades=[XYZ_WRITE, *(t for t in closing if t[6] < "2026-07")],
                   corporate_actions=[XYZ_SPLIT],
                   instrument_info=[instrument(OPT, fy_name, "900000001", "XYZ")])
    cross = statement("November 11, 2025 - August 14, 2026", "2026-08-15, 09:00:00 AEST",
                      trades=[XYZ_WRITE, *closing], corporate_actions=[XYZ_SPLIT],
                      instrument_info=[instrument(OPT, cross_name, "900000001", "XYZ")])
    return merge_statements([parse_statement(fy, "fy.csv"), parse_statement(cross, "c.csv")])


XYZ_EXPIRE = trade(OPT, "XYZ 10JUL26 16.67 P", "2026-07-10, 16:20:00", 6, 0, 0,
                   basis="-398", rpl="398", code="C;Ep")


@pytest.mark.parametrize("fy_name,cross_name", [
    (XYZ_POST, XYZ_PRE), (XYZ_PRE, XYZ_POST), (f"{XYZ_PRE}, {XYZ_POST}", XYZ_PRE)])
def test_option_split_not_taken_for_rename_when_files_name_it_differently(fy_name, cross_name):
    """One file names the split contract by its old strike, the other by its new
    one. That is the split, not a ticker change: the 2 written contracts become 6,
    and all 6 expiring in July match the written lot."""
    rep = compute_tax_report(_split_files(fy_name, cross_name, [XYZ_EXPIRE]),
                             Options(fy_end_year=2026), FX)
    assert not any("could not be matched" in w or "conid: XYZ" in w for w in rep["warnings"])
    (d2,) = rep["d2_open"]
    assert (d2["symbol"], d2["qty"], d2["write_date"]) == ("XYZ 10JUL26 16.67 P", -6, "2026-06-23")
    assert d2["premium_aud"] == pytest.approx(571.51)           # 398 / 0.6964 (23 Jun 2026)

    rep = compute_tax_report(_split_files(fy_name, cross_name, [XYZ_EXPIRE]),
                             Options(fy_end_year=2027), FX)
    assert not rep["unmatched"]
    (lot,) = rep["closed_lots"]
    assert lot["qty"] == 6 and lot["gain_aud"] == 0              # premium was FY2025-26's D2
    assert rep["summary"]["net_capital_gain_18A"] == 0


def test_option_split_with_differing_names_closed_in_the_same_year():
    """Bought back before 30 June: the failed split would have hit the year itself."""
    buyback = trade(OPT, "XYZ 10JUL26 16.67 P", "2026-06-29, 10:00:00", 6, -120, -6,
                    basis="-398", rpl="272", code="C")
    rep = compute_tax_report(_split_files(XYZ_POST, XYZ_PRE, [buyback]),
                             Options(fy_end_year=2026), FX)
    assert not rep["unmatched"]
    (lot,) = rep["closed_lots"]
    assert lot["qty"] == 6
    # 398 / 0.6964 (23 Jun) - 126 / 0.6896 (29 Jun) = 571.51 - 182.71
    assert lot["gain_aud"] == pytest.approx(388.80, abs=0.01)
    assert rep["summary"]["net_capital_gain_18A"] == pytest.approx(388.80, abs=0.01)
    assert rep["reconciliation"]["rows_ok"] == rep["reconciliation"]["rows_checked"] == 1


def test_option_terms_change_without_corporate_action_is_flagged():
    """A strike change with no split in the data to explain it: still unified (as
    before), contract count unchanged, but said plainly — not as a ticker change."""
    rep = run([
        trade(OPT, "ABC 10JUL26 50 P", "2026-06-23, 10:00:00", -2, 400, -2, code="O"),
        trade(OPT, "ABC 10JUL26 16.67 P", "2026-06-29, 10:00:00", 2, -120, -6,
              basis="-398", rpl="272", code="C"),
    ], instrument_info=[instrument(OPT, "ABC   260710P00050000, ABC   260710P00016670",
                                   "900000002", "ABC")])
    assert any(w.startswith("option contract terms changed with no matching corporate action")
               and "ABC 10JUL26 50 P -> ABC 10JUL26 16.67 P" in w for w in rep["warnings"])
    assert not any(w.startswith("symbol change unified via conid: ABC") for w in rep["warnings"])
    (lot,) = rep["closed_lots"]
    assert lot["qty"] == 2 and lot["gain_aud"] == pytest.approx(388.80, abs=0.01)


def test_stock_split_under_old_ticker_reaches_renamed_lots():
    """The split is reported under the ticker of the day; the lots were renamed to
    the current ticker by conid before FIFO, so the split must be renamed too."""
    ca = [["Corporate Actions", "Data", "Stocks", "USD", "2025-10-01", "2025-10-01, 20:25:00",
           "OLDT(US0000000002) Split 2 for 1 (OLDT, EXAMPLE CORP, US0000000002)",
           10, 0, 0, 0, ""]]
    rep = run([
        trade("Stocks", "OLDT", "2025-08-01, 10:00:00", 10, -1000, -1, code="O"),
        trade("Stocks", "NEWT", "2026-02-02, 10:00:00", -20, 1500, -1,
              basis="-1001", rpl="498", code="C"),
    ], corporate_actions=ca,
       instrument_info=[instrument("Stocks", "NEWT, OLDT", "900000003", underlying="NEWT")])
    assert not rep["unmatched"]
    (lot,) = rep["closed_lots"]
    assert (lot["symbol"], lot["qty"]) == ("NEWT", 20)
    # -1001 / 0.6435 (1 Aug 2025) + 1499 / 0.6942 (2 Feb 2026)
    assert lot["gain_aud"] == pytest.approx(603.76, abs=0.01)


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


# Options opened before the uploaded data — e.g. FY2026-27 run with only that
# year's statement while puts written in FY2025-26 were still open at 30 June.
# IBKR's Realized P/L on their closing rows includes the premium, which was a
# D2 gain of the year they were written (s 104-40(2)).

def test_buyback_of_option_written_before_the_data_counts_the_close_leg_only():
    rep = run([trade(OPT, "ABC 19DEC25 50 P", "2025-09-15, 10:00:00", 1, -200, -1,
                     basis="499", rpl="298", code="C")])
    # -201 / 0.6661 (15 Sep 2025); IBKR's +298 would have counted the premium again
    assert rep["summary"]["closed_net"] == pytest.approx(-301.76, abs=0.01)
    assert "only the close leg" in rep["unmatched"][0]["note"]


def test_expiry_of_option_written_before_the_data_counts_nothing():
    rep = run([trade(OPT, "DEF 18JUL25 40 P", "2025-07-19, 16:20:00", 1, 0, 0,
                     basis="300", rpl="300", code="C;Ep")])
    assert rep["summary"]["closed_net"] == 0.0               # was +A$461.40


def test_assignment_of_put_written_before_the_data_folds_premium_and_flags_amendment():
    rep = run([trade(OPT, "XYZ 18JUL25 50 P", "2025-07-19, 16:20:00", 1, 0, 0,
                     basis="299", rpl="0", code="A;C"),
               trade("Stocks", "XYZ", "2025-07-19, 16:20:00", 100, -5000, 0, code="A;O")])
    assert [(c["symbol"], c["cost_native"]) for c in rep["carry_forward"]] == [("XYZ", 4701.0)]
    (flag,) = rep["amendment_flags"]
    assert "written before 2025-07-01" in flag and "USD 299.00" in flag
    assert "s 104-40(5)" in flag
    assert rep["transfers"][0]["option_written"] == "before 2025-07-01"


def test_exercise_of_call_bought_before_the_data_folds_its_cost():
    rep = run([trade(OPT, "LLL 16JAN26 100 C", "2026-01-17, 16:20:00", -1, 0, 0,
                     basis="-1501", rpl="0", code="C;Ex"),
               trade("Stocks", "LLL", "2026-01-17, 16:20:00", 100, -10000, 0, code="Ex;O")])
    assert [(c["symbol"], c["cost_native"]) for c in rep["carry_forward"]] == [("LLL", 11501.0)]
    assert rep["d2_open"] == [] and rep["amendment_flags"] == []
    assert not any(w.startswith("D2 OUTCOME") for w in rep["warnings"])


ROLL = [trade(OPT, "LIT 17JUL25 30 P", "2025-07-02, 10:00:00", 1, -150, -1,
              basis="200", rpl="49", code="C"),
        trade(OPT, "LIT 17JUL25 30 P", "2025-07-02, 10:01:00", 2, -300, -1.3,
              basis="200", rpl="49.35", code="C;O"),
        trade(OPT, "LIT 17JUL25 30 P", "2025-07-02, 10:02:00", -1, 160, -1,
              basis="-150.65", rpl="8.35", code="C")]


def test_position_held_before_the_data_comes_from_the_mark_to_market_summary():
    """-2 written before the data; bought back 1, then 2 in one C;O trade (closes
    1, opens 1 long), then the long sold. Same result as with the history."""
    with_history = run(
        [trade(OPT, "LIT 17JUL25 30 P", "2025-06-10, 10:00:00", -2, 401.3, -1.3, code="O"),
         *ROLL], period="July 1, 2024 - June 30, 2026")
    rep = run(ROLL, prior_positions=[(OPT, "LIT 17JUL25 30 P", -2)])
    assert rep["summary"]["closed_net"] == pytest.approx(
        with_history["summary"]["closed_net"], abs=0.01)
    assert rep["carry_forward"] == []                        # no phantom long
    assert any("already open when the uploaded data starts" in w and "LIT 17JUL25 30 P -2" in w
               for w in rep["warnings"])
    # without the summary the C;O split cannot be known: said, not guessed silently
    bare = run(ROLL)
    assert any("(code C;O) found no position to close" in w for w in bare["warnings"])


def test_shares_held_before_the_data_are_sold_first():
    """FIFO must not match a sale against this year's purchase when an older
    holding, invisible in the trades, was there first."""
    rep = run([trade("Stocks", "XYZ", "2025-08-01, 10:00:00", 100, -5000, -1, code="O"),
               trade("Stocks", "XYZ", "2026-02-02, 10:00:00", -100, 5500, -1,
                     basis="-4000", rpl="1499", code="C")],
              prior_positions=[("Stocks", "XYZ", 100)])
    (lot,) = rep["closed_lots"]
    assert lot["open_date"] == "before 2025-07-01" and lot["discount_eligible"] == ""
    # IBKR's basis 4,000 for the old shares; both legs at 0.6942 (2 Feb 2026)
    assert lot["gain_aud"] == pytest.approx(1499 / 0.6942, abs=0.01)
    assert [(c["symbol"], c["acquired"], c["cost_native"]) for c in rep["carry_forward"]] == \
        [("XYZ", "2025-08-01", 5001.0)]


def test_held_all_year_before_the_data_is_carried_forward_without_a_cost():
    rep = run([], prior_positions=[("Stocks", "HOLD", 50)],
              open_positions=[opos("Stocks", "HOLD", 50, 2500)])
    (row,) = rep["carry_forward"]
    assert row["qty"] == 50 and row["cost_native"] == "" and "not in the uploaded" in row["acquired"]
    assert rep["reconciliation"]["position_diffs"][0]["note"].startswith("held before the")


def test_data_starting_after_1_july_warns_that_a_premium_may_be_missing():
    rep = run([trade(OPT, "ABC 19DEC25 50 P", "2025-12-01, 10:00:00", 1, -200, -1,
                     basis="499", rpl="298", code="C")],
              period="November 11, 2025 - June 30, 2026")
    assert any("possibly after 2025-07-01" in w and "ABC 19DEC25 50 P" in w
               for w in rep["warnings"])


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


def test_binary_upload_rejected_by_content():
    with pytest.raises(ParseError, match="is a binary file, not a CSV"):
        parse_statement(b"Statement,Header\x00\x00\x01\x02", "odd.csv")


def test_utf16_resave_still_parses():
    """Excel's "Unicode text" save is UTF-16 with a BOM — still the same CSV."""
    text = statement("July 1, 2025 - June 30, 2026", "x", trades=list(ENTITY_CASE[1:]))
    st = parse_statement(text.encode("utf-16"), "resaved.csv")
    assert len(st.trades) == 1 and st.account == "U9999999"


def test_truncated_utf16_is_a_parse_error_not_a_traceback():
    # BOM plus an odd number of bytes: the codec raises UnicodeDecodeError,
    # which the CLI would not catch
    with pytest.raises(ParseError, match="broken.csv starts like a UTF-16 text file"):
        parse_statement(b"\xff\xfeS\x00t", "broken.csv")


def test_bad_value_in_statement_names_file_and_line():
    text = statement("July 1, 2025 - June 30, 2026", "x", trades=[],
                     dividends=[["Dividends", "Data", "USD", "2025-11-14", "XYZ", "n/a"]])
    line = text.splitlines().index(next(l for l in text.splitlines()
                                        if l.startswith("Dividends,Data"))) + 1
    with pytest.raises(ParseError, match=f"x.csv: unexpected value on line {line}"):
        parse_statement(text, "x.csv")


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
