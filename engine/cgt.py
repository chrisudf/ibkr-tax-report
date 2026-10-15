"""Australian CGT engine for IBKR statements.

Methodology (the same rules are described for users in README.md, "What it
computes", and on the PDF's methodology page):

- FIFO lot matching per (asset category, symbol). IBKR's own default is also
  FIFO, which lets us reconcile per-row against their Realized P/L column.
- Signed-cash convention: every leg's cash = proceeds + commission (buys
  negative). Realized gain of a matched parcel = open cash + close cash,
  correct for both long and short parcels.
- Each leg is translated to AUD at the RBA daily rate for its own transaction
  date (s 960-50 ITAA 1997) — cost base at acquisition-date rate, capital
  proceeds at CGT-event-date rate.
- Written (sold-to-open) options are CGT event D2 at the date of grant
  (s 104-40(2)); D2 gains never qualify for the CGT discount (s 115-25(3)).
  Written options still open at the end of the financial year are therefore
  taxable in that year ("strict" view). A "deferred / closed-basis" alternative
  total is also reported for comparison. When the uploaded data stops before
  their fate is known, the strict figure stands but a warning names them.
- If a written option is later exercised/assigned, the D2 gain is disregarded
  (s 104-40(5)) and the premium is folded into the underlying share parcel
  (ss 134-1, 116-65): reduces cost base of shares bought under a put, increases
  capital proceeds of shares delivered under a call — the premium translated at
  the rate on the day the option was written. Assignments that cancel a D2 gain
  returned in an earlier year raise an amendment flag (no time limit for this
  amendment: s 170(10AA) item 50 ITAA 1936).
- Long options that expire worthless are a capital loss at expiry (CGT event
  C2, s 104-25).
- The 12-month CGT discount applies only to long parcels acquired at least 12
  months before the CGT event (s 115-25(1)), at the entity's rate (individual/
  trust 50%, complying super fund 33 1/3%, company nil).
- Corporate actions (splits) are parsed from the statement's Corporate Actions
  section and applied chronologically; anything unrecognised is surfaced as a
  warning for manual review, never silently guessed.

The engine is record-keeping/calculation only. It does not decide whether the
taxpayer holds on capital or revenue account, and it makes no recommendations.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

from . import forex
from .fx import RbaRates, FxError
from .parser import Statement, Trade

TOOL_VERSION = "0.1.0"

CGT_CATEGORIES = ("Stocks", "Equity and Index Options")
ENTITY_DISCOUNT = {"individual": 0.5, "trust": 0.5, "smsf": 1.0 / 3.0, "company": 0.0}

# Where each figure goes on each entity's return (ATO CGT schedule 2026
# instructions; SMSF annual return 2026). The summary keys keep the individual
# return's names (net_capital_gain_18A, ...) for every entity.
RETURN_LABELS = {
    "individual": dict(gains="label 18H", net="label 18A", carried="label 18V",
                       gains_short="18H", net_short="18A", carried_short="18V"),
    "smsf": dict(gains="CGT schedule item 1 label J", net="SMSF annual return item 11 label A",
                 carried="SMSF annual return item 14 label V",
                 gains_short="CGT sch. 1J", net_short="SAR 11A", carried_short="SAR 14V"),
    "trust": dict(gains="CGT schedule item 1 label J", net="trust return item 21",
                  carried="trust return item 27",
                  gains_short="CGT sch. 1J", net_short="trust item 21",
                  carried_short="trust item 27"),
    "company": dict(gains="CGT schedule item 1 label J", net="company return item 7",
                    carried="company return item 13",
                    gains_short="CGT sch. 1J", net_short="company item 7",
                    carried_short="company item 13"),
}
# From 1 July 2027 individuals and trusts get no CGT discount (s 115-100(aa),
# (ab) as amended by Act No 49 of 2026); assets they hold at 30 June 2027 are
# deemed sold and reacquired at market value (s 112-155), and later growth is
# indexed (s 110-36(1A)). Not modelled: those years are refused.
DISCOUNT_ENDS = date(2027, 7, 1)
QTY_EPS = 1e-9


class EngineError(Exception):
    pass


def fy_window(fy_end_year: int) -> tuple[date, date]:
    """Australian financial year ending 30 June of fy_end_year."""
    return date(fy_end_year - 1, 7, 1), date(fy_end_year, 6, 30)


def fy_label(fy_end_year: int) -> str:
    return f"FY{fy_end_year - 1}-{str(fy_end_year)[2:]}"


def fy_of(d: date) -> int:
    return d.year + 1 if d.month >= 7 else d.year


def detect_fy(stmt) -> int | None:
    """Default financial year to report: the latest one that has ENDED and that the
    statement actually covers.

    Keying off the statement's end date alone lands on the wrong year whenever the
    export runs past 30 June — which is the normal case, since statements are pulled
    after year end. A year that has not finished cannot be lodged, so prefer the
    latest finished year with activity in it. Day-count majority is not used: the
    recommended workflow uploads several consecutive years for FIFO history, and
    whole years tie at 365 days.
    """
    end = stmt.period_end
    if end is None:
        return None
    years = {fy_of(t.dt.date()) for t in stmt.trades}
    for items in (stmt.dividends, stmt.withholding_tax, stmt.interest,
                  stmt.fees, stmt.borrow_fees, stmt.forex_pl):
        years |= {fy_of(it.d) for it in items}
    if not years:
        return fy_of(end)
    finished = {y for y in years if fy_window(y)[1] <= end}
    return max(finished) if finished else max(years)


def discount_from(open_d: date) -> date:
    """The first day a CGT event on an asset acquired on open_d can be
    discounted: it must be acquired at least 12 months before the event
    (s 115-25(1)). Neither the day of acquisition nor the event day counts, so
    the 12 months run from the next day and end the day before its anniversary
    (Acts Interpretation Act 1901 s 2G); the event must fall on or after that
    anniversary. Bought on 28 February 2023: 1 March 2024, not 29 February."""
    start = open_d + timedelta(days=1)
    try:
        return start.replace(year=start.year + 1)
    except ValueError:  # starts on 29 Feb
        return date(start.year + 1, 3, 1)


def held_12_months(open_d: date, close_d: date) -> bool:
    """s 115-25(1): asset acquired at least 12 months before the CGT event."""
    return close_d >= discount_from(open_d)


_OPT_RE = re.compile(r"^(?P<under>[A-Z0-9.]+)\s+(?P<exp>\d{2}[A-Z]{3}\d{2})\s+"
                     r"(?P<strike>\d+(?:\.\d+)?)\s+(?P<right>[CP])$")


def parse_option_symbol(sym: str):
    m = _OPT_RE.match(sym.strip())
    if not m:
        return None
    return m.group("under"), m.group("exp"), float(m.group("strike")), m.group("right")


def option_expiry(sym: str) -> date | None:
    p = parse_option_symbol(sym)
    if not p:
        return None
    try:
        return datetime.strptime(p[1], "%d%b%y").date()
    except ValueError:
        return None


def underlying_of(t: Trade) -> str:
    if t.category == "Equity and Index Options":
        p = parse_option_symbol(t.symbol)
        return p[0] if p else t.symbol.split()[0]
    return t.symbol


# --------------------------------------------------------------------------
# Corporate actions
# --------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"Split (\d+) for (\d+)")
_PAREN_RE = re.compile(r"\(([^()]*)\)\s*$")


@dataclass
class SplitEvent:
    dt: datetime
    category: str
    ratio: float                 # new shares per old share
    old_symbol: str | None       # None => resolve against open books (options)
    new_symbol: str | None
    description: str


def plan_corporate_actions(stmt: Statement) -> tuple[list[SplitEvent], list[str]]:
    events, warnings = [], []
    for ca in stmt.corporate_actions:
        m = _SPLIT_RE.search(ca.description)
        if not m:
            warnings.append(
                f"Corporate action NOT auto-processed — review manually and adjust the "
                f"input if it affects lots: [{ca.dt.date()}] {ca.description}")
            continue
        ratio = int(m.group(1)) / int(m.group(2))
        # Leading token before '(' is the acting symbol: "NFLX(US641...) Split 10 for 1 (...)"
        head = ca.description.split("(", 1)[0].strip()
        if ca.category == "Stocks":
            events.append(SplitEvent(ca.dt, ca.category, ratio, head or None, head or None,
                                     ca.description))
        else:
            # Option splits rename the contract; new symbol is in the trailing parens:
            # "(NVDL  260717P00026670, NVDL 17JUL26 26.67 P, )"
            new_sym = None
            pm = _PAREN_RE.search(ca.description)
            if pm:
                parts = [p.strip() for p in pm.group(1).split(",") if p.strip()]
                for p in parts:
                    if parse_option_symbol(p):
                        new_sym = p
                        break
            if new_sym is None:
                warnings.append(
                    f"Option corporate action could not be resolved automatically — "
                    f"review manually: [{ca.dt.date()}] {ca.description}")
                continue
            events.append(SplitEvent(ca.dt, ca.category, ratio, None, new_sym,
                                     ca.description))
    events.sort(key=lambda e: e.dt)
    return events, warnings


# --------------------------------------------------------------------------
# FIFO engine
# --------------------------------------------------------------------------

@dataclass
class Lot:
    qty: float                # signed
    cash: float               # signed native-currency total for the open leg
    dt: datetime              # acquisition/grant datetime
    currency: str
    # AUD to add to the open leg's translation: a premium folded into the cash
    # is translated at its own (grant) date, not this lot's (s 960-50(6) item 5).
    aud_adj: float = 0.0
    # Held before the uploaded data starts (from IBKR's Mark-to-Market summary):
    # quantity known; opening cash and date are not — the opening cash of the
    # units each closing row takes comes from that row's IBKR Basis instead.
    pre_data: bool = False


@dataclass
class Match:
    category: str
    symbol: str
    currency: str
    qty: float                # absolute quantity closed
    open_dt: datetime
    close_dt: datetime
    open_cash: float          # allocation of the opening leg (signed)
    close_cash: float         # allocation of the closing leg (signed)
    short: bool               # True when the OPEN was a sell (written/short)
    via_expiry: bool = False
    aud_gain: float = 0.0
    open_fx: float = 0.0
    close_fx: float = 0.0
    pre_data: bool = False    # opened before the uploaded data (open_cash from IBKR's Basis)
    basis_unknown: bool = False
    open_aud_adj: float = 0.0     # see Lot.aud_adj
    close_aud_adj: float = 0.0

    @property
    def gain_native(self) -> float:
        return self.open_cash + self.close_cash


@dataclass
class Transfer:
    """Premium/cost folded from an exercised or assigned option into the
    matching share trade (ss 134-1 / 116-65 / 104-40(5))."""
    option_symbol: str
    option_qty: float
    kind: str                 # "assignment" | "exercise"
    dt: datetime
    cash: float               # option-leg open cash allocated (signed)
    close_cash: float         # closing-row cash allocated (usually 0 for A/Ex)
    currency: str
    option_open_dt: datetime
    stock_trade_idx: int | None = None
    stock_symbol: str = ""
    pre_data: bool = False    # option opened before the uploaded data (cash from IBKR's Basis)


@dataclass
class UnmatchedClose:
    trade: Trade
    qty_unmatched: float
    close_cash_alloc: float
    ib_rpl: float
    resolved: bool            # True when we could fall back to IBKR's Realized P/L
    aud_gain: float = 0.0
    # Buying back (or expiry of) an option written before the data: its premium
    # was a D2 gain of the year it was written, so only this close leg counts.
    written: bool = False


@dataclass
class FifoResult:
    matches: list[Match] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)
    unmatched: list[UnmatchedClose] = field(default_factory=list)
    books: dict = field(default_factory=dict)     # (cat, sym) -> [Lot]
    books_at: dict = field(default_factory=dict)  # copy of books at the end of snapshot_at
    row_checks: list = field(default_factory=list)  # (trade, my_rpl)
    warnings: list[str] = field(default_factory=list)


def _is_presplit_contract(sym: str, ev: SplitEvent) -> bool:
    """True when option `sym` is the contract that option split `ev` turns into
    ev.new_symbol: same underlying, expiry and right, strike = new strike x
    ratio (IBKR prints the new strike to the cent: 80 / 3 shows as 26.67)."""
    new, p = parse_option_symbol(ev.new_symbol or ""), parse_option_symbol(sym)
    return bool(new and p) and p[0] == new[0] and p[1] == new[1] and p[3] == new[3] \
        and abs(p[2] - new[2] * ev.ratio) <= 0.05 * ev.ratio


def _apply_split(books: dict, ev: SplitEvent, warnings: list[str]) -> None:
    if ev.category == "Stocks":
        key = (ev.category, ev.old_symbol)
        lots = books.pop(key, [])
        for lot in lots:
            lot.qty *= ev.ratio
        if lots:
            books.setdefault((ev.category, ev.new_symbol), []).extend(lots)
        return
    # Options: find the pre-split contract whose strike ~= new strike * ratio
    parsed = parse_option_symbol(ev.new_symbol)
    if not parsed:
        warnings.append(f"option split target unparseable: {ev.new_symbol}")
        return
    candidates = [(cat, sym) for (cat, sym), lots in books.items()
                  if cat == ev.category and lots and _is_presplit_contract(sym, ev)]
    if len(candidates) != 1:
        warnings.append(
            f"option split could not be matched to exactly one open contract "
            f"(candidates: {[c[1] for c in candidates]}) — review manually: {ev.description}")
        return
    lots = books.pop(candidates[0])
    for lot in lots:
        lot.qty *= ev.ratio
    books.setdefault((ev.category, ev.new_symbol), []).extend(lots)


def run_fifo(trades: list[Trade], splits: list[SplitEvent],
             fold_adjustments: dict[int, float] | None = None,
             suppress_fold_matches: bool = True,
             initial_lots: dict | None = None,
             data_start: datetime | None = None,
             aud_adjustments: dict[int, float] | None = None,
             snapshot_at: date | None = None) -> FifoResult:
    """One chronological FIFO pass.

    fold_adjustments: trade index -> cash to add to that (share) trade's leg,
    computed in a previous pass from assignment/exercise transfers.
    initial_lots: (category, symbol) -> Lot held before the uploaded data
    (copied, so each pass starts from the same books).
    data_start: when the uploaded data starts — dates an option exercised or
    assigned with no opening lot at all, which was opened before then.
    aud_adjustments: trade index -> AUD to add to that trade's leg on top of
    the translation of its cash (premiums folded at their own date).
    snapshot_at: also keep the books as at the end of that day in
    res.books_at — every trade and split dated on or before it, nothing later
    (res.books runs on to the end of the data).
    """
    res = FifoResult()
    books: dict = defaultdict(list)
    for key, lot in (initial_lots or {}).items():
        books[key].append(replace(lot))
    res.books = books
    fold_adjustments = fold_adjustments or {}
    aud_adjustments = aud_adjustments or {}
    split_i = 0
    snapped = snapshot_at is None

    def snapshot() -> None:
        nonlocal split_i, snapped
        while split_i < len(splits) and splits[split_i].dt.date() <= snapshot_at:
            _apply_split(books, splits[split_i], res.warnings)
            split_i += 1
        # later trades consume lots in place: copy them
        res.books_at = {k: [replace(lot) for lot in lots] for k, lots in books.items() if lots}
        snapped = True

    for idx, t in enumerate(trades):
        if t.category not in CGT_CATEGORIES:
            continue
        if not snapped and t.dt.date() > snapshot_at:
            snapshot()
        while split_i < len(splits) and splits[split_i].dt <= t.dt:
            _apply_split(books, splits[split_i], res.warnings)
            split_i += 1

        q = t.qty
        cash = t.cash + fold_adjustments.get(idx, 0.0)
        aud_adj = aud_adjustments.get(idx, 0.0)
        if abs(q) < QTY_EPS:
            continue
        lots = books[(t.category, t.symbol)]
        is_option = t.category == "Equity and Index Options"
        fold_close = is_option and ("C" in t.codes) and (t.codes & {"A", "Ex"})
        my_rpl = 0.0
        remaining = q
        rem_cash = cash
        unmatched_qty = 0.0
        unmatched_cash = 0.0
        pre_taken, in_data_open = [], 0.0
        # nothing at all on the books: a closing trade can only have closed a
        # position opened before the data (IBKR labels every fill of an order
        # C;O, including one that only adds to the position the first opened)
        nothing_held = not lots

        while abs(remaining) > QTY_EPS and lots and lots[0].qty * remaining < 0:
            lot = lots[0]
            take = min(abs(remaining), abs(lot.qty))
            open_alloc = lot.cash * (take / abs(lot.qty))
            close_alloc = cash * (take / abs(q))
            open_adj = lot.aud_adj * (take / abs(lot.qty))
            lot.qty += take if remaining > 0 else -take
            lot.cash -= open_alloc
            lot.aud_adj -= open_adj
            remaining += take if remaining < 0 else -take
            rem_cash -= close_alloc
            lot_open_dt, lot_pre = lot.dt, lot.pre_data
            if abs(lot.qty) < QTY_EPS:
                lots.pop(0)
            if fold_close and suppress_fold_matches:
                taken = Transfer(
                    option_symbol=t.symbol, option_qty=take,
                    kind="assignment" if "A" in t.codes else "exercise",
                    dt=t.dt, cash=open_alloc, close_cash=close_alloc,
                    currency=t.currency, option_open_dt=lot_open_dt, pre_data=lot_pre)
                res.transfers.append(taken)
            else:
                taken = Match(
                    category=t.category, symbol=t.symbol, currency=t.currency,
                    qty=take, open_dt=lot_open_dt, close_dt=t.dt,
                    open_cash=open_alloc, close_cash=close_alloc,
                    short=q > 0, via_expiry="Ep" in t.codes, pre_data=lot_pre,
                    open_aud_adj=open_adj, close_aud_adj=aud_adj * (take / abs(q)))
                res.matches.append(taken)
                my_rpl += open_alloc + close_alloc
            if lot_pre:
                pre_taken.append((taken, take))
            else:
                in_data_open += open_alloc

        if pre_taken:
            # IBKR's Basis on a closing row is the opening cash of the units it
            # closes (equal to the cent on every option close in real statements
            # checked), so whatever the in-data lots do not account for belongs
            # to the position held before the data.
            pre_cash = t.ib_basis - in_data_open
            total = sum(take for _, take in pre_taken)
            for obj, take in pre_taken:
                share = pre_cash * take / total
                if isinstance(obj, Transfer):
                    obj.cash = share
                else:
                    obj.open_cash = share
                    obj.basis_unknown = not t.ib_basis
                    my_rpl += share

        if abs(remaining) > QTY_EPS:
            if "C" in t.codes and "O" not in t.codes and not fold_close:
                # A pure closing trade with nothing left to match: opening lots
                # predate the uploaded data. Do NOT create a phantom position.
                unmatched_qty = remaining
                unmatched_cash = rem_cash
                rpl_alloc = t.ib_rpl * (abs(remaining) / abs(q)) if t.ib_rpl else 0.0
                # Buying back an option is closing one that was written: its
                # premium is not in this year (D2 at grant, s 104-40(2)).
                written = is_option and remaining > 0
                res.unmatched.append(UnmatchedClose(
                    trade=t, qty_unmatched=remaining, close_cash_alloc=rem_cash,
                    ib_rpl=rpl_alloc, resolved=bool(t.ib_rpl) or written, written=written))
            elif fold_close:
                # Exercised or assigned, but opened before the data: no lot to
                # close. Fold the option's opening cash from IBKR's Basis into the
                # shares like any other assignment/exercise, rather than leave a
                # phantom option position behind.
                opened = (data_start or t.dt) - timedelta(seconds=1)
                res.transfers.append(Transfer(
                    option_symbol=t.symbol, option_qty=abs(remaining),
                    kind="assignment" if "A" in t.codes else "exercise", dt=t.dt,
                    cash=t.ib_basis * (abs(remaining) / abs(q)), close_cash=rem_cash,
                    currency=t.currency, option_open_dt=opened, pre_data=True))
            else:
                if "C" in t.codes and "O" in t.codes and nothing_held and t.ib_rpl:
                    res.warnings.append(
                        f"{t.symbol} on {t.dt.date()}: a trade that both closes and opens "
                        "(code C;O) found no position to close in the uploaded data — the "
                        "part it closed was opened before the data starts, so this year's "
                        "figures for the contract are not reliable. Upload the statement that "
                        "covers when the position was opened.")
                books[(t.category, t.symbol)].append(
                    Lot(qty=remaining, cash=rem_cash, dt=t.dt, currency=t.currency,
                        aud_adj=aud_adj * (abs(remaining) / abs(q))))

        if "C" in t.codes and not fold_close and abs(unmatched_qty) < QTY_EPS:
            res.row_checks.append((t, my_rpl))

    if not snapped:
        snapshot()
    while split_i < len(splits):
        _apply_split(books, splits[split_i], res.warnings)
        split_i += 1
    return res


def _attach_transfers(trades: list[Trade], transfers: list[Transfer]) -> dict[int, float]:
    """Fold each option transfer into the matching share trade (code A/Ex on the
    share side, same underlying, nearest in time). Returns trade-index -> cash
    adjustment; annotates each transfer with its target."""
    stock_rows = [(i, t) for i, t in enumerate(trades)
                  if t.category == "Stocks" and (t.codes & {"A", "Ex"})]
    adjustments: dict[int, float] = defaultdict(float)
    for tr in transfers:
        under = tr.option_symbol.split()[0]
        want_code = "A" if tr.kind == "assignment" else "Ex"
        cands = [(abs((t.dt - tr.dt).total_seconds()), i, t) for i, t in stock_rows
                 if t.symbol == under and want_code in t.codes
                 and abs((t.dt - tr.dt).total_seconds()) <= 5 * 86400]
        if not cands:
            tr.stock_trade_idx = None
            continue
        cands.sort(key=lambda c: (c[0], c[1]))
        _, i, t = cands[0]
        adjustments[i] += tr.cash
        tr.stock_trade_idx = i
        tr.stock_symbol = t.symbol
    return dict(adjustments)


# --------------------------------------------------------------------------
# Report computation
# --------------------------------------------------------------------------

@dataclass
class Options:
    fy_end_year: int
    entity: str = "individual"          # individual | trust | smsf | company
    carried_losses: float = 0.0         # prior-year net capital losses (AUD, positive number)
    account_mask: bool = False


def _normalize_symbol_renames(stmt: Statement, splits: list[SplitEvent]) -> list[str]:
    """Unify symbols renamed mid-period (e.g. ticker changes) using the
    Financial Instrument Information conid mapping.

    Option contracts are renamed too, but never show up as two symbols against
    one conid: an option keeps its own conid across an underlying rename, so
    the instrument info lists it under one name only while the trade rows
    switch mid-life ("SATS 02JUL26 110 P" opens, "ECHO 02JUL26 110 P" closes).
    Left alone the opening leg never matches and the close falls back to
    IBKR's realized P/L while the open leg is still carried as a written
    position at 30 June — the same premium counted twice, in both directions.
    So the ticker renames established at stock level are also applied to the
    underlying token of every option symbol seen in the statement.

    An option split is not a rename. One conid can be listed under its pre-split
    terms in one file's instrument info and its post-split terms in another's
    ("NVDL 10JUL26 80 P" / "NVDL 10JUL26 26.67 P"); renaming here would carry the
    old contract count across and leave the split nothing to match, so the
    extra contracts would close unmatched against IBKR's P/L. Those pairs are
    left to _apply_split, which renames and multiplies the quantity. The split
    events themselves are keyed by symbol, so the ticker renames found here are
    applied to them as well."""
    notes = []
    rename: dict[str, str] = {}
    option_splits = [ev for ev in splits if ev.category == "Equity and Index Options"]
    for conid, syms in stmt.conid_symbols.items():
        if len(syms) < 2:
            continue
        if any(ev.new_symbol in syms and all(s == ev.new_symbol or _is_presplit_contract(s, ev)
                                             for s in syms) for ev in option_splits):
            continue
        # The instrument info names the current symbol outright; everything
        # below is a fallback for statements that leave the column empty.
        current = stmt.conid_current.get(conid)
        if current not in syms:
            last_seen: dict[str, datetime] = {}
            for t in stmt.trades:
                # An option trade dates its underlying too — for a ticker held
                # only through options that is the only signal available.
                for s in {t.symbol, t.symbol.partition(" ")[0]} & set(syms):
                    if t.dt > last_seen.get(s, datetime.min):
                        last_seen[s] = t.dt
            open_syms = {p.symbol for p in stmt.open_positions}
            open_syms |= {p.symbol.partition(" ")[0] for p in stmt.open_positions}
            current = None
            for s in syms:
                if s in open_syms:
                    current = s
            if current is None and last_seen:
                current = max(last_seen, key=lambda s: last_seen[s])
            if current is None:
                current = syms[-1]
        for s in syms:
            if s != current:
                rename[s] = current
    # An option's own conid survives a rename of its underlying, so the loop
    # above cannot see these; re-point them using the ticker renames it found.
    underlying_rename = {old: new for old, new in rename.items()
                         if " " not in old and " " not in new}
    if underlying_rename:
        option_symbols = {t.symbol for t in stmt.trades
                          if t.category == "Equity and Index Options"}
        option_symbols |= {p.symbol for p in stmt.open_positions
                           if p.category == "Equity and Index Options"}
        for sym in option_symbols:
            root, sep, rest = sym.partition(" ")
            if sep and root in underlying_rename:
                rename.setdefault(sym, f"{underlying_rename[root]} {rest}")

    if rename:
        for t in stmt.trades:
            if t.symbol in rename:
                t.symbol = rename[t.symbol]
        for p in stmt.open_positions:
            if p.symbol in rename:
                p.symbol = rename[p.symbol]
        for snap in stmt.position_snapshots:
            renamed: dict = defaultdict(float)
            for (cat, sym), qty in snap.qty.items():
                renamed[(cat, rename.get(sym, sym))] += qty
            snap.qty = dict(renamed)
        for ev in splits:
            if ev.category == "Stocks":
                ev.old_symbol = rename.get(ev.old_symbol, ev.old_symbol)
                ev.new_symbol = rename.get(ev.new_symbol, ev.new_symbol)
            else:
                root, sep, rest = (ev.new_symbol or "").partition(" ")
                if sep and root in underlying_rename:
                    ev.new_symbol = f"{underlying_rename[root]} {rest}"
        for old, new in sorted(rename.items()):
            po, pn = parse_option_symbol(old), parse_option_symbol(new)
            if po and pn and po[1:] != pn[1:]:
                # Not a ticker change: the contract itself was adjusted, and no
                # split in the data says by how much.
                notes.append(
                    f"option contract terms changed with no matching corporate action in the "
                    f"uploaded data: {old} -> {new} (one IBKR conid). Treated as one contract "
                    "with the number of contracts unchanged; if the adjustment (e.g. a split) "
                    "changed it, this contract's figures are wrong — upload the statement that "
                    "covers the adjustment, or review manually.")
            else:
                notes.append(f"symbol change unified via conid: {old} -> {new}")
    return notes


def _pre_data_lots(stmt: Statement) -> dict:
    """Positions already held when the uploaded data starts.

    The earliest statement's Mark-to-Market Performance Summary gives each
    position's quantity at the start of its period ("Prior Quantity"); trades in
    the data before then (an earlier file without that summary) are taken off.
    What is left was opened before any of the data: one lot per position, with
    the quantity known but not the opening cash or date. FIFO closes these first
    (they are the oldest), and each closing row's IBKR Basis supplies the opening
    cash of the units it takes. Without these lots, a sale of shares held before
    the data would silently consume this year's purchases instead."""
    snaps = [p for p in stmt.position_snapshots if p.as_of]
    if not snaps:
        return {}
    first = min(snaps, key=lambda p: p.as_of)
    snap_start = datetime.combine(first.as_of, time())
    start = datetime.combine(min(first.as_of, stmt.period_start or first.as_of), time())
    lots = {}
    for (cat, sym), qty in sorted(first.qty.items()):
        if cat not in CGT_CATEGORIES:
            continue
        same = [t for t in stmt.trades if (t.category, t.symbol) == (cat, sym)]
        qty -= sum(t.qty for t in same if t.dt < snap_start)
        if abs(qty) < QTY_EPS:
            continue
        ccy = (same[0].currency if same else None) or next(
            (p.currency for p in stmt.open_positions if (p.category, p.symbol) == (cat, sym)),
            None) or stmt.base_currency or "USD"
        lots[(cat, sym)] = Lot(qty=qty, cash=0.0, dt=start - timedelta(seconds=1),
                               currency=ccy, pre_data=True)
    return lots


