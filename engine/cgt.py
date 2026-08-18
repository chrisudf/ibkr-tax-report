"""Australian CGT engine for IBKR statements.

Methodology (each point verified against ATO sources; see docs/TAX_RULES.md):

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
  total is also reported for comparison.
- If a written option is later exercised/assigned, the D2 gain is disregarded
  (s 104-40(5)) and the premium is folded into the underlying share parcel
  (ss 134-1, 116-65): reduces cost base of shares bought under a put, increases
  capital proceeds of shares delivered under a call. Assignments that cancel a
  D2 gain already returned in an earlier year raise an amendment flag
  (amendment period is unlimited for this event: s 170(10) ITAA 1936).
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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .fx import RbaRates, FxError
from .parser import Statement, Trade

TOOL_VERSION = "0.1.0"

CGT_CATEGORIES = ("Stocks", "Equity and Index Options")
ENTITY_DISCOUNT = {"individual": 0.5, "trust": 0.5, "smsf": 1.0 / 3.0, "company": 0.0}
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


def held_12_months(open_d: date, close_d: date) -> bool:
    """s 115-25(1): asset acquired at least 12 months before the CGT event."""
    try:
        anniversary = open_d.replace(year=open_d.year + 1)
    except ValueError:  # 29 Feb
        anniversary = open_d.replace(year=open_d.year + 1, day=28)
    return close_d > anniversary


_OPT_RE = re.compile(r"^(?P<under>[A-Z0-9.]+)\s+(?P<exp>\d{2}[A-Z]{3}\d{2})\s+"
                     r"(?P<strike>\d+(?:\.\d+)?)\s+(?P<right>[CP])$")


def parse_option_symbol(sym: str):
    m = _OPT_RE.match(sym.strip())
    if not m:
        return None
    return m.group("under"), m.group("exp"), float(m.group("strike")), m.group("right")


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


@dataclass
class UnmatchedClose:
    trade: Trade
    qty_unmatched: float
    close_cash_alloc: float
    ib_rpl: float
    resolved: bool            # True when we could fall back to IBKR's Realized P/L
    aud_gain: float = 0.0


@dataclass
class FifoResult:
    matches: list[Match] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)
    unmatched: list[UnmatchedClose] = field(default_factory=list)
    books: dict = field(default_factory=dict)     # (cat, sym) -> [Lot]
    row_checks: list = field(default_factory=list)  # (trade, my_rpl)
    warnings: list[str] = field(default_factory=list)


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
    under, exp, new_strike, right = parsed
    target_strike = new_strike * ev.ratio
    candidates = []
    for (cat, sym), lots in books.items():
        if cat != ev.category or not lots:
            continue
        p = parse_option_symbol(sym)
        if p and p[0] == under and p[1] == exp and p[3] == right \
                and abs(p[2] - target_strike) <= 0.05 * ev.ratio:
            candidates.append((cat, sym))
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
             suppress_fold_matches: bool = True) -> FifoResult:
    """One chronological FIFO pass.

    fold_adjustments: trade index -> cash to add to that (share) trade's leg,
    computed in a previous pass from assignment/exercise transfers.
    """
    res = FifoResult()
    books: dict = defaultdict(list)
    res.books = books
    fold_adjustments = fold_adjustments or {}
    split_i = 0

    for idx, t in enumerate(trades):
        if t.category not in CGT_CATEGORIES:
            continue
        while split_i < len(splits) and splits[split_i].dt <= t.dt:
            _apply_split(books, splits[split_i], res.warnings)
            split_i += 1

        q = t.qty
        cash = t.cash + fold_adjustments.get(idx, 0.0)
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

        while abs(remaining) > QTY_EPS and lots and lots[0].qty * remaining < 0:
            lot = lots[0]
            take = min(abs(remaining), abs(lot.qty))
            open_alloc = lot.cash * (take / abs(lot.qty))
            close_alloc = cash * (take / abs(q))
            lot.qty += take if remaining > 0 else -take
            lot.cash -= open_alloc
            remaining += take if remaining < 0 else -take
            rem_cash -= close_alloc
            lot_open_dt = lot.dt
            if abs(lot.qty) < QTY_EPS:
                lots.pop(0)
            if fold_close and suppress_fold_matches:
                res.transfers.append(Transfer(
                    option_symbol=t.symbol, option_qty=take,
                    kind="assignment" if "A" in t.codes else "exercise",
                    dt=t.dt, cash=open_alloc, close_cash=close_alloc,
                    currency=t.currency, option_open_dt=lot_open_dt))
            else:
                res.matches.append(Match(
                    category=t.category, symbol=t.symbol, currency=t.currency,
                    qty=take, open_dt=lot_open_dt, close_dt=t.dt,
                    open_cash=open_alloc, close_cash=close_alloc,
                    short=q > 0, via_expiry="Ep" in t.codes))
                my_rpl += open_alloc + close_alloc

        if abs(remaining) > QTY_EPS:
            if "C" in t.codes and "O" not in t.codes and not fold_close:
                # A pure closing trade with nothing left to match: opening lots
                # predate the uploaded data. Do NOT create a phantom position.
                unmatched_qty = remaining
                unmatched_cash = rem_cash
                rpl_alloc = t.ib_rpl * (abs(remaining) / abs(q)) if t.ib_rpl else 0.0
                res.unmatched.append(UnmatchedClose(
                    trade=t, qty_unmatched=remaining, close_cash_alloc=rem_cash,
                    ib_rpl=rpl_alloc, resolved=bool(t.ib_rpl)))
            else:
                books[(t.category, t.symbol)].append(
                    Lot(qty=remaining, cash=rem_cash, dt=t.dt, currency=t.currency))

        if "C" in t.codes and not fold_close and abs(unmatched_qty) < QTY_EPS:
            res.row_checks.append((t, my_rpl))

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


def _normalize_symbol_renames(stmt: Statement) -> list[str]:
    """Unify symbols renamed mid-period (e.g. ticker changes) using the
    Financial Instrument Information conid mapping."""
    notes = []
    rename: dict[str, str] = {}
    for conid, syms in stmt.conid_symbols.items():
        if len(syms) < 2:
            continue
        last_seen: dict[str, datetime] = {}
        for t in stmt.trades:
            if t.symbol in syms:
                last_seen[t.symbol] = t.dt
        open_syms = {p.symbol for p in stmt.open_positions}
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
    if rename:
        for t in stmt.trades:
            if t.symbol in rename:
                t.symbol = rename[t.symbol]
        for p in stmt.open_positions:
            if p.symbol in rename:
                p.symbol = rename[p.symbol]
        for old, new in sorted(rename.items()):
            notes.append(f"symbol change unified via conid: {old} -> {new}")
    return notes


def _sum_cash_aud(items, fx: RbaRates):
    rows, total_aud = [], 0.0
    by_ccy = defaultdict(float)
    for it in items:
        aud, rate = fx.to_aud(it.amount, it.currency, it.d)
        rows.append(dict(currency=it.currency, date=str(it.d), description=it.description,
                         amount=round(it.amount, 2), fx=rate, aud=round(aud, 2)))
        total_aud += aud
        by_ccy[it.currency] += it.amount
    return rows, round(total_aud, 2), {k: round(v, 2) for k, v in by_ccy.items()}


def compute_tax_report(stmt: Statement, opts: Options, fx: RbaRates | None = None) -> dict:
    fx = fx or RbaRates()
    warnings = list(stmt.warnings)
    fy_start, fy_end = fy_window(opts.fy_end_year)
    if opts.entity not in ENTITY_DISCOUNT:
        raise EngineError(f"unknown entity type {opts.entity!r}")
    discount_rate = ENTITY_DISCOUNT[opts.entity]

    warnings.extend(_normalize_symbol_renames(stmt))

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

    excluded = defaultdict(int)
    for t in stmt.trades:
        if t.category not in CGT_CATEGORIES and t.category != "Forex":
            excluded[t.category] += 1
    for cat, n in excluded.items():
        warnings.append(f"{n} {cat} trades excluded — asset category not supported; "
                        "handle manually.")

    splits, ca_warnings = plan_corporate_actions(stmt)
    warnings.extend(ca_warnings)

    trades = stmt.trades
    # Pass 1: discover assignment/exercise transfers.
    pass1 = run_fifo(trades, splits)
    fold_adj = _attach_transfers(trades, pass1.transfers)
    orphan_transfers = [tr for tr in pass1.transfers if tr.stock_trade_idx is None]
    for tr in orphan_transfers:
        warnings.append(
            f"{tr.kind} of {tr.option_symbol} on {tr.dt.date()} has no matching share "
            "trade (cash-settled or missing data) — treated as a normal option close.")
    # Pass 2: definitive run with share-leg cash adjusted by folded premiums.
    # Orphan folds (no share leg) fall back to normal matches.
    res = run_fifo(trades, splits, fold_adjustments=fold_adj,
                   suppress_fold_matches=True)
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
                    open_cash=tr.cash, close_cash=tr.close_cash, short=tr.cash > 0))
            else:
                res_transfers.append(tr)
        res.transfers = res_transfers
    _attach_transfers(trades, res.transfers)  # annotate pass-2 objects for display
    warnings.extend(res.warnings)

    # ---------------- realized matches, AUD, FY filter ----------------
    closed_rows = []
    tot = dict(gains_disc=0.0, gains_nondisc=0.0, losses=0.0, net=0.0)
    prior_written_notes = []
    for m in res.matches:
        close_d = m.close_dt.date()
        if not (fy_start <= close_d <= fy_end):
            continue
        is_option = m.category == "Equity and Index Options"
        open_d = m.open_dt.date()
        if m.short and is_option and fy_of(open_d) < opts.fy_end_year:
            # Written option granted in a prior FY: premium was a D2 gain in
            # that year. This year only the buy-back cost is a capital loss.
            aud_close, fxr = fx.to_aud(m.close_cash, m.currency, close_d)
            g = aud_close
            m.aud_gain, m.close_fx = g, fxr
            aud_open, m.open_fx = fx.to_aud(m.open_cash, m.currency, open_d)
            note = (f"premium A${aud_open:,.2f} was a D2 gain in {fy_label(fy_of(open_d))}; "
                    f"only the close leg is recognised this year")
            prior_written_notes.append(f"{m.symbol}: {note}")
            disc = False
        else:
            aud_open, m.open_fx = fx.to_aud(m.open_cash, m.currency, open_d)
            aud_close, m.close_fx = fx.to_aud(m.close_cash, m.currency, close_d)
            g = aud_open + aud_close
            m.aud_gain = g
            disc = (not m.short) and g > 0 and held_12_months(open_d, close_d)
            note = ""
        if g >= 0:
            tot["gains_disc" if disc else "gains_nondisc"] += g
        else:
            tot["losses"] += g
        tot["net"] += g
        closed_rows.append(dict(
            category=m.category, symbol=m.symbol, currency=m.currency,
            qty=round(m.qty, 4), open_date=str(open_d), close_date=str(close_d),
            days_held=(close_d - open_d).days,
            open_cash=round(m.open_cash, 2), close_cash=round(m.close_cash, 2),
            open_fx=m.open_fx, close_fx=m.close_fx,
            gain_native=round(m.gain_native, 2), gain_aud=round(g, 2),
            short="Y" if m.short else "", expiry="Y" if m.via_expiry else "",
            discount_eligible="Y" if disc else "", note=note))

    # ---------------- unmatched closes ----------------
    unmatched_rows, unmatched_total_aud, unresolved_count = [], 0.0, 0
    for u in res.unmatched:
        close_d = u.trade.dt.date()
        in_fy = fy_start <= close_d <= fy_end
        if u.resolved and in_fy:
            aud_g, fxr = fx.to_aud(u.ib_rpl, u.trade.currency, close_d)
            u.aud_gain = aud_g
            unmatched_total_aud += aud_g
        elif in_fy:
            unresolved_count += 1
            aud_g, fxr = 0.0, 0.0
        else:
            continue
        unmatched_rows.append(dict(
            category=u.trade.category, symbol=u.trade.symbol, currency=u.trade.currency,
            close_date=str(close_d), qty=round(u.qty_unmatched, 4),
            close_cash=round(u.close_cash_alloc, 2),
            ib_realized_pl=round(u.ib_rpl, 2) if u.resolved else "",
            gain_aud=round(aud_g, 2) if u.resolved else "UNRESOLVED",
            note="acquired before uploaded data; IBKR Realized P/L used at close-date FX; "
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
    tot["gains_nondisc"] += max(unmatched_total_aud, 0.0) if unmatched_total_aud >= 0 else 0.0
    if unmatched_total_aud < 0:
        tot["losses"] += unmatched_total_aud
    tot["net"] += unmatched_total_aud

    # ---------------- D2: written options open at FY end ----------------
    d2_rows, d2_total = [], 0.0
    for (cat, sym), lots in sorted(res.books.items()):
        if cat != "Equity and Index Options":
            continue
        for lot in lots:
            if lot.qty >= -QTY_EPS:
                continue
            open_d = lot.dt.date()
            if not (fy_start <= open_d <= fy_end):
                if open_d < fy_start:
                    prior_written_notes.append(
                        f"{sym}: written {open_d} (prior FY), still open — D2 belongs to "
                        f"{fy_label(fy_of(open_d))}")
                continue
            prem_aud, fxr = fx.to_aud(lot.cash, lot.currency, open_d)
            d2_total += prem_aud
            d2_rows.append(dict(symbol=sym, write_date=str(open_d), qty=round(lot.qty, 4),
                                premium_native=round(lot.cash, 2), currency=lot.currency,
                                fx=fxr, premium_aud=round(prem_aud, 2)))
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
                                    fx=fxr, premium_aud=round(prem_aud, 2)))
                d2_later_closed.append(
                    f"{m.symbol}: written {od}, closed {cd} (next FY) — D2 gain stays in "
                    f"{fy_label(opts.fy_end_year)}; close leg is a capital loss in "
                    f"{fy_label(fy_of(cd))}")
    # Transfers that cancel a prior-year D2 -> amendment flags
    amendment_flags = []
    for tr in res.transfers:
        od = tr.option_open_dt.date()
        if fy_of(od) < fy_of(tr.dt.date()) and fy_of(tr.dt.date()) == opts.fy_end_year:
            prem_aud, _ = fx.to_aud(tr.cash, tr.currency, od)
            amendment_flags.append(
                f"{tr.option_symbol}: written {od} ({fy_label(fy_of(od))}), "
                f"{tr.kind} on {tr.dt.date()} — the A${prem_aud:,.2f} D2 gain returned in "
                f"{fy_label(fy_of(od))} is disregarded (s 104-40(5)); amend that return "
                "(unlimited amendment period, s 170(10) ITAA 1936). Premium folded into the "
                f"share parcel {tr.stock_symbol}.")

    # ---------------- transfers (this-FY assignment/exercise folds) --------
    transfer_rows = [dict(option=tr.option_symbol, kind=tr.kind, date=str(tr.dt.date()),
                          option_written=str(tr.option_open_dt.date()),
                          qty=round(tr.option_qty, 4),
                          premium_native=round(tr.cash, 2), currency=tr.currency,
                          folded_into=tr.stock_symbol or "(unmatched)")
                     for tr in res.transfers
                     if fy_start <= tr.dt.date() <= fy_end]

    # ---------------- carry-forward positions ----------------
    carry_rows = []
    for (cat, sym), lots in sorted(res.books.items()):
        for lot in lots:
            if abs(lot.qty) < QTY_EPS:
                continue
            cost_native = -lot.cash
            cost_aud, fxr = fx.to_aud(cost_native, lot.currency, lot.dt.date())
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
            if not explained and abs(mq - sq) <= 1e-4:
                explained = ("cost-only difference — often a return of capital or other "
                             "corporate adjustment IBKR applied to its basis; verify against "
                             "the fund's annual tax statement")
            pos_diffs.append(dict(symbol=sym, my_qty=round(mq, 4), stmt_qty=round(sq, 4),
                                  my_cost=round(mc, 2), stmt_cost=round(sc, 2),
                                  note=explained or "review"))

    # ---------------- other income ----------------
    div_rows, div_aud, div_ccy = _sum_cash_aud(stmt.dividends, fx)
    wht_rows, wht_aud, wht_ccy = _sum_cash_aud(stmt.withholding_tax, fx)
    int_rows, int_aud, int_ccy = _sum_cash_aud(stmt.interest, fx)
    fee_rows, fee_aud, fee_ccy = _sum_cash_aud(stmt.fees, fx)
    bor_rows, bor_aud, bor_ccy = _sum_cash_aud(stmt.borrow_fees, fx)
    fxp_rows, fxp_aud, fxp_ccy = _sum_cash_aud(stmt.forex_pl, fx)
    if not stmt.forex_pl and any(t.category == "Forex" for t in stmt.trades):
        warnings.append(
            "Forex trades exist but no 'Forex P/L Details' section was found. Realised FX "
            "gains/losses (Div 775, ordinary income) are NOT included — also upload the "
            "Realized Summary export, which contains that section.")

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

    summary = dict(
        fy=fy_label(opts.fy_end_year), entity=opts.entity,
        discount_rate=discount_rate,
        closed_gains_discountable=round(gains_disc, 2),
        closed_gains_nondiscountable=round(tot["gains_nondisc"], 2),
        closed_losses=round(tot["losses"], 2),
        closed_net=round(tot["net"], 2),
        d2_open_written=round(d2_total, 2),
        strict_subtotal=round(strict_subtotal, 2),
        deferred_alternative=round(tot["net"], 2),
        current_year_losses=round(losses_current, 2),
        carried_losses_applied=round(min(opts.carried_losses, use_nd + use_d), 2),
        carried_losses_input=round(opts.carried_losses, 2),
        losses_applied_total=round(use_nd + use_d, 2),
        discount_applied=round(discount_amt, 2),
        net_capital_gain_18A=round(net_capital_gain, 2),
        total_capital_gains_18H=round(total_gains_18h, 2),
        losses_carried_forward_18V=round(rem_losses, 2),
        other_income=dict(
            dividends_aud=div_aud, withholding_tax_aud=wht_aud,
            interest_aud=int_aud, fees_aud=fee_aud, borrow_fees_aud=bor_aud,
            forex_pl_aud=fxp_aud,
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
        amendment_flags=amendment_flags,
        cross_year_notes=prior_written_notes + d2_later_closed,
        other_income=dict(dividends=div_rows, withholding_tax=wht_rows, interest=int_rows,
                          fees=fee_rows, borrow_fees=bor_rows, forex_pl=fxp_rows),
        reconciliation=dict(
            rows_checked=row_ok + row_bad, rows_ok=row_ok, rows_mismatched=row_bad,
            row_mismatches=row_bad_rows[:50],
            positions_applicable=recon_position_applicable,
            positions_ok=pos_ok, position_diffs=pos_diffs[:50]),
        warnings=warnings,
    )
