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
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


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
class CashReport:
    """Starting and ending cash per currency from one statement's Cash Report,
    used to anchor opening foreign-currency balances and to check that the
    cash flows read from the other sections are complete."""
    source_name: str
    period_start: date | None
    period_end: date | None
    start: dict[str, float]
    end: dict[str, float]
    # The statement this report came from, before merging: its ending cash
    # includes rows dated outside its own period, so it is checked against that
    # file's own rows, not a date window of the merged data.
    owner: "Statement | None" = field(default=None, repr=False, compare=False)


@dataclass
class PositionSnapshot:
    """Positions held when one statement's period starts ("Prior Quantity" in
    its Mark-to-Market Performance Summary), keyed by (asset category, symbol).
    The earliest one tells the engine what was already held before the data."""
    source_name: str
    as_of: date | None
    qty: dict[tuple[str, str], float]
    # The statement it came from, before merging: its own trades are the ones
    # the opening quantities do not include yet.
    owner: "Statement | None" = field(default=None, repr=False, compare=False)


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
    forex_pl: list[CashItem] = field(default_factory=list)   # IBKR's FX P/L, in base currency
    deposits: list[CashItem] = field(default_factory=list)   # deposits (+) / withdrawals (-)
    cash_reports: list[CashReport] = field(default_factory=list)
    position_snapshots: list[PositionSnapshot] = field(default_factory=list)
    conid_symbols: dict[str, list[str]] = field(default_factory=dict)  # conid -> symbols seen
    conid_current: dict[str, str] = field(default_factory=dict)        # conid -> current symbol
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


_EXPORT_HINT = ("This tool reads the CSV export of an IBKR Activity Statement or Realized "
                "Summary. Run the statement again in the IBKR portal with CSV as the format and "
                "upload that file as downloaded (see 'Exporting from IBKR' in the README).")

# Formats people reach for first. Checked by content, so a renamed file is caught too;
# the extension only refines the wording and backs up the check.
_MAGIC = ((b"%PDF-", "a PDF"),
          (b"PK\x03\x04", "a ZIP container (an Excel .xlsx workbook or a .zip archive)"),
          (b"\xd0\xcf\x11\xe0", "a legacy Excel .xls workbook"))
_EXT_KIND = {".pdf": "a PDF", ".xlsx": "an Excel workbook", ".xlsm": "an Excel workbook",
             ".xls": "a legacy Excel .xls workbook", ".zip": "a .zip archive",
             ".html": "an HTML page", ".htm": "an HTML page", ".docx": "a Word document"}