def _snapshot_mismatches(stmt: Statement, pre_lots: dict, splits: list[SplitEvent]) -> list[str]:
    """Each later statement's opening positions (Mark-to-Market "Prior
    Quantity") against what the data before it adds up to. A difference means
    trades are missing before that date — a gap between the files, or a file
    that is not complete — and FIFO would match later sales against parcels
    that were already sold. Symbols touched by a split are left out: their
    quantities change without a trade."""
    snaps = sorted((p for p in stmt.position_snapshots if p.as_of), key=lambda p: p.as_of)
    split_roots = {(ev.old_symbol or ev.new_symbol or "").partition(" ")[0] for ev in splits}
    out = []
    for snap in snaps[1:]:
        start = datetime.combine(snap.as_of, time())
        held: dict = defaultdict(float)
        for key, lot in pre_lots.items():
            held[key] += lot.qty
        for t in stmt.trades:
            if t.category in CGT_CATEGORIES and t.dt < start:
                held[(t.category, t.symbol)] += t.qty
        keys = set(held) | {k for k in snap.qty if k[0] in CGT_CATEGORIES}
        diffs = [f"{sym} {snap.qty.get((cat, sym), 0.0):g} (the data adds up to "
                 f"{held.get((cat, sym), 0.0):g})"
                 for cat, sym in sorted(keys)
                 if sym.partition(" ")[0] not in split_roots
                 and abs(held.get((cat, sym), 0.0) - snap.qty.get((cat, sym), 0.0)) > 1e-6]
        if diffs:
            more = f" and {len(diffs) - 8} more" if len(diffs) > 8 else ""
            out.append(
                f"POSITIONS DO NOT ADD UP at the start of {snap.source_name} ({snap.as_of}): "
                f"IBKR reports {', '.join(diffs[:8])}{more}. Trades before that date are "
                "missing — a gap between the uploaded files, or an incomplete file — so FIFO "
                "history for these symbols is unreliable. Upload the missing statement.")
    return out


