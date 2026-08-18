"""Parser for IBKR multi-section CSV statements.

Handles both export flavours (they share one section grammar):
  - Activity Statement (annual/custom period)
  - Realized Summary ("AS_RLZD") — adds a Forex P/L Details section

Every line is `Section,RowType,...`; RowType is Header/Data/SubTotal/Total.
Columns are mapped by header NAME, never by position, because IBKR inserts
and drops columns between statement flavours and account configurations.
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime


class ParseError(Exception):
    pass


@dataclass
class Trade:
    category: str            # "Stocks" | "Equity and Index Options" | "Forex" | ...
    currency: str
    symbol: str
    dt: datetime
    qty: float
    proceeds: float          # signed: sell +, buy -
    comm: float              # signed (normally <= 0)
    ib_basis: float
    ib_rpl: float            # IBKR's own Realized P/L for closing rows
    codes: frozenset[str]    # lifecycle codes: O, C, A, Ep, Ex, P, R, ...

    @property
    def cash(self) -> float:
        """Signed net cashflow of the leg (proceeds + commission)."""
        return self.proceeds + self.comm

    def identity(self) -> tuple:
        """Row identity used to de-duplicate the same trade appearing in
        multiple uploaded statements with overlapping periods."""
        return (self.category, self.currency, self.symbol, self.dt, self.qty,
                round(self.proceeds, 6), round(self.comm, 6), tuple(sorted(self.codes)))


@dataclass
class CorporateAction:
    category: str
    currency: str
    report_date: date | None
    dt: datetime
    description: str
    qty: float
    code: str


@dataclass
class OpenPosition:
    category: str
    currency: str
    symbol: str
    qty: float
    cost_basis: float


@dataclass
class CashItem:
    currency: str
    d: date
    description: str
    amount: float


@dataclass
class Statement:
    source_name: str
    title: str = ""
    account: str = ""
    account_alias: str = ""
    name: str = ""
    base_currency: str = ""
    period_start: date | None = None
    period_end: date | None = None
    when_generated: str = ""
    trades: list[Trade] = field(default_factory=list)
    corporate_actions: list[CorporateAction] = field(default_factory=list)
    open_positions: list[OpenPosition] = field(default_factory=list)
    dividends: list[CashItem] = field(default_factory=list)
    withholding_tax: list[CashItem] = field(default_factory=list)
    interest: list[CashItem] = field(default_factory=list)
    fees: list[CashItem] = field(default_factory=list)
    borrow_fees: list[CashItem] = field(default_factory=list)
    forex_pl: list[CashItem] = field(default_factory=list)   # Div 775 ordinary income items
    conid_symbols: dict[str, list[str]] = field(default_factory=dict)  # conid -> symbols seen
    code_legend: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


_OCC_RE = re.compile(r"^([A-Z0-9.]{1,6})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def occ_to_display(sym: str) -> str:
    """Convert an OCC-style option symbol ("SATS  260710P00105000") to IBKR's
    display form ("SATS 10JUL26 105 P") so instrument-info symbols can be
    matched against trade/position symbols. Non-option symbols pass through."""
    m = _OCC_RE.match(sym)
    if not m:
        return sym
    root, yy, mm, dd, right, strike8 = m.groups()
    month = _MONTHS[int(mm) - 1] if 1 <= int(mm) <= 12 else None
    if month is None:
        return sym
    strike = int(strike8) / 1000.0
    strike_s = f"{strike:g}"
    return f"{root} {dd}{month}{yy} {strike_s} {right}"


def _num(s: str) -> float:
    s = (s or "").replace(",", "").strip()
    if s in ("", "--", "-"):
        return 0.0
    return float(s)


def _dt(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%Y-%m-%d, %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ParseError(f"unrecognised date/time: {s!r}")


def _d(s: str) -> date:
    return _dt(s).date()


_PERIOD_RE = re.compile(r"^\s*(\w+ \d{1,2}, \d{4})\s*-\s*(\w+ \d{1,2}, \d{4})\s*$")


def _parse_period(v: str) -> tuple[date | None, date | None]:
    m = _PERIOD_RE.match(v)
    if m:
        f = "%B %d, %Y"
        return datetime.strptime(m.group(1), f).date(), datetime.strptime(m.group(2), f).date()
    try:  # single-day statement: "July 1, 2025"
        d = datetime.strptime(v.strip(), "%B %d, %Y").date()
        return d, d
    except ValueError:
        return None, None


class _Section:
    """Tracks the current header row of a section so Data rows can be
    addressed by column name. IBKR repeats Header rows inside one section
    when the column set changes (e.g. Trades: stocks vs forex subsection)."""

    def __init__(self):
        self.cols: dict[str, int] = {}

    def set_header(self, row: list[str]) -> None:
        self.cols = {}
        for i, h in enumerate(row[2:], start=2):
            h = h.strip()
            if h and h not in self.cols:
                self.cols[h] = i

    def get(self, row: list[str], name: str, default: str = "") -> str:
        i = self.cols.get(name)
        if i is None or i >= len(row):
            return default
        return row[i]

    def has(self, name: str) -> bool:
        return name in self.cols


def parse_statement(content: str | bytes, source_name: str = "statement.csv") -> Statement:
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")
    stmt = Statement(source_name=source_name)
    sections: dict[str, _Section] = {}
    saw_statement_section = False

    for row in csv.reader(io.StringIO(content)):
        if len(row) < 2:
            continue
        sec_name, row_type = row[0], row[1]
        sec = sections.setdefault(sec_name, _Section())
        if row_type == "Header":
            sec.set_header(row)
            continue
        if row_type != "Data":
            continue  # SubTotal / Total / Notes

        if sec_name == "Statement":
            saw_statement_section = True
            k, v = sec.get(row, "Field Name"), sec.get(row, "Field Value")
            if k == "Title":
                stmt.title = v
            elif k == "Period":
                stmt.period_start, stmt.period_end = _parse_period(v)
                if stmt.period_start is None:
                    stmt.warnings.append(f"{source_name}: could not parse statement period {v!r}")
            elif k == "WhenGenerated":
                stmt.when_generated = v

        elif sec_name == "Account Information":
            k, v = sec.get(row, "Field Name"), sec.get(row, "Field Value")
            if k == "Account":
                stmt.account = v.split()[0] if v else v
            elif k == "Name":
                stmt.name = v
            elif k == "Account Alias":
                stmt.account_alias = v
            elif k == "Base Currency":
                stmt.base_currency = v

        elif sec_name == "Trades":
            if sec.get(row, "DataDiscriminator") != "Order":
                continue  # ClosedLot detail / summaries — orders are canonical
            cat = sec.get(row, "Asset Category")
            sym = sec.get(row, "Symbol").strip()
            if not sym:
                continue
            code_s = sec.get(row, "Code")
            try:
                t = Trade(
                    category=cat,
                    currency=sec.get(row, "Currency"),
                    symbol=sym,
                    dt=_dt(sec.get(row, "Date/Time")),
                    qty=_num(sec.get(row, "Quantity")),
                    proceeds=_num(sec.get(row, "Proceeds")),
                    comm=_num(sec.get(row, "Comm/Fee") or sec.get(row, "Comm in USD")),
                    ib_basis=_num(sec.get(row, "Basis")),
                    ib_rpl=_num(sec.get(row, "Realized P/L")),
                    codes=frozenset(c for c in code_s.split(";") if c),
                )
            except (ValueError, ParseError) as e:
                stmt.warnings.append(f"{source_name}: skipped unparseable trade row for {sym}: {e}")
                continue
            stmt.trades.append(t)

        elif sec_name == "Corporate Actions":
            cat = sec.get(row, "Asset Category")
            if cat in ("", "Total"):
                continue
            rd = sec.get(row, "Report Date").strip()
            stmt.corporate_actions.append(CorporateAction(
                category=cat,
                currency=sec.get(row, "Currency"),
                report_date=_d(rd) if rd else None,
                dt=_dt(sec.get(row, "Date/Time")),
                description=sec.get(row, "Description"),
                qty=_num(sec.get(row, "Quantity")),
                code=sec.get(row, "Code"),
            ))

        elif sec_name == "Open Positions":
            if sec.get(row, "DataDiscriminator") != "Summary":
                continue
            stmt.open_positions.append(OpenPosition(
                category=sec.get(row, "Asset Category"),
                currency=sec.get(row, "Currency"),
                symbol=sec.get(row, "Symbol").strip(),
                qty=_num(sec.get(row, "Quantity")),
                cost_basis=_num(sec.get(row, "Cost Basis")),
            ))

        elif sec_name in ("Dividends", "Withholding Tax", "Interest"):
            cur = sec.get(row, "Currency")
            ds = sec.get(row, "Date").strip()
            if not ds or cur.startswith("Total"):
                continue
            item = CashItem(cur, _d(ds), sec.get(row, "Description"),
                            _num(sec.get(row, "Amount")))
            {"Dividends": stmt.dividends,
             "Withholding Tax": stmt.withholding_tax,
             "Interest": stmt.interest}[sec_name].append(item)

        elif sec_name == "Fees":
            cur = sec.get(row, "Currency")
            ds = sec.get(row, "Date").strip()
            if not ds or cur.startswith("Total"):
                continue
            stmt.fees.append(CashItem(cur, _d(ds), sec.get(row, "Description"),
                                      _num(sec.get(row, "Amount"))))

        elif sec_name == "Borrow Fee Details":
            cur = sec.get(row, "Currency")
            ds = sec.get(row, "Value Date").strip()
            if not ds or cur.startswith("Total"):
                continue
            stmt.borrow_fees.append(CashItem(
                cur, _d(ds), sec.get(row, "Symbol"),
                _num(sec.get(row, "Borrow Fee") or sec.get(row, "Amount"))))

        elif sec_name == "Forex P/L Details":
            if sec.get(row, "Asset Category") != "Forex":
                continue
            ds = sec.get(row, "Date/Time").strip()
            # Realized P/L is reported in the account base currency
            # (column literally named e.g. "Realized P/L in USD").
            amount_col = next((c for c in sec.cols if c.startswith("Realized P/L")), None)
            if amount_col is None:
                continue
            stmt.forex_pl.append(CashItem(
                sec.get(row, "Currency") or "USD",
                _d(ds) if ds else (stmt.period_end or date.today()),
                f"{sec.get(row, 'FX Currency')}: {sec.get(row, 'Description')}",
                _num(sec.get(row, amount_col)),
            ))

        elif sec_name == "Financial Instrument Information":
            conid = sec.get(row, "Conid").strip()
            syms = [occ_to_display(s.strip()) for s in sec.get(row, "Symbol").split(",")
                    if s.strip()]
            if conid and syms:
                bucket = stmt.conid_symbols.setdefault(conid, [])
                for s in syms:
                    if s not in bucket:
                        bucket.append(s)

        elif sec_name == "Codes":
            for k_col, m_col in (("Code", "Meaning"), ("Code (Cont.)", "Meaning (Cont.)")):
                k = sec.get(row, k_col).strip()
                if k:
                    stmt.code_legend[k] = sec.get(row, m_col).strip()

    if not saw_statement_section:
        raise ParseError(
            f"{source_name}: no 'Statement' section found — this does not look like an "
            "IBKR Activity Statement / Realized Summary CSV export.")
    if not stmt.trades and not stmt.dividends and not stmt.interest:
        stmt.warnings.append(f"{source_name}: statement contains no trades or income rows.")
    stmt.trades.sort(key=lambda t: t.dt)  # csv.reader preserves file order for ties
    return stmt


def merge_statements(stmts: list[Statement]) -> Statement:
    """Merge several statements (e.g. consecutive-year activity statements, or
    an activity statement plus its realized-summary sibling) into one, de-duplicating
    trades and cash items that appear in more than one file's period.

    De-dup rule: identical rows are counted per file, and the merged output keeps
    the MAX count across files (so a genuine same-second duplicate fill inside one
    file survives, while a period-overlap duplicate across files is removed).
    """
    if not stmts:
        raise ParseError("no statements to merge")
    accounts = {s.account for s in stmts if s.account}
    if len(accounts) > 1:
        raise ParseError(f"statements belong to different accounts: {sorted(accounts)} — "
                         "run one account at a time.")
    stmts = sorted(stmts, key=lambda s: (s.period_start or date.min, s.period_end or date.min))
    out = Statement(source_name=", ".join(s.source_name for s in stmts))
    base = stmts[-1]  # latest period wins for point-in-time sections
    out.title, out.account, out.name = base.title, base.account, base.name
    out.account_alias, out.base_currency = base.account_alias, base.base_currency
    out.when_generated = base.when_generated
    out.period_start = min((s.period_start for s in stmts if s.period_start), default=None)
    out.period_end = max((s.period_end for s in stmts if s.period_end), default=None)
    out.open_positions = base.open_positions          # snapshot at latest period end
    out.code_legend = {k: v for s in stmts for k, v in s.code_legend.items()}
    for s in stmts:
        out.warnings.extend(s.warnings)
        for conid, syms in s.conid_symbols.items():
            bucket = out.conid_symbols.setdefault(conid, [])
            for sym in syms:
                if sym not in bucket:
                    bucket.append(sym)

    def dedupe(key_of, lists: list[list]) -> list:
        counts: list[Counter] = [Counter(key_of(x) for x in lst) for lst in lists]
        want = Counter()
        for c in counts:
            for k, n in c.items():
                want[k] = max(want[k], n)
        result, taken = [], Counter()
        for lst in lists:
            for x in lst:
                k = key_of(x)
                if taken[k] < want[k]:
                    result.append(x)
                    taken[k] += 1
        return result

    ci_key = lambda x: (x.currency, x.d, x.description, round(x.amount, 6))
    out.trades = sorted(dedupe(lambda t: t.identity(), [s.trades for s in stmts]),
                        key=lambda t: t.dt)
    out.corporate_actions = dedupe(
        lambda a: (a.category, a.dt, a.description, a.qty),
        [s.corporate_actions for s in stmts])
    out.corporate_actions.sort(key=lambda a: a.dt)
    for attr in ("dividends", "withholding_tax", "interest", "fees", "borrow_fees", "forex_pl"):
        setattr(out, attr, sorted(dedupe(ci_key, [getattr(s, attr) for s in stmts]),
                                  key=lambda x: x.d))

    # Overlap sanity note
    periods = [(s.period_start, s.period_end, s.source_name) for s in stmts
               if s.period_start and s.period_end]
    for i in range(1, len(periods)):
        if periods[i][0] <= periods[i - 1][1]:
            out.warnings.append(
                f"overlapping statement periods: {periods[i-1][2]} and {periods[i][2]} — "
                "duplicate rows were de-duplicated automatically.")
    return out
