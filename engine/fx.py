"""RBA daily exchange rates (statistical table F11.1).

ATO requires foreign-currency amounts to be translated at the exchange rate
prevailing at the time of the transaction (s 960-50 ITAA 1997). The ATO
publishes RBA rates as an acceptable source. Rates are quoted as A$1 = X
foreign units, so AUD amount = foreign amount / rate.

For non-trading days (weekends, public holidays) we fall back to the most
recent prior published rate, consistent with ATO guidance to use the rate
"on the day" or the nearest available.
"""
from __future__ import annotations

import csv
import os
from datetime import date, datetime, timedelta

DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "rba_f11.csv")

# Map F11.1 column headers to ISO currency codes.
_HEADER_TO_CCY = {
    "A$1=USD": "USD", "A$1=CNY": "CNY", "A$1=JPY": "JPY", "A$1=EUR": "EUR",
    "A$1=KRW": "KRW", "A$1=GBP": "GBP", "A$1=SGD": "SGD", "A$1=INR": "INR",
    "A$1=THB": "THB", "A$1=NZD": "NZD", "A$1=TWD": "TWD", "A$1=MYR": "MYR",
    "A$1=IDR": "IDR", "A$1=VND": "VND", "A$1=AED": "AED", "A$1=PGK": "PGK",
    "A$1=HKD": "HKD", "A$1=CAD": "CAD", "A$1=ZAR": "ZAR", "A$1=CHF": "CHF",
    "A$1=PHP": "PHP", "A$1=SDR": "SDR",
}

MAX_BACKTRACK_DAYS = 10


class FxError(Exception):
    pass


class RbaRates:
    """Daily AUD/foreign rates from one or more RBA F11.1 CSV files."""

    def __init__(self, paths: list[str] | None = None):
        self.rates: dict[str, dict[date, float]] = {}
        self.last_date: dict[str, date] = {}
        self.first_date: dict[str, date] = {}
        for p in (paths or [DATA_FILE]):
            self._load(p)

    def _load(self, path: str) -> None:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            col_ccy: dict[int, str] = {}
            for row in csv.reader(fh):
                if not row or not row[0].strip():
                    continue
                first = row[0].strip()
                if first == "Title":
                    col_ccy = {i: _HEADER_TO_CCY[h.strip()]
                               for i, h in enumerate(row) if h.strip() in _HEADER_TO_CCY}
                    continue
                try:
                    d = datetime.strptime(first, "%d-%b-%Y").date()
                except ValueError:
                    continue
                if not col_ccy:
                    raise FxError(f"{path}: data rows before Title header — not an RBA F11.1 file?")
                for i, ccy in col_ccy.items():
                    if i < len(row) and row[i].strip():
                        self.rates.setdefault(ccy, {})[d] = float(row[i])
        for ccy, table in self.rates.items():
            self.first_date[ccy] = min(table)
            self.last_date[ccy] = max(table)

    def rate(self, ccy: str, d: date) -> tuple[float, date]:
        """A$1 = rate units of ccy on date d (backtracking to the most recent
        prior published day). Returns (rate, rate_date_used)."""
        if ccy == "AUD":
            return 1.0, d
        table = self.rates.get(ccy)
        if not table:
            raise FxError(f"no RBA rate series for {ccy}")
        dd = d
        for _ in range(MAX_BACKTRACK_DAYS + 1):
            if dd in table:
                return table[dd], dd
            dd -= timedelta(days=1)
        raise FxError(f"no RBA {ccy} rate within {MAX_BACKTRACK_DAYS} days on or before {d}")

    def to_aud(self, amount: float, ccy: str, d: date) -> tuple[float, float]:
        """Convert amount of ccy on date d. Returns (aud_amount, rate_used)."""
        r, _ = self.rate(ccy, d)
        return amount / r, r

    def coverage(self, ccy: str = "USD") -> tuple[date, date]:
        if ccy not in self.rates:
            raise FxError(f"no RBA rate series for {ccy}")
        return self.first_date[ccy], self.last_date[ccy]