def _withholding_above_treaty(stmt: Statement, fx: RbaRates, window) -> tuple[float, list]:
    """US withholding above the 15% Australia–US treaty rate on portfolio
    dividends, in AUD, and the symbols it applies to. The "US Tax" rows of one
    security and date are netted first — IBKR reverses a charge and posts it
    again — then compared with that day's dividend."""
    fy_start, fy_end = window
    dividends, withheld = defaultdict(float), defaultdict(float)
    for it in stmt.dividends:
        dividends[(it.d, it.description.split("(")[0].strip())] += it.amount
    for it in stmt.withholding_tax:
        if "US Tax" in it.description and fy_start <= it.d <= fy_end:
            withheld[(it.d, it.currency, it.description.split("(")[0].strip())] += it.amount
    excess, syms = 0.0, []
    for (d, ccy, sym), amount in sorted(withheld.items()):
        gross = dividends.get((d, sym), 0.0)
        over = -amount - 0.15 * gross
        if gross > 0 and over > 0.005 + 0.001 * gross:   # rounding of a 15% charge
            excess += fx.to_aud(over, ccy, d)[0]
            if sym not in syms:
                syms.append(sym)
    return round(excess, 2), syms


def _rows_after_statement_end(stmt: Statement, fy_end: date) -> list[str]:
    """Rows a statement ending on the FY end dates after it. IBKR's statement
    day runs to 10:00 Sydney the next morning and rows are filed by their US
    report date, so a 30 June statement carries rows dated 1 July in Sydney
    time — rows the next year's statement starting 1 July may not repeat."""
    out = []
    for rep in stmt.cash_reports:
        own = rep.owner
        if own is None or own.period_end != fy_end:
            continue
        for label, items in (("dividend", own.dividends), ("withholding", own.withholding_tax),
                             ("interest", own.interest), ("fee", own.fees)):
            out += [f"{label} {it.d} {it.currency} {it.amount:,.2f}"
                    for it in items if it.d > fy_end]
        out += [f"trade {t.symbol} {t.dt.date()}" for t in own.trades
                if t.dt.date() > fy_end and t.category in CGT_CATEGORIES]
    return out