def _decode(content: bytes, source_name: str) -> str:
    """Bytes of an upload -> text, refusing anything that is plainly not a CSV
    with a message that says what the file is and what to upload instead."""
    ext = os.path.splitext(source_name)[1].lower()
    kind = next((k for magic, k in _MAGIC if content.startswith(magic)), None)
    if kind and kind.startswith("a ZIP") and ext in _EXT_KIND:
        kind = _EXT_KIND[ext]
    if kind is None and content[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return content.decode("utf-16")   # re-saved as "Unicode text"; still a CSV
        except UnicodeDecodeError as e:
            raise ParseError(f"{source_name} starts like a UTF-16 text file but cannot be "
                             f"decoded ({e.reason}) — it may be truncated. {_EXPORT_HINT}") from e
    lead = content[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if kind is None and lead.startswith((b"<!doctype html", b"<html")):
        kind = "an HTML page"
    if kind is None and b"\x00" in content[:4096]:
        kind = "a binary file"
    if kind is None:
        kind = _EXT_KIND.get(ext)
    if kind:
        extra = (" If it holds CSV statements, extract them and upload those."
                 if "zip" in kind else "")
        raise ParseError(f"{source_name} is {kind}, not a CSV file.{extra} {_EXPORT_HINT}")
    return content.decode("utf-8-sig", errors="replace")


def parse_statement(content: str | bytes, source_name: str = "statement.csv") -> Statement:
    if isinstance(content, bytes):
        content = _decode(content, source_name)
    reader = csv.reader(io.StringIO(content))
    try:
        return _parse_rows(reader, source_name)
    except csv.Error as e:
        raise ParseError(
            f"{source_name} could not be read as a CSV file (line {reader.line_num}: {e}). "
            "Upload it exactly as IBKR exported it — opening and re-saving it in Excel can "
            f"change its layout. {_EXPORT_HINT}") from e
    except (ValueError, IndexError) as e:
        raise ParseError(
            f"{source_name}: unexpected value on line {reader.line_num} ({e}) — this does not "
            f"read as an unmodified IBKR statement export. {_EXPORT_HINT}") from e


def _parse_rows(reader, source_name: str) -> Statement:
    stmt = Statement(source_name=source_name)
    sections: dict[str, _Section] = {}
    saw_statement_section = False
    cash_balances: dict[str, dict[str, float]] = {"Starting Cash": {}, "Ending Cash": {}}
    saw_mtm, prior_qty = False, {}

    for row in reader:
        if len(row) < 2:
            continue
        sec_name, row_type = row[0], row[1]
        sec = sections.setdefault(sec_name, _Section())
        if row_type == "Header":
            sec.set_header(row)
            # present even when nothing was held: then every prior quantity is 0
            saw_mtm = saw_mtm or sec_name == "Mark-to-Market Performance Summary"
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
                    # the forex subsection names its commission column after the
                    # base currency ("Comm in USD", "Comm in AUD", ...)
                    comm=_num(sec.get(row, "Comm/Fee") or sec.get(row, next(
                        (c for c in sec.cols if c.startswith("Comm in ")), "Comm in USD"))),
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

        elif sec_name == "Deposits & Withdrawals":
            cur = sec.get(row, "Currency")
            ds = sec.get(row, "Settle Date").strip()
            if not ds or cur.startswith("Total"):
                continue
            stmt.deposits.append(CashItem(cur, _d(ds), sec.get(row, "Description"),
                                          _num(sec.get(row, "Amount"))))

        elif sec_name == "Cash Report":
            label, cur = sec.get(row, "Currency Summary"), sec.get(row, "Currency")
            if label in ("Starting Cash", "Ending Cash") and cur and \
                    cur != "Base Currency Summary":
                cash_balances[label][cur] = _num(sec.get(row, "Total"))

        elif sec_name == "Mark-to-Market Performance Summary":
            cat, sym = sec.get(row, "Asset Category"), sec.get(row, "Symbol").strip()
            if cat in ("", "Total") or not sym or sym.startswith("Total"):
                continue
            try:
                prior = _num(sec.get(row, "Prior Quantity"))
            except (ValueError, ParseError):
                continue   # "--" on summary lines
            if prior:
                prior_qty[(cat, occ_to_display(sym))] = prior

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
                # For a renamed instrument IBKR lists both names in Symbol, in no
                # dependable order ("ECHO, SATS" but "SATG, ECHX"); the Underlying
                # column carries the current one. Guarded by the membership test
                # because on an option row Underlying is the underlying ticker,
                # not that contract's own symbol.
                underlying = sec.get(row, "Underlying").strip()
                if underlying in bucket:
                    stmt.conid_current[conid] = underlying

        elif sec_name == "Codes":
            for k_col, m_col in (("Code", "Meaning"), ("Code (Cont.)", "Meaning (Cont.)")):
                k = sec.get(row, k_col).strip()
                if k:
                    stmt.code_legend[k] = sec.get(row, m_col).strip()

    if not saw_statement_section:
        raise ParseError(
            f"{source_name}: no 'Statement' section found — this does not look like an "
            f"IBKR Activity Statement / Realized Summary CSV export. {_EXPORT_HINT}")
    if not stmt.trades and not stmt.dividends and not stmt.interest:
        stmt.warnings.append(f"{source_name}: statement contains no trades or income rows.")
    if cash_balances["Starting Cash"] or cash_balances["Ending Cash"]:
        stmt.cash_reports.append(CashReport(
            source_name, stmt.period_start, stmt.period_end,
            dict(cash_balances["Starting Cash"]), dict(cash_balances["Ending Cash"]), stmt))
    if saw_mtm:
        stmt.position_snapshots.append(PositionSnapshot(source_name, stmt.period_start,
                                                        prior_qty, stmt))
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
        out.conid_current.update(s.conid_current)

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
    for attr in ("dividends", "withholding_tax", "interest", "fees", "borrow_fees", "forex_pl",
                 "deposits"):
        setattr(out, attr, sorted(dedupe(ci_key, [getattr(s, attr) for s in stmts]),
                                  key=lambda x: x.d))
    out.cash_reports = [r for s in stmts for r in s.cash_reports]   # stmts sorted by period
    out.position_snapshots = [p for s in stmts for p in s.position_snapshots]

    # Overlap sanity note
    periods = [(s.period_start, s.period_end, s.source_name) for s in stmts
               if s.period_start and s.period_end]
    covered_to, covered_by = (periods[0][1], periods[0][2]) if periods else (None, None)
    for i in range(1, len(periods)):
        start, end, name = periods[i]
        if start <= periods[i - 1][1]:
            out.warnings.append(
                f"overlapping statement periods: {periods[i-1][2]} and {name} — "
                "duplicate rows were de-duplicated automatically.")
        elif start > covered_to + timedelta(days=1):
            # Trades in the gap are missing: FIFO would match later sales against
            # parcels that may already have been sold in it.
            out.warnings.append(
                f"GAP IN THE UPLOADED DATA — nothing covers {covered_to + timedelta(days=1)} to "
                f"{start - timedelta(days=1)} (between {covered_by} and {name}). Trades in "
                "that period are missing, so parcels sold in it may be matched again later. "
                "Upload the statement covering it.")
        if end > covered_to:
            covered_to, covered_by = end, name
    return out
