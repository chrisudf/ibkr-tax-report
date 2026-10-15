"""Validated refresh of the RBA F11.1 exchange-rate table.

A freshly downloaded copy replaces the table in use only if it

  1. parses as an F11.1 file with a USD series,
  2. covers a longer range: starts no later and ends strictly later, and
  3. agrees row for row with the table in use wherever the two overlap
     (same dates, same rates, every currency).

Anything else keeps the table in use and reports why. The RBA's current file
is a rolling window starting in 2023; if it ever moves its start forward,
rule 2 refuses it rather than silently losing the older rates.

    python rba_refresh.py        # refresh data/rba_f11.csv in place

The hosted app (app.py) runs the same check on a timer and hot-swaps the
rates in memory; see IBKR_TAX_RBA_DIR in deploy/README.md.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import urllib.request
from dataclasses import dataclass

from engine.fx import DATA_FILE, RbaRates

RBA_URL = "https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv"
USER_AGENT = "ibkr-tax-report (+https://github.com/chrisudf/ibkr-tax-report)"
MAX_BYTES = 5 * 1024 * 1024   # the real file is ~140 KB
TIMEOUT = 30                  # seconds


class Rejected(Exception):
    """The candidate table failed validation; the table in use stays."""


@dataclass
class Outcome:
    status: str        # "updated" | "unchanged" | "rejected" | "failed"
    detail: str
    rates: RbaRates | None = None   # the new table, when status == "updated"


def fetch(url: str = RBA_URL, timeout: float = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise Rejected(f"download is larger than {MAX_BYTES // (1024 * 1024)} MB")
    return data


def compare(current: RbaRates, candidate: RbaRates) -> str:
    """Validate candidate against current. Returns "updated" (candidate is
    strictly newer and consistent) or "unchanged" (it adds nothing); raises
    Rejected otherwise."""
    try:
        new_first, new_last = candidate.coverage("USD")
    except Exception as e:
        raise Rejected(f"no USD series ({e})") from None
    # every rate, including dates before the table in use: amounts are divided
    # by the rate, so 0 divides by zero and inf silently converts to A$0
    for ccy, table in candidate.rates.items():
        for d, v in table.items():
            if not (math.isfinite(v) and v > 0):
                raise Rejected(f"invalid rate {v} for {ccy} on {d}")
    old_first, old_last = current.coverage("USD")
    if new_first > old_first:
        raise Rejected(f"starts {new_first}, later than the table in use ({old_first}); "
                       "rates before then would be lost")
    if new_last < old_last:
        raise Rejected(f"ends {new_last}, earlier than the table in use ({old_last})")

    lo, hi = old_first, old_last   # the overlap: candidate covers all of current
    for ccy in sorted(set(current.rates) | set(candidate.rates)):
        old = {d: v for d, v in current.rates.get(ccy, {}).items() if lo <= d <= hi}
        new = {d: v for d, v in candidate.rates.get(ccy, {}).items() if lo <= d <= hi}
        if old == new:
            continue
        for d in sorted(set(old) | set(new)):
            if old.get(d) != new.get(d):
                raise Rejected(f"overlap differs: {ccy} on {d} is {old.get(d)} in the table "
                               f"in use, {new.get(d)} in the download")

    return "updated" if new_last > old_last else "unchanged"


def load_candidate(path: str) -> RbaRates:
    try:
        return RbaRates([path])
    except Exception as e:  # FxError, ValueError from float(), UnicodeDecodeError, ...
        raise Rejected(f"could not be parsed as RBA F11.1 ({type(e).__name__}: {e})") from None


def refresh(current: RbaRates, dest: str, fetch_fn=fetch) -> Outcome:
    """Download, validate against `current`, and on success atomically write
    the new table to `dest`. Never raises."""
    try:
        data = fetch_fn()
    except Rejected as e:
        return Outcome("rejected", str(e))
    except Exception as e:  # URLError, HTTPError, timeout, connection reset
        return Outcome("failed", f"download failed ({type(e).__name__}: {e})")

    dest_dir = os.path.dirname(os.path.abspath(dest))
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".rba_f11.", suffix=".csv", dir=dest_dir)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        candidate = load_candidate(tmp)
        status = compare(current, candidate)
        first, last = candidate.coverage("USD")
        if status == "updated":
            os.replace(tmp, dest)
            old_last = current.coverage("USD")[1]
            return Outcome("updated", f"coverage now {first} to {last} (was to {old_last})",
                           candidate)
        return Outcome("unchanged", f"download ends {last}, same as the table in use")
    except Rejected as e:
        return Outcome("rejected", str(e))
    except OSError as e:   # directory missing, unwritable or full
        return Outcome("failed", f"could not write {dest} ({e})")
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def load_best(bundled: str, refreshed: str | None) -> tuple[RbaRates, str]:
    """The rate table to start with: the refreshed copy if it validates
    against the bundled one and is newer, else the bundled one."""
    base = RbaRates([bundled])
    if not refreshed or not os.path.exists(refreshed):
        return base, "bundled table"
    try:
        candidate = load_candidate(refreshed)
        if compare(base, candidate) == "updated":
            return candidate, f"refreshed copy {refreshed}"
        return base, "bundled table (refreshed copy adds nothing)"
    except Rejected as e:
        return base, f"bundled table (ignored {refreshed}: {e})"


def main() -> int:
    current = RbaRates([DATA_FILE])
    outcome = refresh(current, DATA_FILE)
    print(f"{outcome.status}: {outcome.detail}")
    return 0 if outcome.status in ("updated", "unchanged") else 1


if __name__ == "__main__":
    sys.exit(main())