def _entity_notes(opts: Options) -> list[str]:
    """What the figures assume for this entity, and what they leave to the
    accountant."""
    notes = []
    if opts.entity == "individual":
        notes.append("Figures for an Australian resident individual (no foreign or temporary "
                     "resident discount limits, ss 115-105, 115-110).")
    if opts.entity in ("individual", "trust") and opts.fy_end_year == DISCOUNT_ENDS.year:
        notes.append(
            "Keep the market value at 30 June 2027 of everything still held then (the Open "
            "Positions of a statement ending 30 June 2027): from 1 July 2027 the CGT discount "
            "ends for individuals and trusts, and s 112-155 deems those assets sold and "
            "reacquired at that value (Act No 49 of 2026).")
    if opts.entity == "smsf":
        notes.append(
            "Assumes the fund is wholly in accumulation phase. Exempt current pension income is "
            "not applied: gains on segregated current pension assets are disregarded "
            "(s 118-320), and an unsegregated fund exempts the actuarial proportion (SMSF annual "
            "return item 11 label Y). A fund with members in retirement phase needs its "
            "accountant to adjust these figures.")
        notes.append(
            "For a complying super fund the CGT rules are the primary code (s 295-85), so gains "
            "and losses on shares and options are capital; ATO ID 2009/110 and 2009/111 apply "
            "this to exchange-traded options a fund writes and buys (on their own facts — a "
            "hedging strategy). Have the fund's accountant confirm for a strategy that writes "
            "options routinely.")
    if opts.entity == "company":
        notes.append(
            "A company may apply prior-year net capital losses only if it passes the continuity "
            "of ownership test or the business continuity test (CGT schedule instructions, "
            "part 2B); the losses entered are applied without checking that.")
    if opts.entity == "trust":
        notes.append(
            "Figures are the trust's net capital gain. How it reaches beneficiaries (streaming, "
            "grossing up discounted gains, each beneficiary's own discount and losses) and the "
            "trust loss rules are outside this working paper.")
    if opts.entity != "individual":
        notes.append("A CGT schedule must be lodged with the return when total current-year "
                     "capital gains or capital losses exceed $10,000.")
    return notes


def _d2_outcome_warning(unresolved: list, data_end: date | None, fy_end: date) -> str:
    """Warning for D2 premiums whose post-30-June fate the uploaded data cannot show.

    unresolved: (symbol, write date, expiry or None, premium AUD) per open lot."""
    total = sum(u[3] for u in unresolved)
    items = "; ".join(f"{sym} (written {od}, expires {exp or 'unknown'}, D2 A${p:,.2f})"
                      for sym, od, exp, p in unresolved)
    # Ending ON the expiry date is not enough: the expiry/assignment row may be
    # dated the next day depending on the statement's time zone.
    later = [u for u in unresolved if u[2] is None or data_end is None or data_end <= u[2]]
    stale = [u for u in unresolved if u not in later]
    msg = (f"D2 OUTCOME NOT IN UPLOADED DATA — {len(unresolved)} written option(s) still open "
           f"at {fy_end} are counted in full as D2 gains (A${total:,.2f}, strict view), but the "
           f"uploaded data ends {data_end or 'at an unknown date'} and does not show what "
           f"happened to them afterwards: {items}. If any was assigned or exercised after "
           "30 June, s 104-40(5) disregards its D2 gain and the net capital gain is overstated "
           "by up to that premium.")
    if later:
        last = max((u[2] for u in later if u[2]), default=None)
        # It must also START by 1 July: an outcome row dated before the file begins
        # is missing however late the file ends.
        msg += (" To resolve: also upload an Activity Statement CSV that starts on or before "
                f"{fy_end + timedelta(days=1)} and ends after "
                f"{last or 'the latest expiry'} (the post-year-end statement; overlapping "
                "periods are de-duplicated). For a contract that has not expired yet the "
                "strict figure stands, and a later assignment or exercise means amending this "
                "return.")
    if stale:
        msg += (f" The data runs past the expiry of {', '.join(u[0] for u in stale)} yet shows "
                "no close, expiry or assignment for it — check the statements are complete "
                "and leave no gap after 30 June.")
    return msg


def _sum_cash_aud(items, fx: RbaRates, window: tuple[date, date], dropped: dict):
    """Sum cash items falling inside the FY window, translated at each item's own date.

    Statements routinely cover more than the FY being reported (extra history for
    FIFO, or a period that simply runs past 30 June); items outside the window
    belong to another year's return and are counted in `dropped`, not here.
    """
    fy_start, fy_end = window
    rows, total_aud = [], 0.0
    by_ccy = defaultdict(float)
    for it in items:
        if not (fy_start <= it.d <= fy_end):
            dropped["n"] += 1
            continue
        aud, rate = fx.to_aud(it.amount, it.currency, it.d)
        rows.append(dict(currency=it.currency, date=str(it.d), description=it.description,
                         amount=round(it.amount, 2), fx=rate, aud=round(aud, 2)))
        total_aud += aud
        by_ccy[it.currency] += it.amount
    return rows, round(total_aud, 2), {k: round(v, 2) for k, v in by_ccy.items()}


