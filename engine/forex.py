"""Div 775 forex realisations on foreign-currency cash, first-in first-out.

An Australian resident's functional currency is AUD, so every non-AUD cash
balance in the account is a right to receive foreign currency (a credit
balance) or an obligation to pay it (a debit balance), whatever IBKR's base
currency is:

- Spending, converting or withdrawing a credit balance is forex realisation
  event 2 (s 775-45: ceasing to have a right to receive foreign currency). The
  gain or loss is the AUD value of the units at that time less their AUD forex
  cost base.
- Paying back a debit balance is FRE 4 (s 775-55: ceasing to have an obligation
  to pay foreign currency). The gain or loss is the AUD proceeds of assuming
  the obligation less the AUD value paid.
- Units are matched first-in first-out (s 775-145(1)), exactly as in the ATO's
  worked example "Use of first-in first-out method for fungible assets, rights
  and obligations" (tests/test_forex.py reproduces it to the cent).

The cash flows come from the statements themselves: trades (including currency
conversions), dividends, withholding tax, interest, fees, deposits and
withdrawals. They are checked against each statement's Cash Report. AUD values
use the RBA rate for the day, except for a conversion to or from AUD, where
the AUD side of the trade is the exact value.

Not applied, because they depend on elections and facts outside the
statements: the $250,000 balance election (s 775-230), the retranslation
election (s 775-270), and the weighted-average alternative to FIFO.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date

from .fx import RbaRates
from .parser import Statement

EPS = 1e-9


def _r2(x: float) -> float:
    return round(x, 2) + 0.0      # + 0.0 turns -0.0 into 0.0
RECONCILE_TOLERANCE = 10.0   # foreign units per statement; GST is not itemised anywhere


@dataclass
class Flow:
    d: date
    ccy: str
    amount: float            # foreign-currency units: + received, - paid
    aud: float | None        # exact AUD value of |amount|, when the trade states it
    source: str
    seq: int = 0


@dataclass
class _Lot:
    units: float             # + credit balance (right), - debit balance (obligation)
    aud_per_unit: float      # forex cost base (credit) or proceeds of assuming (debit)
    d: date


def collect_flows(stmt: Statement) -> list[Flow]:
    """Every movement of non-AUD cash the statements show."""
    flows: list[Flow] = []
    base = stmt.base_currency
    for t in stmt.trades:
        when = t.dt.date()
        if t.category == "Forex":
            pair = t.symbol.split(".")
            if len(pair) != 2:
                continue
            first, second = pair   # quantity is in the first currency, proceeds in the second
            if first != "AUD" and t.qty:
                flows.append(Flow(when, first, t.qty,
                                  abs(t.proceeds) if second == "AUD" else None,
                                  f"conversion {t.symbol}"))
            if second != "AUD" and t.proceeds:
                flows.append(Flow(when, second, t.proceeds,
                                  abs(t.qty) if first == "AUD" else None,
                                  f"conversion {t.symbol}"))
            # the forex subsection's commission is in the base currency ("Comm in USD")
            if t.comm and base and base != "AUD":
                flows.append(Flow(when, base, t.comm, None,
                                  f"commission on conversion {t.symbol}"))
        elif t.currency and t.currency != "AUD" and t.cash:
            flows.append(Flow(when, t.currency, t.cash, None,
                              f"{'sell' if t.qty < 0 else 'buy'} {t.symbol}"))
    for label, items in (("dividend", stmt.dividends), ("withholding tax", stmt.withholding_tax),
                         ("interest", stmt.interest), ("fee", stmt.fees),
                         ("deposit/withdrawal", stmt.deposits)):
        for it in items:
            if it.currency and it.currency != "AUD" and it.amount:
                flows.append(Flow(it.d, it.currency, it.amount, None,
                                  f"{label}: {it.description}"))
    for i, f in enumerate(flows):
        f.seq = i
    return flows


def opening_flows(stmt: Statement, flows: list[Flow]) -> tuple[list[Flow], list[str]]:
    """Opening foreign-currency balances from the earliest Cash Report.

    The files cannot say when that cash was acquired, so its cost is taken at
    the RBA rate on the first day — worth a warning when it is not zero."""
    notes: list[str] = []
    reports = [r for r in stmt.cash_reports if r.period_start]
    if not reports:
        return [], notes
    first = min(reports, key=lambda r: r.period_start)
    out = []
    for ccy, amount in sorted(first.start.items()):
        if ccy == "AUD" or abs(amount) < 0.005:
            continue
        if any(f.ccy == ccy and f.d < first.period_start for f in flows):
            notes.append(f"{ccy}: files without a Cash Report start before "
                         f"{first.period_start}; the opening balance there is not known and "
                         "was taken as zero.")
            continue
        out.append(Flow(first.period_start, ccy, amount, None,
                        "opening balance (acquisition dates before the uploaded files)", -1))
        notes.append(f"{ccy}: the opening balance of {amount:,.2f} on {first.period_start} "
                     "comes from before the uploaded files, so its AUD cost is taken at the RBA "
                     "rate on that day. Upload the earlier statements to replace this "
                     "assumption with the actual history.")
    return out, notes


def _order(f: Flow):
    # An opening balance first; then receipts before payments on the same day:
    # one RBA rate applies to the whole day, and IBKR's automatic conversions
    # (code AFx) settle a purchase the moment it is made, which would otherwise
    # read as a brief overdraft.
    return (f.d, -1 if f.seq < 0 else (0 if f.amount > 0 else 1), f.seq)


def realise(flows: list[Flow], fx: RbaRates, until: date | None = None
            ) -> tuple[list[dict], dict[str, list[_Lot]]]:
    """Run the FIFO pool per currency. Returns one row per flow that realised
    something (date order) and the lots left open at the end."""
    rows: list[dict] = []
    open_lots: dict[str, list[_Lot]] = {}
    by_ccy: dict[str, list[Flow]] = defaultdict(list)
    for f in flows:
        if until is None or f.d <= until:
            by_ccy[f.ccy].append(f)
    for ccy, items in sorted(by_ccy.items()):
        lots: deque[_Lot] = deque()
        for f in sorted(items, key=_order):
            size = abs(f.amount)

            def aud_of(units: float, f=f, size=size) -> float:
                if f.aud is not None:
                    return f.aud * units / size
                return fx.to_aud(units, f.ccy, f.d)[0]

            closing_sign = -1 if f.amount > 0 else 1      # receipts close debit lots
            remaining = size
            value = cost = done = 0.0
            first_d = last_d = None
            while remaining > EPS and lots and lots[0].units * closing_sign > 0:
                lot = lots[0]
                take = min(remaining, abs(lot.units))
                value += aud_of(take)
                cost += take * lot.aud_per_unit
                done += take
                first_d = first_d or lot.d
                last_d = lot.d
                lot.units -= take * closing_sign
                remaining -= take
                if abs(lot.units) <= EPS:
                    lots.popleft()
            if done > EPS:
                # FRE 2: value received - forex cost base; FRE 4: proceeds of
                # assuming the obligation - value paid
                gain = value - cost if f.amount < 0 else cost - value
                rows.append(dict(
                    date=str(f.d), currency=ccy,
                    event="FRE 2 (s 775-45)" if f.amount < 0 else "FRE 4 (s 775-55)",
                    units=_r2(done), aud_value=_r2(value), aud_cost=_r2(cost),
                    gain_aud=_r2(gain),
                    acquired=str(first_d) if first_d == last_d else f"{first_d} to {last_d}",
                    source=f.source))
            if remaining > EPS:
                sign = 1 if f.amount > 0 else -1
                lots.append(_Lot(sign * remaining, aud_of(remaining) / remaining, f.d))
        open_lots[ccy] = list(lots)
    rows.sort(key=lambda r: r["date"])
    return rows, open_lots


def reconcile(stmt: Statement) -> list[dict]:
    """Starting cash + the flows read from each statement vs its Ending Cash,
    per statement and foreign currency. Each Cash Report is checked against its
    own file's rows: IBKR's ending cash includes rows dated outside the period,
    so a date window of the merged data would not add up."""
    out = []
    for rep in stmt.cash_reports:
        if not (rep.period_start and rep.period_end) or rep.owner is None:
            continue
        own = collect_flows(rep.owner)
        for ccy in sorted((set(rep.start) | set(rep.end)) - {"AUD"}):
            moved = sum(f.amount for f in own if f.ccy == ccy)
            start, end = rep.start.get(ccy, 0.0), rep.end.get(ccy, 0.0)
            out.append(dict(source=rep.source_name, currency=ccy,
                            period=f"{rep.period_start} to {rep.period_end}",
                            start=round(start, 2), flows=round(moved, 2), end=round(end, 2),
                            gap=round(end - start - moved, 2)))
    return out


def compute(stmt: Statement, fx: RbaRates, fy_start: date, fy_end: date) -> dict:
    """Div 775 on the account's foreign-currency cash for one financial year."""
    flows = collect_flows(stmt)
    opening, notes = opening_flows(stmt, flows)
    rows, open_lots = realise(opening + flows, fx, until=fy_end)
    fy_rows = [r for r in rows if fy_start <= date.fromisoformat(r["date"]) <= fy_end]
    # the total is the sum of the schedule's rounded rows, so the schedule foots
    total = sum(r["gain_aud"] for r in fy_rows)
    closing = {ccy: dict(units=round(sum(l.units for l in lots), 2),
                         aud_cost=round(sum(l.units * l.aud_per_unit for l in lots), 2))
               for ccy, lots in open_lots.items() if lots}
    return dict(rows=fy_rows, total_aud=round(total, 2), notes=notes,
                reconciliation=reconcile(stmt), closing=closing,
                gains_aud=round(sum(r["gain_aud"] for r in fy_rows if r["gain_aud"] > 0), 2),
                losses_aud=round(sum(r["gain_aud"] for r in fy_rows if r["gain_aud"] < 0), 2))