def _div775_from_cash(stmt: Statement, fx: RbaRates, fy_start: date, fy_end: date,
                      fy_end_year: int, warnings: list[str]):
    """Div 775 for an account whose IBKR base currency is not AUD (engine/forex.py).

    IBKR's Forex P/L Details for such an account measures its non-base balances
    (typically AUD cash) in the base currency. For an Australian resident AUD is
    not a foreign currency, and the base-currency cash itself — the actual
    exposure — is not in that section at all, so it is not used."""
    base = stmt.base_currency or "the base currency"
    res = forex.compute(stmt, fx, fy_start, fy_end)
    rows, notes = res["rows"], []
    n2 = sum(1 for r in rows if r["event"].startswith("FRE 2"))
    notes.append(
        f"Div 775 on foreign-currency cash is computed from the statements' cash flows: "
        f"{len(rows)} realisation(s) in {fy_label(fy_end_year)} — {n2} FRE 2 (spending, "
        f"converting or withdrawing a credit balance, s 775-45) and {len(rows) - n2} FRE 4 "
        f"(paying back a debit balance, s 775-55); gains A${res['gains_aud']:,.2f}, losses "
        f"A${-res['losses_aud']:,.2f}, net {'-' if res['total_aud'] < 0 else ''}"
        f"A${abs(res['total_aud']):,.2f}. Units are matched "
        "first-in first-out (s 775-145(1)). AUD values are RBA daily rates, except for "
        "conversions to or from AUD, which use the trade's own AUD amount.")
    for r in res["reconciliation"]:
        line = (f"{r['currency']} cash in {r['source']} ({r['period']}): start "
                f"{r['start']:,.2f} + flows read {r['flows']:,.2f} = "
                f"{r['start'] + r['flows']:,.2f}, against IBKR's ending cash {r['end']:,.2f}")
        if abs(r["gap"]) <= forex.RECONCILE_TOLERANCE:
            notes.append(line + (f" — reconciles within {abs(r['gap']):,.2f} (the statement "
                                 "does not itemise GST charged on fees)."
                                 if abs(r["gap"]) >= 0.005 else " — reconciles."))
        else:
            warnings.append(
                f"DIV 775 MAY BE INCOMPLETE — {line}: a gap of {r['gap']:,.2f} "
                f"{r['currency']}. Some cash movements are not itemised in the sections this "
                "tool reads (for example cash from a corporate action, or a transfer between "
                "accounts). Review them before relying on the FX figure.")
    if not stmt.cash_reports:
        warnings.append(
            "No Cash Report section in the uploaded files: opening foreign-currency balances "
            "are taken as zero for Div 775, and the cash flows cannot be checked against "
            "IBKR's totals. The Activity Statement CSV (file (a)) has one.")
    for n in res["notes"]:
        warnings.append("DIV 775 OPENING BALANCE — " + n)
    if stmt.forex_pl:
        notes.append(
            f"IBKR's 'Forex P/L Details' (Realized Summary) measures this {base}-base "
            f"account's other balances, such as AUD cash, in {base}. For an Australian "
            f"resident AUD is not a foreign currency and the {base} cash itself is not "
            "covered, so that section is not used.")
    for ccy, c in sorted(res["closing"].items()):
        credit = c["units"] > 0
        notes.append(
            f"{ccy} {'credit' if credit else 'debit'} balance carried past {fy_end}: "
            f"{abs(c['units']):,.2f}, FIFO "
            f"{'forex cost base' if credit else 'proceeds of assuming the obligation'} "
            f"A${abs(c['aud_cost']):,.2f}. Next year's figure needs this year's statements "
            "uploaded together with next year's.")
    notes.append(
        "Not applied, because they depend on elections and facts outside the statements: "
        "the $250,000 balance election (s 775-230), under which FRE 2 gains and losses on a "
        "qualifying forex account with a credit balance, and FRE 4 on one with a debit "
        "balance, are disregarded while its balance test is met (whether an IBKR account is "
        "a qualifying forex account is a question for a tax agent); the retranslation "
        "election (s 775-270); and the weighted-average alternative to FIFO. Cash movements "
        "are dated at trade date: forex effects between a trade in a share or option and "
        "its settlement are folded into the capital gain under the 12-month rule "
        "(ss 775-70, 775-75).")
    method = "computed from cash flows: FIFO on foreign-currency cash (s 775-145)"
    return [], res["total_aud"], method, rows, notes


def compute_tax_report(stmt: Statement, opts: Options, fx: RbaRates | None = None) -> dict:
    fx = fx or RbaRates()
    warnings = list(stmt.warnings)
    fy_start, fy_end = fy_window(opts.fy_end_year)
    if opts.entity not in ENTITY_DISCOUNT:
        raise EngineError(f"unknown entity type {opts.entity!r}")
    discount_rate = ENTITY_DISCOUNT[opts.entity]
    if opts.entity in ("individual", "trust") and fy_start >= DISCOUNT_ENDS:
        raise EngineError(
            f"{fy_label(opts.fy_end_year)} is not supported for {opts.entity}s: from 1 July 2027 "
            "the 50% CGT discount no longer applies to their CGT events (s 115-100(aa), (ab) "
            "ITAA 1997, as amended by Act No 49 of 2026). Assets held at 30 June 2027 are "
            "deemed sold and reacquired at market value then (s 112-155), with that gain still "
            "discountable when the asset is sold, and later growth is indexed. This tool does "
            "not model those rules yet, and a figure without them would be wrong.")

    # Planned before the renames: a contract that one file names before an option
    # split and another after it must be left for the split to adjust.
    splits, ca_warnings = plan_corporate_actions(stmt)
    warnings.extend(_normalize_symbol_renames(stmt, splits))

    # Data coverage vs requested FY
    if stmt.period_start and stmt.period_start > fy_start:
        warnings.append(
            f"uploaded data starts {stmt.period_start}, after the start of "
            f"{fy_label(opts.fy_end_year)} — positions opened earlier are not visible. "
            "Upload prior-period statements for complete FIFO history.")
    if stmt.period_end and stmt.period_end < fy_end:
        warnings.append(
            f"uploaded data ends {stmt.period_end}, before the end of "
            f"{fy_label(opts.fy_end_year)} — results are provisional.")
    if stmt.period_end and stmt.period_end > fy_end:
        warnings.append(
            f"uploaded data extends to {stmt.period_end}, past the end of "
            f"{fy_label(opts.fy_end_year)} — post-30-June activity is used only for "
            "context and is excluded from this year's figures.")

    excluded = defaultdict(int)
    for t in stmt.trades:
        if t.category not in CGT_CATEGORIES and t.category != "Forex":
            excluded[t.category] += 1
    for cat, n in excluded.items():
        warnings.append(f"{n} {cat} trades excluded — asset category not supported; "
                        "handle manually.")

    warnings.extend(ca_warnings)

    trades = stmt.trades
    pre_lots = _pre_data_lots(stmt)
    warnings.extend(_snapshot_mismatches(stmt, pre_lots, splits))
    data_start_d = stmt.period_start
    data_start = datetime.combine(data_start_d, time()) if data_start_d else None
    before_data = f"before {data_start_d}" if data_start_d else "before the uploaded data"
    # Pass 1: discover assignment/exercise transfers.
    pass1 = run_fifo(trades, splits, initial_lots=pre_lots, data_start=data_start)
    fold_adj = _attach_transfers(trades, pass1.transfers)
    orphan_transfers = [tr for tr in pass1.transfers if tr.stock_trade_idx is None]
    for tr in orphan_transfers:
        warnings.append(
            f"{tr.kind} of {tr.option_symbol} on {tr.dt.date()} has no matching share "
            "trade (cash-settled or missing data) — treated as a normal option close.")
    # The folded premium keeps its own date's rate (s 960-50(6) item 5): the
    # share leg's AUD is its cash at the share trade's rate, corrected by the
    # premium's AUD at the write date less its AUD at that rate. Unknown for an
    # option opened before the data — it stays at the share trade's rate.
    aud_adj: dict[int, float] = defaultdict(float)
    for tr in pass1.transfers:
        if tr.stock_trade_idx is None or tr.pre_data:
            continue
        stock_d = trades[tr.stock_trade_idx].dt.date()
        aud_adj[tr.stock_trade_idx] += (fx.to_aud(tr.cash, tr.currency, tr.option_open_dt.date())[0]
                                        - fx.to_aud(tr.cash, tr.currency, stock_d)[0])
    # Pass 2: definitive run with share-leg cash adjusted by folded premiums.
    # Orphan folds (no share leg) fall back to normal matches.
    res = run_fifo(trades, splits, fold_adjustments=fold_adj,
                   suppress_fold_matches=True, initial_lots=pre_lots, data_start=data_start,
                   aud_adjustments=dict(aud_adj), snapshot_at=fy_end)
    if orphan_transfers:
        # A fold with no share leg (cash-settled index option, or missing data)
        # reverts to a normal option close: proceeds are the closing trade's cash.
        orphan_keys = {(tr.option_symbol, tr.dt, round(tr.cash, 6)) for tr in orphan_transfers}
        res_transfers = []
        for tr in res.transfers:
            if (tr.option_symbol, tr.dt, round(tr.cash, 6)) in orphan_keys:
                res.matches.append(Match(
                    category="Equity and Index Options", symbol=tr.option_symbol,
                    currency=tr.currency, qty=tr.option_qty,
                    open_dt=tr.option_open_dt, close_dt=tr.dt,
                    open_cash=tr.cash, close_cash=tr.close_cash, short=tr.cash > 0,
                    pre_data=tr.pre_data))
            else:
                res_transfers.append(tr)
        res.transfers = res_transfers
    _attach_transfers(trades, res.transfers)  # annotate pass-2 objects for display
    warnings.extend(res.warnings)

    # ---------------- realized matches, AUD, FY filter ----------------
    closed_rows = []
    tot = dict(gains_disc=0.0, gains_nondisc=0.0, losses=0.0, net=0.0)
    prior_written_notes = []
    pre_closed, pre_unresolved, pre_maybe_this_fy = [], [], []
    written_closed = dict(n=0, premiums=0.0, buybacks=0.0, net_gains=0.0)
    # The closed-basis comparison counts an option's premium in the year it is
    # closed. For one written in an earlier year the strict figures count only
    # the close leg this year (its premium was that year's D2), so the
    # comparison adds the premium back — otherwise it would appear in no year.
    deferred_extra = 0.0
    for m in res.matches:
        close_d = m.close_dt.date()
        if not (fy_start <= close_d <= fy_end):
            continue
        is_option = m.category == "Equity and Index Options"
        open_d = m.open_dt.date()
        if m.pre_data:
            # Opened before the uploaded data: the opening cash is IBKR's Basis
            # for these units, the date is not in the files.
            if m.basis_unknown and not (m.short and is_option):
                pre_unresolved.append(f"{m.symbol} ({close_d})")
                continue
            aud_close, m.close_fx = fx.to_aud(m.close_cash, m.currency, close_d)
            if m.short and is_option:
                # Written before the data: its premium was a D2 gain of the year it
                # was written (s 104-40(2)). Only the close leg belongs to this year.
                g = aud_close
                note = (f"written {before_data} (premium {m.currency} {m.open_cash:,.2f} "
                        "per IBKR's basis) — its D2 gain belongs to the year it was "
                        "written; only the close leg is recognised this year")
                if data_start_d and data_start_d > fy_start:
                    pre_maybe_this_fy.append(f"{m.symbol} ({m.currency} {m.open_cash:,.2f})")
            else:
                aud_open, m.open_fx = fx.to_aud(m.open_cash, m.currency, close_d)
                g = aud_open + aud_close
                note = (f"acquired {before_data}: cost per IBKR's basis, translated at the "
                        "close-date rate (acquisition date not in the files); no discount "
                        "claimed")
            m.aud_gain = g
            pre_closed.append(m.symbol)
            if g >= 0:
                tot["gains_nondisc"] += g
            else:
                tot["losses"] += g
            tot["net"] += g
            closed_rows.append(dict(
                category=m.category, symbol=m.symbol, currency=m.currency,
                qty=round(m.qty, 4), open_date=before_data, close_date=str(close_d),
                days_held="", open_cash=round(m.open_cash, 2),
                close_cash=round(m.close_cash, 2), open_fx=m.open_fx, close_fx=m.close_fx,
                gain_native=round(m.gain_native, 2), gain_aud=round(g, 2),
                short="Y" if m.short else "", expiry="Y" if m.via_expiry else "",
                discount_eligible="", note=note))
            continue
        if m.short and is_option and fy_of(open_d) < opts.fy_end_year:
            # Written option granted in a prior FY: premium was a D2 gain in
            # that year. This year only the buy-back cost is a capital loss.
            aud_close, fxr = fx.to_aud(m.close_cash, m.currency, close_d)
            g = aud_close
            m.aud_gain, m.close_fx = g, fxr
            aud_open, m.open_fx = fx.to_aud(m.open_cash, m.currency, open_d)
            deferred_extra += aud_open
            note = (f"premium A${aud_open:,.2f} was a D2 gain in {fy_label(fy_of(open_d))}; "
                    f"only the close leg is recognised this year")
            prior_written_notes.append(f"{m.symbol}: {note}")
            disc = False
        else:
            aud_open, m.open_fx = fx.to_aud(m.open_cash, m.currency, open_d)
            aud_close, m.close_fx = fx.to_aud(m.close_cash, m.currency, close_d)
            g = aud_open + m.open_aud_adj + aud_close + m.close_aud_adj
            m.aud_gain = g
            disc = (not m.short) and g > 0 and held_12_months(open_d, close_d)
            note = ""
        parts = [g]
        if m.short and is_option and fy_of(open_d) == opts.fy_end_year:
            # Written and closed in the same year: two CGT events. Granting is D2
            # (the premium, s 104-40(2)); buying it back is a capital loss of its
            # own. Counted gross in total gains and losses — the same as when the
            # two fall in different years. 18A is the same either way; the net
            # per contract is kept for comparison.
            parts = [aud_open + m.open_aud_adj, aud_close + m.close_aud_adj]
            written_closed["n"] += 1
            written_closed["premiums"] += parts[0]
            written_closed["buybacks"] += parts[1]
            written_closed["net_gains"] += max(g, 0.0)
            note = (f"D2 gain A${parts[0]:,.2f} (premium) and buy-back A${parts[1]:,.2f} are "
                    "separate CGT events; counted gross")
        for part in parts:
            if part >= 0:
                tot["gains_disc" if disc else "gains_nondisc"] += part
            else:
                tot["losses"] += part
        tot["net"] += g
        closed_rows.append(dict(
            category=m.category, symbol=m.symbol, currency=m.currency,
            qty=round(m.qty, 4), open_date=str(open_d), close_date=str(close_d),
            days_held=(close_d - open_d).days,
            # a day count cannot tell whether 12 months have passed (365 days may
            # not be enough); this is the first day the discount applies
            discount_from="" if m.short else str(discount_from(open_d)),
            open_cash=round(m.open_cash, 2), close_cash=round(m.close_cash, 2),
            open_fx=m.open_fx, close_fx=m.close_fx,
            gain_native=round(m.gain_native, 2), gain_aud=round(g, 2),
            short="Y" if m.short else "", expiry="Y" if m.via_expiry else "",
            discount_eligible="Y" if disc else "", note=note))

    # ---------------- unmatched closes ----------------
    unmatched_rows, unmatched_total_aud, unresolved_count = [], 0.0, 0
    unmatched_gains = unmatched_losses = 0.0
    for u in res.unmatched:
        close_d = u.trade.dt.date()
        in_fy = fy_start <= close_d <= fy_end
        if u.written and in_fy:
            # IBKR's Realized P/L would add the premium back in — it was already
            # a D2 gain of the year the option was written.
            aud_g, fxr = fx.to_aud(u.close_cash_alloc, u.trade.currency, close_d)
            u.aud_gain = aud_g
            unmatched_total_aud += aud_g
            if data_start_d and data_start_d > fy_start:
                pre_maybe_this_fy.append(u.trade.symbol)
        elif u.resolved and in_fy:
            aud_g, fxr = fx.to_aud(u.ib_rpl, u.trade.currency, close_d)
            u.aud_gain = aud_g
            unmatched_total_aud += aud_g
        elif in_fy:
            unresolved_count += 1
            aud_g, fxr = 0.0, 0.0
        else:
            continue
        if in_fy and (u.resolved or u.written):
            if aud_g >= 0:
                unmatched_gains += aud_g
            else:
                unmatched_losses += aud_g
        unmatched_rows.append(dict(
            category=u.trade.category, symbol=u.trade.symbol, currency=u.trade.currency,
            close_date=str(close_d), qty=round(u.qty_unmatched, 4),
            close_cash=round(u.close_cash_alloc, 2),
            ib_realized_pl=round(u.ib_rpl, 2) if u.resolved else "",
            gain_aud=round(aud_g, 2) if u.resolved else "UNRESOLVED",
            note="written before uploaded data; its premium was a D2 gain of the year it "
                 "was written, so only the close leg is counted" if u.written else
                 "acquired before uploaded data; IBKR Realized P/L used at close-date FX; "
                 "no discount claimed" if u.resolved else
                 "acquired before uploaded data and no IBKR Realized P/L available"))
    if unmatched_rows:
        warnings.append(
            f"{len(unmatched_rows)} closing trades had no opening lots in the uploaded data. "
            f"{'All' if not unresolved_count else len(unmatched_rows) - unresolved_count} fell "
            "back to IBKR's Realized P/L converted at the close-date rate (approximation — "
            "acquisition-date FX unavailable; CGT discount not claimed). Upload prior-year "
            "statements for exact treatment.")
    if unresolved_count:
        warnings.append(f"{unresolved_count} unmatched closing trades could NOT be resolved and "
                        "are EXCLUDED from totals — resolve manually.")
    tot["gains_nondisc"] += unmatched_gains
    tot["losses"] += unmatched_losses
    tot["net"] += unmatched_total_aud

    # ---------------- D2: written options open at FY end ----------------
    d2_rows, d2_total = [], 0.0
    # Still on the books when the uploaded data runs out, so what happened after
    # 30 June is not visible. An assignment or exercise would disregard the D2
    # gain counted here (s 104-40(5)) — the strict total stays as computed, but
    # it may be overstated and the run must say so.
    d2_unresolved = []
    for (cat, sym), lots in sorted(res.books.items()):
        if cat != "Equity and Index Options":
            continue
        for lot in lots:
            if lot.qty >= -QTY_EPS:
                continue
            open_d = lot.dt.date()
            if lot.pre_data:
                prior_written_notes.append(
                    f"{sym}: written {before_data} and still open at {fy_end} — its D2 gain "
                    "belongs to the year it was written")
                if data_start_d and data_start_d > fy_start:
                    pre_maybe_this_fy.append(sym)
                continue
            if not (fy_start <= open_d <= fy_end):
                if open_d < fy_start:
                    prior_written_notes.append(
                        f"{sym}: written {open_d} (prior FY), still open — D2 belongs to "
                        f"{fy_label(fy_of(open_d))}")
                continue
            prem_aud, fxr = fx.to_aud(lot.cash, lot.currency, open_d)
            d2_total += prem_aud
            expiry = option_expiry(sym)
            d2_rows.append(dict(symbol=sym, write_date=str(open_d), qty=round(lot.qty, 4),
                                premium_native=round(lot.cash, 2), currency=lot.currency,
                                fx=fxr, premium_aud=round(prem_aud, 2),
                                expires=str(expiry or ""),
                                after_fy_end="NOT IN UPLOADED DATA"))
            d2_unresolved.append((sym, open_d, expiry, prem_aud))
    # Written in-FY but closed AFTER fy_end (visible only with multi-year data):
    d2_later_closed = []
    for m in res.matches:
        if m.short and m.category == "Equity and Index Options":
            od, cd = m.open_dt.date(), m.close_dt.date()
            if fy_start <= od <= fy_end and cd > fy_end:
                prem_aud, fxr = fx.to_aud(m.open_cash, m.currency, od)
                d2_total += prem_aud
                d2_rows.append(dict(symbol=m.symbol, write_date=str(od), qty=-round(m.qty, 4),
                                    premium_native=round(m.open_cash, 2), currency=m.currency,
                                    fx=fxr, premium_aud=round(prem_aud, 2),
                                    expires=str(option_expiry(m.symbol) or ""),
                                    after_fy_end=f"{'expired' if m.via_expiry else 'closed'} {cd}"))
                d2_later_closed.append(
                    f"{m.symbol}: written {od}, closed {cd} (next FY) — D2 gain stays in "
                    f"{fy_label(opts.fy_end_year)}; close leg is a capital loss in "
                    f"{fy_label(fy_of(cd))}")
    # Written in-FY but assigned/exercised AFTER fy_end: s 104-40(5) disregards
    # the D2 gain, so there is correctly nothing to return this year. The
    # contract was still open at 30 June though, so without a note its absence
    # from the D2 list silently disagrees with the year-end position snapshot.
    d2_later_assigned = []
    for tr in res.transfers:
        if tr.pre_data or tr.kind != "assignment":
            continue
        od, td = tr.option_open_dt.date(), tr.dt.date()
        if fy_start <= od <= fy_end and td > fy_end:
            prem_aud, _ = fx.to_aud(tr.cash, tr.currency, od)
            d2_later_assigned.append(
                f"{tr.option_symbol}: written {od}, still open at {fy_end}, {tr.kind} on "
                f"{td} (next FY) — the A${prem_aud:,.2f} D2 gain is disregarded "
                f"(s 104-40(5)) and is NOT returned in {fy_label(opts.fy_end_year)}; "
                f"premium folded into the {tr.stock_symbol or '(unmatched)'} parcel "
                f"acquired {td}. No amendment arises if this return is lodged after the "
                f"{tr.kind}.")
    if d2_unresolved:
        # First in the list: it is the one gap that changes the headline figure.
        warnings.insert(0, _d2_outcome_warning(d2_unresolved, stmt.period_end, fy_end))

    if pre_lots:
        held = [f"{sym} {lot.qty:g}" for (cat, sym), lot in sorted(pre_lots.items())]
        more = f" and {len(held) - 12} more" if len(held) > 12 else ""
        warnings.append(
            f"{len(held)} position(s) were already open when the uploaded data starts "
            f"({data_start_d}), per IBKR's Mark-to-Market summary: {', '.join(held[:12])}"
            f"{more}. FIFO closes them first, taking their opening cash from IBKR's cost "
            "basis on each closing trade. Shares and bought options among them are "
            "translated at the close-date rate with no CGT discount; options written among "
            "them are treated as written in an earlier year, so only their close leg counts. "
            "Upload the statements covering when they were opened for exact figures.")
    if pre_unresolved:
        warnings.append(
            f"{len(pre_unresolved)} closing trade(s) of positions held before the uploaded data "
            "carry no IBKR cost basis and are EXCLUDED from totals — resolve manually: "
            + ", ".join(pre_unresolved))
    if pre_maybe_this_fy:
        warnings.append(
            f"The uploaded data starts {data_start_d}, after {fy_start}. These options were "
            f"written before the data starts, possibly after {fy_start}; if so, their premiums "
            f"are D2 gains of {fy_label(opts.fy_end_year)} that these figures leave out: "
            + ", ".join(sorted(set(pre_maybe_this_fy)))
            + f". Upload a statement starting on or before {fy_start}.")

    # Transfers that cancel a prior-year D2 -> amendment flags
    amendment_flags, exercise_notes = [], []
    for tr in res.transfers:
        if tr.pre_data:
            if tr.kind == "assignment" and tr.cash > 0 and fy_start <= tr.dt.date() <= fy_end:
                amendment_flags.append(
                    f"{tr.option_symbol}: written {before_data}, assigned on {tr.dt.date()} — "
                    f"if its premium ({tr.currency} {tr.cash:,.2f} per IBKR's basis) was "
                    "returned as a D2 gain for the year it was written, that gain is now "
                    "disregarded (s 104-40(5)); amend that return, translating the premium at "
                    "the rate on the day it was written. Premium folded into the share parcel "
                    f"{tr.stock_symbol or '(unmatched)'}. Upload that year's statement to have "
                    "the amount worked out.")
            continue
        od = tr.option_open_dt.date()
        if not (fy_of(od) < fy_of(tr.dt.date()) and fy_of(tr.dt.date()) == opts.fy_end_year):
            continue
        if tr.kind != "assignment":
            exercise_notes.append(
                f"{tr.option_symbol}: bought {od}, exercised {tr.dt.date()} — its cost is part "
                f"of the cost base of the {tr.stock_symbol or '(unmatched)'} shares acquired "
                "on exercise (s 134-1(1) item 1); no D2 gain is involved.")
            continue
        prem_aud, _ = fx.to_aud(tr.cash, tr.currency, od)
        written_fy = fy_label(fy_of(od))
        # The earlier return may already have left it out: prepared after the
        # assignment, with a statement that shows it (as this tool does), it
        # never counted the gain.
        amendment_flags.append(
            f"{tr.option_symbol}: written {od} ({written_fy}), assigned on {tr.dt.date()} — "
            f"if the {written_fy} return counted its A${prem_aud:,.2f} premium as a D2 gain, "
            "that gain is now disregarded (s 104-40(5)): amend that return; there is no time "
            "limit for this amendment (s 170(10AA) item 50 ITAA 1936). Nothing to amend if "
            "that return was prepared after the assignment and already left the gain out, "
            "as this tool does when the statement runs past the assignment. Premium folded "
            f"into the share parcel {tr.stock_symbol}.")

    # ---------------- transfers (this-FY assignment/exercise folds) --------
    transfer_rows = [dict(option=tr.option_symbol, kind=tr.kind, date=str(tr.dt.date()),
                          option_written=before_data if tr.pre_data
                          else str(tr.option_open_dt.date()),
                          qty=round(tr.option_qty, 4),
                          premium_native=round(tr.cash, 2), currency=tr.currency,
                          folded_into=tr.stock_symbol or "(unmatched)")
                     for tr in res.transfers
                     if fy_start <= tr.dt.date() <= fy_end]

    # ---------------- carry-forward positions ----------------
    # The parcels held at 30 June: what carries into next year. Data past 30
    # June (uploaded to see what happened to options written in the year) must
    # not move it. Data ending earlier: the parcels at its end, and the report
    # already says it is provisional.
    carry_rows = []
    for (cat, sym), lots in sorted(res.books_at.items()):
        for lot in lots:
            if abs(lot.qty) < QTY_EPS:
                continue
            if lot.pre_data:
                carry_rows.append(dict(category=cat, symbol=sym, qty=round(lot.qty, 4),
                                       acquired=f"{before_data} (cost not in the uploaded "
                                                "files)",
                                       cost_native="", currency=lot.currency, fx="",
                                       cost_aud=""))
                continue
            cost_native = -lot.cash
            cost_aud, fxr = fx.to_aud(cost_native, lot.currency, lot.dt.date())
            cost_aud -= lot.aud_adj
            carry_rows.append(dict(category=cat, symbol=sym, qty=round(lot.qty, 4),
                                   acquired=str(lot.dt.date()),
                                   cost_native=round(cost_native, 2), currency=lot.currency,
                                   fx=fxr, cost_aud=round(cost_aud, 2)))

    # ---------------- reconciliation vs IBKR ----------------
    row_ok, row_bad, row_bad_rows = 0, 0, []
    for t, my_rpl in res.row_checks:
        if abs(my_rpl - t.ib_rpl) <= 0.02:
            row_ok += 1
        else:
            row_bad += 1
            row_bad_rows.append(dict(symbol=t.symbol, date=str(t.dt), mine=round(my_rpl, 2),
                                     ibkr=round(t.ib_rpl, 2),
                                     diff=round(my_rpl - t.ib_rpl, 2)))
    pos_ok, pos_diffs = 0, []
    mine_pos = defaultdict(lambda: [0.0, 0.0])
    for (cat, sym), lots in res.books.items():
        for lot in lots:
            mine_pos[sym][0] += lot.qty
            mine_pos[sym][1] += -lot.cash
    stmt_pos = {p.symbol: (p.qty, p.cost_basis) for p in stmt.open_positions}
    fold_by_under = defaultdict(float)
    for tr in res.transfers:
        fold_by_under[tr.stock_symbol or tr.option_symbol.split()[0]] += tr.cash
    recon_position_applicable = bool(stmt.open_positions) and \
        (stmt.period_end is None or stmt.period_end >= fy_end)
    if recon_position_applicable:
        for sym in sorted(set(mine_pos) | set(stmt_pos)):
            mq, mc = mine_pos.get(sym, [0.0, 0.0])
            sq, sc = stmt_pos.get(sym, (0.0, 0.0))
            if abs(mq - sq) <= 1e-4 and abs(mc - sc) <= 1.0:
                pos_ok += 1
                continue
            explained = ""
            if abs(mq - sq) <= 1e-4 and abs((mc - sc) + fold_by_under.get(sym, 0.0)) <= 1.0:
                explained = "difference equals option premium folded into cost base (s 134-1) — expected"
                pos_ok += 1
            if not explained and abs(mq - sq) <= 1e-4 and any(
                    k[1] == sym for k, lots in res.books.items() for lot in lots if lot.pre_data):
                explained = ("held before the uploaded data: quantity agrees, the cost of that "
                             "part is not in the files")
                pos_ok += 1
            if not explained and abs(mq - sq) <= 1e-4:
                explained = ("cost-only difference — often a return of capital or other "
                             "corporate adjustment IBKR applied to its basis; verify against "
                             "the fund's annual tax statement")
            pos_diffs.append(dict(symbol=sym, my_qty=round(mq, 4), stmt_qty=round(sq, 4),
                                  my_cost=round(mc, 2), stmt_cost=round(sc, 2),
                                  note=explained or "review"))

    # ---------------- other income ----------------
    fy_win = (fy_start, fy_end)
    dropped_cash = {"n": 0}
    div_rows, div_aud, div_ccy = _sum_cash_aud(stmt.dividends, fx, fy_win, dropped_cash)
    # Tax withheld in AUD (on AUD interest, say) is Australian, not a foreign
    # income tax: it is no FITO, and IBKR should not normally withhold it.
    foreign_wht = [it for it in stmt.withholding_tax if it.currency != "AUD"]
    aus_wht = [it for it in stmt.withholding_tax if it.currency == "AUD"]
    wht_rows, wht_aud, wht_ccy = _sum_cash_aud(foreign_wht, fx, fy_win, dropped_cash)
    aus_rows, aus_aud, _ = _sum_cash_aud(aus_wht, fx, fy_win, dropped_cash)
    excess_aud, excess_syms = _withholding_above_treaty(stmt, fx, fy_win)
    int_rows, int_aud, int_ccy = _sum_cash_aud(stmt.interest, fx, fy_win, dropped_cash)
    fee_rows, fee_aud, fee_ccy = _sum_cash_aud(stmt.fees, fx, fy_win, dropped_cash)
    bor_rows, bor_aud, bor_ccy = _sum_cash_aud(stmt.borrow_fees, fx, fy_win, dropped_cash)
    fx_real_rows, forex_notes = [], []
    if (stmt.base_currency or "").upper() == "AUD":
        # IBKR's Forex P/L Details measures the account's non-base balances in the
        # base currency; with an AUD base that is the foreign cash, in AUD.
        fxp_rows, fxp_aud, _ = _sum_cash_aud(stmt.forex_pl, fx, fy_win, dropped_cash)
        forex_method = "IBKR Forex P/L Details (AUD base-currency account)"
        if not stmt.forex_pl and any(t.category == "Forex" for t in stmt.trades):
            warnings.append(
                "Forex trades exist but no 'Forex P/L Details' section was found. Realised "
                "FX gains/losses (Div 775, ordinary income) are NOT included — also upload "
                "the Realized Summary export, which contains that section.")
    else:
        fxp_rows, fxp_aud, forex_method, fx_real_rows, forex_notes = _div775_from_cash(
            stmt, fx, fy_start, fy_end, opts.fy_end_year, warnings)
    if aus_rows:
        warnings.append(
            f"A${-aus_aud:,.2f} was withheld in AUD ({'; '.join(r['description'] for r in aus_rows)}). "
            "That is Australian tax withheld, not foreign tax, so it is not part of the foreign "
            "income tax offset; it is shown separately. IBKR normally withholds it only when it "
            "does not hold the account's TFN or Australian residency — check those with IBKR, "
            "then claim it as tax withheld or ask IBKR to refund it.")
    if excess_aud:
        warnings.append(
            f"US dividend withholding above the 15% treaty rate on {', '.join(excess_syms)}: "
            f"A${excess_aud:,.2f} of the foreign tax withheld. A foreign income tax offset is "
            "available only for tax correctly levied under the Australia–US tax treaty; the "
            "excess is for reclaiming from the IRS, not the offset. Usually IBKR has no valid "
            "W-8BEN (individuals) or W-8BEN-E (funds, companies) for the account — check it.")
    late = _rows_after_statement_end(stmt, fy_end)
    if late:
        warnings.append(
            f"{len(late)} row(s) dated after {fy_end} appear in the statement ending {fy_end}: "
            f"{', '.join(late[:8])}{' …' if len(late) > 8 else ''}. IBKR's statement day ends at "
            "10:00 Sydney time and it files rows by their US report date, so these belong to its "
            f"{fy_end} statement although they are dated {fy_end + timedelta(days=1)} or later in "
            "Sydney time. They are counted by their date, in the next financial year, and a "
            "next-year statement starting 1 July may not include them: upload this statement "
            "with next year's, or count them in this year if you take the US date instead.")
    if dropped_cash["n"]:
        warnings.append(
            f"{dropped_cash['n']} dividend/interest/fee/FX rows dated outside "
            f"{fy_label(opts.fy_end_year)} were excluded from Other amounts — they belong "
            "to another year's return.")

    # ---------------- net capital gain ----------------
    gains_disc, gains_nondisc = tot["gains_disc"], tot["gains_nondisc"] + d2_total
    losses_current = -tot["losses"]                      # positive number
    losses_avail = losses_current + max(opts.carried_losses, 0.0)
    # apply losses to non-discountable first (standard, taxpayer-favourable order)
    use_nd = min(losses_avail, gains_nondisc)
    rem = losses_avail - use_nd
    use_d = min(rem, gains_disc)
    rem_losses = rem - use_d
    disc_base = gains_disc - use_d
    discount_amt = disc_base * discount_rate
    net_capital_gain = (gains_nondisc - use_nd) + disc_base - discount_amt
    total_gains_18h = gains_disc + gains_nondisc         # ATO 18H: total current-year gains
    strict_subtotal = tot["net"] + d2_total
    # Current-year losses are applied before any carried in (s 102-5, steps 1
    # and 2): CGT schedule item 2, labels B and C.
    current_applied = min(losses_current, gains_disc + gains_nondisc)
    carried_applied = (use_nd + use_d) - current_applied

    entity_notes = _entity_notes(opts)
    summary = dict(
        fy=fy_label(opts.fy_end_year), entity=opts.entity,
        labels=RETURN_LABELS[opts.entity],
        discount_rate=discount_rate,
        closed_gains_discountable=round(gains_disc, 2),
        closed_gains_nondiscountable=round(tot["gains_nondisc"], 2),
        closed_losses=round(tot["losses"], 2),
        closed_net=round(tot["net"], 2),
        d2_open_written=round(d2_total, 2),
        d2_outcome_unknown=round(sum(u[3] for u in d2_unresolved), 2),
        strict_subtotal=round(strict_subtotal, 2),
        deferred_alternative=round(tot["net"] + deferred_extra, 2),
        current_year_losses=round(losses_current, 2),
        current_year_losses_applied=round(current_applied, 2),
        carried_losses_applied=round(carried_applied, 2),
        carried_losses_input=round(opts.carried_losses, 2),
        losses_applied_total=round(use_nd + use_d, 2),
        discount_applied=round(discount_amt, 2),
        net_capital_gain_18A=round(net_capital_gain, 2),
        total_capital_gains_18H=round(total_gains_18h, 2),
        # For comparison: each option written and closed in the year netted
        # against its buy-back, as one result per contract.
        total_capital_gains_net_basis=round(
            total_gains_18h - written_closed["premiums"] + written_closed["net_gains"], 2),
        written_options_closed=dict(
            count=written_closed["n"], premiums_aud=round(written_closed["premiums"], 2),
            buybacks_aud=round(written_closed["buybacks"], 2)),
        losses_carried_forward_18V=round(rem_losses, 2),
        other_income=dict(
            dividends_aud=div_aud, withholding_tax_aud=wht_aud,
            interest_aud=int_aud, fees_aud=fee_aud, borrow_fees_aud=bor_aud,
            forex_pl_aud=fxp_aud, forex_method=forex_method,
            australian_withholding_aud=aus_aud, withholding_above_treaty_aud=excess_aud,
            dividends_by_ccy=div_ccy, withholding_by_ccy=wht_ccy,
            interest_by_ccy=int_ccy),
    )

    fx_first, fx_last = fx.coverage("USD")
    return dict(
        meta=dict(
            tool_version=TOOL_VERSION,
            generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            source_files=stmt.source_name,
            statement_title=stmt.title, when_generated=stmt.when_generated,
            account=(stmt.account[:2] + "***" + stmt.account[-3:]
                     if opts.account_mask and len(stmt.account) > 5 else stmt.account),
            name=stmt.name, base_currency=stmt.base_currency,
            period=f"{stmt.period_start} to {stmt.period_end}",
            fy=fy_label(opts.fy_end_year), fy_start=str(fy_start), fy_end=str(fy_end),
            fx_source=f"RBA F11.1 daily, coverage {fx_first} to {fx_last}",
        ),
        summary=summary,
        closed_lots=sorted(closed_rows, key=lambda r: (r["close_date"], r["symbol"])),
        d2_open=sorted(d2_rows, key=lambda r: (r["write_date"], r["symbol"])),
        transfers=transfer_rows,
        carry_forward=carry_rows,
        unmatched=unmatched_rows,
        fx_realisations=fx_real_rows,
        forex_notes=forex_notes,
        entity_notes=entity_notes,
        amendment_flags=amendment_flags,
        cross_year_notes=prior_written_notes + d2_later_closed + d2_later_assigned
        + exercise_notes,
        other_income=dict(dividends=div_rows, withholding_tax=wht_rows,
                          australian_withholding=aus_rows, interest=int_rows,
                          fees=fee_rows, borrow_fees=bor_rows, forex_pl=fxp_rows),
        reconciliation=dict(
            rows_checked=row_ok + row_bad, rows_ok=row_ok, rows_mismatched=row_bad,
            row_mismatches=row_bad_rows[:50],
            positions_applicable=recon_position_applicable,
            positions_ok=pos_ok, position_diffs=pos_diffs[:50]),
        warnings=warnings,
    )
